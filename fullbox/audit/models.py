from django.conf import settings
from django.db import models


def sku_snapshot(sku):
    """Минимальный слепок SKU для лога."""
    if not sku:
        return {}
    return {
        "id": sku.id,
        "sku_code": sku.sku_code,
        "name": sku.name,
        "brand": sku.brand,
        "agency_id": getattr(sku.agency, "id", None),
        "market_id": getattr(sku.market, "id", None),
        "color": sku.color,
        "size": sku.size,
        "honest_sign": sku.honest_sign,
        "use_nds": sku.use_nds,
        "updated_at": sku.updated_at.isoformat() if sku.updated_at else None,
    }


class AuditEntry(models.Model):
    ACTION_CHOICES = [
        ("create", "Создание"),
        ("update", "Изменение"),
        ("delete", "Удаление"),
        ("clone", "Клонирование"),
    ]

    action = models.CharField("Действие", max_length=32, choices=ACTION_CHOICES)
    sku = models.ForeignKey("sku.SKU", on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_entries")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Пользователь"
    )
    description = models.TextField("Описание", blank=True)
    snapshot = models.JSONField("Снимок", null=True, blank=True)
    created_at = models.DateTimeField("Когда", auto_now_add=True)

    class Meta:
        verbose_name = "Журнал изменений SKU"
        verbose_name_plural = "Журналы изменений SKU"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_action_display()} {self.sku} ({self.created_at:%Y-%m-%d %H:%M})"


def log_sku_change(action: str, sku, user=None, description: str = "", snapshot: dict | None = None):
    """Утилита для записи события по SKU."""
    snap = snapshot if snapshot is not None else sku_snapshot(sku)
    AuditEntry.objects.create(action=action, sku=sku, user=user, description=description, snapshot=snap)

# Create your models here.
