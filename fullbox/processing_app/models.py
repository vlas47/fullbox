from django.conf import settings
from django.db import models

from employees.models import Employee


class ProcessingPrintJob(models.Model):
    STATUS_PENDING = "pending"
    STATUS_PRINTING = "printing"
    STATUS_PRINTED = "printed"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_PRINTING, "Printing"),
        (STATUS_PRINTED, "Printed"),
        (STATUS_FAILED, "Failed"),
    ]

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    order_id = models.CharField(max_length=64, blank=True)
    card_id = models.CharField(max_length=128, blank=True)
    article = models.CharField(max_length=128, blank=True)
    barcode = models.CharField(max_length=128)
    size = models.CharField(max_length=64, blank=True)
    printer_name = models.CharField(max_length=255, blank=True)
    label_png_base64 = models.TextField(blank=True)
    label_width_mm = models.PositiveIntegerField(default=58)
    label_height_mm = models.PositiveIntegerField(default=40)
    requested_by = models.CharField(max_length=150, blank=True)
    agent = models.CharField(max_length=128, blank=True)
    error = models.TextField(blank=True)

    def __str__(self) -> str:
        return f"PrintJob #{self.pk} ({self.barcode})"


class ProcessingFlowSession(models.Model):
    STATUS_OPEN = "open"
    STATUS_CLOSED = "closed"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Открыта"),
        (STATUS_CLOSED, "Закрыта"),
    ]

    order_id = models.CharField(max_length=64)
    order_type = models.CharField(max_length=32, default="processing")
    agent_id = models.CharField(max_length=128, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processing_flow_sessions",
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processing_flow_sessions",
    )
    flow_state = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_OPEN)
    last_seen = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["order_id", "status"]),
            models.Index(fields=["agent_id", "status"]),
            models.Index(fields=["user", "status"]),
        ]

    def __str__(self) -> str:
        label = self.agent_id or "agent"
        return f"FlowSession {self.order_id} ({label})"
