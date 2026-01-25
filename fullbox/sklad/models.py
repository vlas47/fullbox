from django.db import models

from sku.models import Agency


class InventoryState(models.Model):
    STATE_PROCESSING = "processing"

    STATE_CHOICES = [
        (STATE_PROCESSING, "В обработке"),
    ]

    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="inventory_states")
    order_type = models.CharField(max_length=32, default="processing")
    order_id = models.CharField(max_length=64)
    sku = models.CharField(max_length=64)
    size = models.CharField(max_length=64, blank=True)
    barcode = models.CharField(max_length=64, blank=True)
    goods_type = models.CharField(max_length=64, blank=True)
    qty = models.PositiveIntegerField(default=0)
    state = models.CharField(max_length=32, choices=STATE_CHOICES, default=STATE_PROCESSING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["agency", "state"]),
            models.Index(fields=["agency", "sku"]),
            models.Index(fields=["order_type", "order_id"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "agency",
                    "order_type",
                    "order_id",
                    "sku",
                    "size",
                    "barcode",
                    "goods_type",
                    "state",
                ],
                name="uniq_inventory_state_row",
            )
        ]

    def __str__(self) -> str:
        return f"{self.sku} · {self.size or '-'} · {self.state}"
