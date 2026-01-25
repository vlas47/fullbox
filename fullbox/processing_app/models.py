from django.db import models


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
