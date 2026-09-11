"""Explicit, expiring reviewed actions with durable duplicate protection."""
import base64
import csv
import hashlib
import io
import json
import re
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework import serializers

from .access import writes_enabled, require_enabled
from .client import ProviderError, sanitize, unwrap
from .models import Operation, PluginSettings
from .sync import queue_sync, transaction_detail

FLOWS = ('AccountsPayable', 'AccountsReceivable', 'FundZumWallet', 'WithdrawZumWallet', 'LoadCard', 'UnloadCard', 'StatementPayment')
METHODS = ('Eft', 'Interac', 'PrepaidCard', 'CreditCardIssuance')


def fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def identifier(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError):
        raise ValidationError('Select a valid ZUM record identifier.')


def amount(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result <= 0 or result > Decimal('999999999.99') or result != result.quantize(Decimal('.01')):
            raise ValueError
        return str(result.quantize(Decimal('.01')))
    except (ValueError, InvalidOperation):
        raise ValidationError('Amount must be positive with at most two decimal places.')


def allowed(payload, fields, required=()):
    if not isinstance(payload, dict) or set(payload) - set(fields):
        raise ValidationError('The action contains unsupported fields.')
    if any(payload.get(key) in (None, '') for key in required):
        raise ValidationError('Complete all required fields: ' + ', '.join(required))
    return dict(payload)


def validate_batch_file(encoded):
    try:
        content = base64.b64decode(encoded, validate=True)
        if len(content) > 2 * 1024 * 1024:
            raise ValueError
        text = content.decode('utf-8-sig')
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=',;\t')
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        rows = list(reader)
        if not rows or len(rows) > 2000:
            raise ValueError
        fields = {re.sub(r'[^a-z_]', '', key.lower()): key for key in reader.fieldnames or []}
        key = next((fields[name] for name in ('amount_in_cents', 'amount_in_cents_required', 'amount', 'amount_required') if name in fields), None)
        if not key:
            raise ValueError
        cents = 'cents' in key
        amounts = [Decimal(str(row[key])) / (100 if cents else 1) for row in rows]
        if any(not value.is_finite() or value <= 0 or value != value.quantize(Decimal('.01')) for value in amounts):
            raise ValueError
        return {'rows': len(rows), 'total_amount': str(sum(amounts).quantize(Decimal('.01'))), 'file_digest': hashlib.sha256(content).hexdigest()}
    except (ValueError, csv.Error, UnicodeError, KeyError, TypeError, InvalidOperation):
        raise ValidationError('Upload a valid UTF-8 ZUM CSV template (up to 2 MB / 2,000 rows) with positive amounts.')


