from django.conf import settings
from django.db import models

from sku.models import Agency, SKU


class MarkingCode(models.Model):
    ORDER_TYPE_CHOICES = [
        ("processing", "Обработка"),
        ("receiving", "Приемка"),
        ("placement", "Размещение"),
        ("shipping", "Отгрузка"),
        ("other", "Прочее"),
    ]
    SOURCE_CHOICES = [
        ("scan", "Сканер"),
        ("import", "Импорт"),
    ]

    order_type = models.CharField(max_length=32, choices=ORDER_TYPE_CHOICES, default="processing")
    order_id = models.CharField(max_length=64, blank=True)
    agency = models.ForeignKey(
        Agency,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="marking_codes",
        verbose_name="Клиент",
    )
    sku = models.ForeignKey(
        SKU,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="marking_codes",
        verbose_name="SKU",
    )
    sku_code = models.CharField("Артикул", max_length=64)
    size = models.CharField("Размер", max_length=64, blank=True)
    barcode = models.CharField("Штрихкод", max_length=128, blank=True)
    code = models.TextField("Код ЧЗ", unique=True)
    source = models.CharField("Источник", max_length=16, choices=SOURCE_CHOICES, default="scan")
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="marking_codes",
        verbose_name="Пользователь",
    )

    class Meta:
        verbose_name = "Код ЧЗ"
        verbose_name_plural = "Коды ЧЗ"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["order_type", "order_id"]),
            models.Index(fields=["sku_code", "size"]),
        ]

    def __str__(self) -> str:
        return f"{self.code} ({self.sku_code} {self.size})"
