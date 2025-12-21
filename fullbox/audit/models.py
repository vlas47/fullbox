from django.conf import settings
from django.db import models
from django.utils import timezone


class AuditJournal(models.Model):
    code = models.CharField("Код", max_length=64, unique=True)
    name = models.CharField("Название", max_length=255)
    description = models.TextField("Описание", blank=True)

    class Meta:
        verbose_name = "Журнал"
        verbose_name_plural = "Журналы"
        ordering = ["code"]

    def __str__(self):
        return self.name


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

    journal = models.ForeignKey(
        AuditJournal, on_delete=models.CASCADE, related_name="entries", verbose_name="Журнал"
    )
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


def get_sku_journal():
    return AuditJournal.objects.get_or_create(
        code="sku",
        defaults={
            "name": "Изменения номенклатуры",
            "description": "Фиксация всех операций с номенклатурой (создание, изменение, удаление, клонирование).",
        },
    )[0]


def log_sku_change(action: str, sku, user=None, description: str = "", snapshot: dict | None = None):
    """Утилита для записи события по SKU."""
    snap = snapshot if snapshot is not None else sku_snapshot(sku)
    journal = get_sku_journal()
    AuditEntry.objects.create(
        journal=journal,
        action=action,
        sku=sku,
        user=user,
        description=description,
        snapshot=snap,
    )


class OrderAuditEntry(models.Model):
    ACTION_CHOICES = [
        ("create", "Создание"),
        ("update", "Изменение"),
        ("status", "Статус"),
        ("comment", "Комментарий"),
        ("upload", "Файлы"),
    ]

    order_id = models.CharField("ID заявки", max_length=128)
    order_type = models.CharField("Тип заявки", max_length=64, default="receiving")
    action = models.CharField("Действие", max_length=32, choices=ACTION_CHOICES)
    agency = models.ForeignKey(
        "sku.Agency", on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Клиент"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Пользователь"
    )
    description = models.TextField("Описание", blank=True)
    payload = models.JSONField("Данные", null=True, blank=True)
    created_at = models.DateTimeField("Когда", default=timezone.now)

    class Meta:
        verbose_name = "Аудит заявки"
        verbose_name_plural = "Аудит заявок"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.order_type} {self.order_id} [{self.get_action_display()}]"


def log_order_action(
    action: str,
    order_id: str,
    order_type: str = "receiving",
    user=None,
    agency=None,
    description: str = "",
    payload: dict | None = None,
):
    OrderAuditEntry.objects.create(
        order_id=order_id,
        order_type=order_type,
        action=action,
        user=user,
        agency=agency,
        description=description,
        payload=payload or {},
    )

# Create your models here.
