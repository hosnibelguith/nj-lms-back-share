import uuid

from django.db import models
from django.utils import timezone


class PluginSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    enabled = models.BooleanField(default=False, help_text=(
        'Enable Mohawk reject delivery and the iCollector dashboard. Disabling pauses '
        'queued deliveries and polling. A request already sent may finish.'))
    capture_since = models.DateTimeField(null=True, blank=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Plugin switch'
        verbose_name_plural = 'Plugin switch'

    def save(self, *args, **kwargs):
        if self.enabled and self.capture_since is None:
            self.capture_since = timezone.now()
            if kwargs.get('update_fields') is not None:
                kwargs['update_fields'] = set(kwargs['update_fields']) | {'capture_since'}
        super().save(*args, **kwargs)

    def __str__(self):
        return 'iCollector — ' + ('enabled' if self.enabled else 'disabled')


class RejectDelivery(models.Model):
    # One immutable source attempt, irrespective of retries or webhook duplicates.
    source_id = models.UUIDField(primary_key=True)
    lender_id = models.UUIDField()
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, default='pending', db_index=True,
        choices=[('pending', 'Pending'), ('sending', 'Sending'),
                 ('delivered', 'Delivered'), ('blocked', 'Needs review'), ('cancelled', 'Cancelled')])
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    lease_id = models.UUIDField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    last_http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    last_error = models.CharField(max_length=200, blank=True)
    remote_row_id = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['created_at']

    @property
    def idempotency_key(self):
        return str(self.source_id)


class DashboardSnapshot(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    payload = models.JSONField(default=dict, blank=True)
    fetched_at = models.DateTimeField(null=True, blank=True)
    attempted_at = models.DateTimeField(null=True, blank=True)
    lease_id = models.UUIDField(default=uuid.uuid4)
    lease_until = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=200, blank=True)