def action_request(action, raw):
    if action == 'create_transaction':
        data = allowed(raw, ('ZumRailsType', 'TransactionMethod', 'Amount', 'UserId', 'WalletId', 'FundingSourceId', 'Memo', 'Comment', 'ScheduledStartDate', 'UseInteracANR', 'InteracHasSecurityQuestionAndAnswer', 'InteracSecurityQuestion', 'InteracSecurityAnswer'), ('ZumRailsType', 'TransactionMethod', 'Amount', 'Memo'))
        if data['ZumRailsType'] not in FLOWS or data['TransactionMethod'] not in METHODS:
            raise ValidationError('Select a supported money flow and payment method.')
        data['Amount'] = amount(data['Amount'])
        if not re.fullmatch(r'[A-Za-z0-9 _-]{1,15}', str(data['Memo'])):
            raise ValidationError('Memo must be 1–15 letters, numbers, spaces, underscores or dashes.')
        for key in ('UserId', 'WalletId', 'FundingSourceId'):
            if data.get(key):
                data[key] = identifier(data[key])
            else:
                data.pop(key, None)
        if data['ZumRailsType'] in ('FundZumWallet', 'WithdrawZumWallet'):
            if not data.get('WalletId') or not data.get('FundingSourceId') or data.get('UserId') or data['TransactionMethod'] != 'Eft':
                raise ValidationError('Wallet transfers require EFT, a wallet and a funding source.')
        elif not data.get('UserId') or bool(data.get('WalletId')) == bool(data.get('FundingSourceId')):
            raise ValidationError('Select a user and exactly one wallet or funding source.')
        if data.get('ScheduledStartDate'):
            date = serializers.DateField().run_validation(data['ScheduledStartDate'])
            if date <= timezone.localdate():
                raise ValidationError('Scheduled date must be after today.')
            data['ScheduledStartDate'] = date.isoformat()
        if data.get('InteracHasSecurityQuestionAndAnswer') and (not data.get('InteracSecurityQuestion') or not data.get('InteracSecurityAnswer')):
            raise ValidationError('Enter the Interac security question and answer.')
        return 'POST', '/transaction', data
    if action in ('cancel_transaction', 'refund_transaction', 'complete_transaction'):
        data = allowed(raw, ('TransactionId', 'Amount'), ('TransactionId',))
        path = '/transaction/' + identifier(data['TransactionId'])
        if action == 'cancel_transaction':
            return 'DELETE', path, None
        if action == 'complete_transaction':
            return 'POST', path + '/Completion', None
        return 'POST', path + '/Refund', {'Amount': amount(data.get('Amount'))}
    if action in ('create_user', 'update_user'):
        fields = ('UserId', 'FirstName', 'LastName', 'CompanyName', 'DateOfBirth', 'Email', 'PhoneNumber', 'ClientUserId')
        data = allowed(raw, fields, ('FirstName', 'LastName', 'Email'))
        data['Email'] = serializers.EmailField().run_validation(data['Email'])
        if data.get('DateOfBirth'):
            data['DateOfBirth'] = serializers.DateField().run_validation(data['DateOfBirth']).isoformat()
        if action == 'update_user':
            path = '/user/UpdateBasicInformation/' + identifier(data.pop('UserId', None))
            return 'PATCH', path, data
        data.pop('UserId', None)
        return 'POST', '/user', data
    if action == 'update_bank':
        data = allowed(raw, ('UserId', 'InstitutionNumber', 'TransitNumber', 'AccountNumber'), ('UserId', 'InstitutionNumber', 'TransitNumber', 'AccountNumber'))
        path = '/user/UpdateBankAccountInformation/' + identifier(data.pop('UserId'))
        if not re.fullmatch(r'\d{3}', str(data['InstitutionNumber'])) or not re.fullmatch(r'\d{5}', str(data['TransitNumber'])) or not re.fullmatch(r'\d{5,12}', str(data['AccountNumber'])):
            raise ValidationError('Use a 3-digit institution, 5-digit transit and 5–12-digit account number.')
        return 'PATCH', path, data
    if action == 'delete_user':
        data = allowed(raw, ('UserId',), ('UserId',))
        return 'DELETE', '/user/' + identifier(data['UserId']), None
    if action == 'update_billing_address':
        fields = ('UserId', 'AddressLine1', 'AddressLine2', 'AddressPostalCode', 'AddressCountry', 'AddressCity', 'AddressState')
        data = allowed(raw, fields, ('UserId', 'AddressLine1', 'AddressPostalCode', 'AddressCity', 'AddressState'))
        path = '/user/UpdateBillingAddressInformation/' + identifier(data.pop('UserId'))
        data['AddressCountry'] = 'Canada'
        return 'PATCH', path, data
    if action in ('pause_subscription', 'resume_subscription'):
        data = allowed(raw, ('SubscriptionId',), ('SubscriptionId',))
        verb = 'pause' if action == 'pause_subscription' else 'resume'
        return 'PUT', '/subscription/' + verb + '/' + identifier(data['SubscriptionId']), {'PauseDuration': 'Indefinite'} if verb == 'pause' else None
    if action in ('activate_card', 'update_card_status', 'update_card_program', 'reissue_card'):
        data = allowed(raw, ('UserId', 'UserCardId', 'CardStatus', 'NewProgramId'), ('UserId', 'UserCardId'))
        data['UserId'], data['UserCardId'] = identifier(data['UserId']), identifier(data['UserCardId'])
        if action == 'update_card_status' and data.get('CardStatus') not in ('Activated', 'Inactive', 'FraudBlock', 'Closed'):
            raise ValidationError('Select a supported card status.')
        if action == 'update_card_program' and not data.get('NewProgramId'):
            raise ValidationError('Enter the card program assigned by ZUM.')
        paths = {'activate_card': 'activate-card', 'update_card_status': 'update-card-status', 'update_card_program': 'update-card-program', 'reissue_card': 'reissue-card'}
        return 'POST', '/card/' + paths[action], data
    if action in ('validate_batch', 'process_batch'):
        data = allowed(raw, ('TransactionType', 'TransactionMethod', 'WalletId', 'FundingSourceId', 'Bytes', 'FileName', 'WithdrawSumTotalFromFundingSource', 'ValidationId'), ('TransactionType', 'TransactionMethod', 'Bytes'))
        if data['TransactionMethod'] not in ('Eft', 'Interac'):
            raise ValidationError('Select EFT or Interac for this batch.')
        if data['TransactionType'] not in ('AccountsPayable', 'AccountsReceivable') or bool(data.get('WalletId')) == bool(data.get('FundingSourceId')):
            raise ValidationError('Select AP or AR and exactly one wallet or funding source.')
        for key in ('WalletId', 'FundingSourceId'):
            if data.get(key):
                data[key] = identifier(data[key])
            else:
                data.pop(key, None)
        validate_batch_file(data['Bytes'])
        data.pop('ValidationId', None)
        if action == 'validate_batch':
            data.pop('FileName', None)
            data.pop('WithdrawSumTotalFromFundingSource', None)
            data.pop('TransactionMethod', None)
            return 'POST', '/transaction/ValidateBatchFile', data
        if not data.get('FileName') or not str(data['FileName']).lower().endswith('.csv'):
            raise ValidationError('Select a CSV filename.')
        data['SkipFileAlreadyProcessedInLast24Hours'] = True
        data.setdefault('WithdrawSumTotalFromFundingSource', False)
        return 'POST', '/transaction/ProcessBatchFile', data
    raise ValidationError('Unsupported payment processor action.')


