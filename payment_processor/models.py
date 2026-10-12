import uuid
from django.db import models
from django.utils import timezone


class PluginSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    enabled = models.BooleanField(default=False, help_text='Plug/unplug Payment processor. Disabling stops its API and worker; existing LMS integrations are unaffected.')
    allow_provider_writes = models.BooleanField(default=False, help_text='Also requires PAYMENT_PROCESSOR_ALLOW_WRITES and ZUMRAILS_TRANSACTIONS_ENABLED in server settings. Each action requires a reviewed confirmation.')
    reconcile_minutes = models.PositiveSmallIntegerField(default=5)
    read_existing_webhooks = models.BooleanField(default=True, help_text='Read already-received LMS webhook events without changing or processing them in the LMS.')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Plugin switch'
        verbose_name_plural = 'Plugin switch'

    def __str__(self):
        return 'Payment processor — ' + ('plugged in' if self.enabled else 'unplugged')


class SyncRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scope = models.CharField(max_length=64, db_index=True)
    resource = models.CharField(max_length=30, db_index=True)
    status = models.CharField(max_length=20, default='queued', db_index=True)
    next_page = models.PositiveIntegerField(default=1)
    expected_count = models.PositiveIntegerField(null=True)
    fetched_count = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True)
    heartbeat_at = models.DateTimeField(null=True)
    retry_at = models.DateTimeField(null=True)
    error = models.CharField(max_length=250, blank=True)
    cursor = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [models.Index(fields=['scope', 'resource', 'status'])]


class SyncRow(models.Model):
    run = models.ForeignKey(SyncRun, on_delete=models.CASCADE, related_name='rows')
    provider_id = models.CharField(max_length=100)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(null=True, db_index=True)
    amount = models.DecimalField(max_digits=20, decimal_places=2, null=True)
    currency = models.CharField(max_length=8, blank=True)
    flow = models.CharField(max_length=50, blank=True, db_index=True)
    method = models.CharField(max_length=50, blank=True, db_index=True)
    status = models.CharField(max_length=80, blank=True, db_index=True)
    failure = models.CharField(max_length=255, blank=True)
    user_id = models.CharField(max_length=100, blank=True)
    user_name = models.CharField(max_length=255, blank=True)
    completed_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['run', 'provider_id'], name='processor_snapshot_object_unique')]


class DetailCache(models.Model):
    scope = models.CharField(max_length=64)
    resource = models.CharField(max_length=40)
    provider_id = models.CharField(max_length=100)
    payload = models.JSONField(default=dict)
    synced_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['scope', 'resource', 'provider_id'], name='processor_detail_unique')]


class WebhookInbox(models.Model):
    scope = models.CharField(max_length=64, db_index=True)
    digest = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)
    source = models.CharField(max_length=20, default='plugin')
    received_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    retry_at = models.DateTimeField(null=True)
    error = models.CharField(max_length=250, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['scope', 'digest'], name='processor_webhook_unique')]


class Operation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scope = models.CharField(max_length=64, db_index=True)
    actor_email = models.EmailField()
    action = models.CharField(max_length=50)
    payload_digest = models.CharField(max_length=64)
    summary = models.JSONField(default=dict)
    status = models.CharField(max_length=20, default='prepared')
    result = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    executed_at = models.DateTimeField(null=True)


class RequestBudget(models.Model):
    key = models.CharField(max_length=100, primary_key=True)
    timestamps = models.JSONField(default=list)


class WorkerLease(models.Model):
    scope = models.CharField(max_length=64, primary_key=True)
    holder = models.UUIDField(default=uuid.uuid4)
    expires_at = models.DateTimeField(default=timezone.now)
