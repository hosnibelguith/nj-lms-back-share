"""Explicit, expiring reviewed actions with durable duplicate protection."""
import hashlib
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
from .sync import queue_sync

FLOWS = ('AccountsPayable', 'AccountsReceivable', 'FundZumWallet', 'WithdrawZumWallet')
METHODS = ('Eft', 'Interac')
ACTIONS = ('create_transaction', 'cancel_transaction', 'create_user', 'update_user', 'update_bank', 'delete_user', 'update_billing_address')


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
    if action == 'cancel_transaction':
        data = allowed(raw, ('TransactionId',), ('TransactionId',))
        return 'DELETE', '/transaction/' + identifier(data['TransactionId']), None
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
    raise ValidationError('Unsupported payment processor action.')


def prepare(client, user, action, raw):
    method, path, payload = action_request(action, raw)
    summary = sanitize(raw)
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
        raise ProviderError('Live actions are disabled. Enable provider writes in the Payment processor admin switch and server configuration.', status=403)
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
        operation.status = 'submitted'
        operation.executed_at = timezone.now()
        operation.save(using='default', update_fields=['status', 'executed_at'])
    # No lock during HTTP. The persisted submitted state blocks double clicks,
    # retries and process crashes from replaying an uncertain external request.
    try:
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
    queue_sync(client)
    return operation