def prepare(client, user, action, raw):
    method, path, payload = action_request(action, raw)
    summary = sanitize(raw)
    if action.endswith('_batch'):
        summary.update(validate_batch_file(raw['Bytes']))
    with transaction.atomic(using='default'):
        PluginSettings.objects.using('default').select_for_update().get(pk=1)
        operation = Operation.objects.using('default').filter(scope=client.scope, actor_email=user.email.casefold(), action=action,
            payload_digest=fingerprint(raw), status__in=['prepared', 'submitted', 'unknown', 'succeeded'],
            created_at__gte=timezone.now() - timedelta(minutes=10)).order_by('-created_at').first()
        if operation is None:
            operation = Operation.objects.using('default').create(scope=client.scope, actor_email=user.email.casefold(), action=action,
                payload_digest=fingerprint(raw), summary=summary, expires_at=timezone.now() + timedelta(minutes=10))
    return operation


def execute(client, user, operation_id, raw, confirmation):
    require_enabled()
    if not writes_enabled():
        raise ProviderError('Live provider changes are disabled for this local review.', status=403)
    if confirmation != 'CONFIRM':
        raise ValidationError('Type CONFIRM after reviewing this action.')
    with transaction.atomic(using='default'):
        try:
            operation = Operation.objects.using('default').select_for_update().get(pk=operation_id, scope=client.scope, actor_email=user.email.casefold())
        except Operation.DoesNotExist:
            raise ValidationError('The reviewed action was not found.')
        if operation.status == 'succeeded':
            return operation
        if operation.status != 'prepared' or operation.expires_at <= timezone.now():
            raise ValidationError('This action expired or was already submitted. Review its audit status before creating another.')
        if operation.payload_digest != fingerprint(raw):
            raise ValidationError('The action changed after review. Review it again.')
        method, path, payload = action_request(operation.action, raw)
        if operation.action == 'process_batch':
            valid = Operation.objects.using('default').filter(pk=identifier(raw.get('ValidationId')), scope=client.scope, actor_email=user.email.casefold(), action='validate_batch', status='succeeded').first()
            if not valid or valid.summary.get('file_digest') != operation.summary.get('file_digest') or str(valid.result.get('InvalidTransactions')) != '0' or valid.created_at < timezone.now() - timedelta(minutes=10):
                raise ValidationError('Validate this exact batch with ZUM successfully before processing it.')
            if any(valid.summary.get(key) != operation.summary.get(key) for key in ('TransactionType', 'TransactionMethod', 'WalletId', 'FundingSourceId')):
                raise ValidationError('Batch destination changed after validation.')
            if Operation.objects.using('default').filter(scope=client.scope, action='process_batch', status__in=['submitted', 'succeeded', 'unknown'],
                    summary__file_digest=operation.summary.get('file_digest'), created_at__gte=timezone.now() - timedelta(hours=24)).exists():
                raise ValidationError('This file has already been submitted within the last 24 hours. Check action history.')
        operation.status = 'submitted'
        operation.executed_at = timezone.now()
        operation.save(using='default', update_fields=['status', 'executed_at'])
    # No lock during HTTP. The persisted submitted state blocks double clicks,
    # retries and process crashes from replaying an uncertain external request.
    try:
        if operation.action in ('activate_card', 'update_card_status', 'update_card_program', 'reissue_card'):
            cards = unwrap(client.request('GET', '/card/get-cards/' + payload['UserId']))
            if not isinstance(cards, list) or not any(str(card.get('Id')) == payload['UserCardId'] for card in cards):
                raise ProviderError('The selected card does not belong to this user.', status=422)
        if operation.action == 'refund_transaction':
            original, _ = transaction_detail(client, identifier(raw['TransactionId']), force=True)
            if original.get('TransactionMethod') != 'CreditCard' or not original.get('IsRefundable', original.get('TransactionStatus') == 'Completed'):
                raise ProviderError('This transaction is not refundable through the supported credit-card refund API.', status=422)
            if Decimal(raw['Amount']) > Decimal(str(original['Amount'])):
                raise ProviderError('Refund amount exceeds the original transaction.', status=422)
        if operation.action == 'create_transaction':
            payload['ClientTransactionId'] = str(operation.id)
        result = unwrap(client.request(method, path, payload, operation_id=operation.id))
        operation.result = sanitize(result) if isinstance(result, dict) else {'response': sanitize(result)}
        operation.status = 'succeeded'
    except ProviderError as error:
        operation.status = 'unknown' if error.unknown else 'failed'
        operation.result = {'error': str(error)}
        operation.save(using='default', update_fields=['status', 'result'])
        raise
    operation.save(using='default', update_fields=['status', 'result'])
    queue_sync(client, ('subscriptions',)) if operation.action in ('pause_subscription', 'resume_subscription') else queue_sync(client)
    return operation
