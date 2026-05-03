from django.conf import settings
from django.db import models

from sku.models import Agency


class MoveRequest(models.Model):
    CONTEXT_RECEIVING = "receiving"
    CONTEXT_PROCESSING = "processing"
    CONTEXT_MANUAL = "manual"
    CONTEXT_CHOICES = [
        (CONTEXT_RECEIVING, "Приемка"),
        (CONTEXT_PROCESSING, "Обработка"),
        (CONTEXT_MANUAL, "Ручной запрос"),
    ]

    PRIORITY_NORMAL = "normal"
    PRIORITY_HIGH = "high"
    PRIORITY_URGENT = "urgent"
    PRIORITY_CHOICES = [
        (PRIORITY_NORMAL, "Обычный"),
        (PRIORITY_HIGH, "Высокий"),
        (PRIORITY_URGENT, "Срочно"),
    ]

    STATUS_CREATED = "created"
    STATUS_PLANNED = "planned"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_PARTIAL = "partial"
    STATUS_DONE = "done"
    STATUS_BLOCKED = "blocked"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = [
        (STATUS_CREATED, "Создан"),
        (STATUS_PLANNED, "Спланирован"),
        (STATUS_IN_PROGRESS, "В работе"),
        (STATUS_PARTIAL, "Частично выполнен"),
        (STATUS_DONE, "Выполнен"),
        (STATUS_BLOCKED, "Заблокирован"),
        (STATUS_CANCELED, "Отменен"),
    ]

    context_type = models.CharField(max_length=16, choices=CONTEXT_CHOICES, default=CONTEXT_MANUAL)
    context_id = models.CharField(max_length=64, blank=True)
    agency = models.ForeignKey(
        Agency,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="move_requests",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="move_requests",
    )
    requested_by_role = models.CharField(max_length=32, blank=True)
    requested_by_name = models.CharField(max_length=255, blank=True)
    destination_zone = models.CharField(max_length=16, default="PR")
    destination_row = models.PositiveIntegerField(null=True, blank=True)
    destination_section = models.PositiveIntegerField(null=True, blank=True)
    destination_tier = models.PositiveIntegerField(null=True, blank=True)
    destination_cell = models.PositiveIntegerField(null=True, blank=True)
    priority = models.CharField(max_length=16, choices=PRIORITY_CHOICES, default=PRIORITY_NORMAL)
    due_at = models.DateTimeField(null=True, blank=True)
    comment = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_CREATED)
    planning_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Запрос ричтрака"
        verbose_name_plural = "Запросы ричтрака"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["context_type", "context_id"]),
            models.Index(fields=["status", "priority"]),
            models.Index(fields=["agency", "status"]),
        ]

    def __str__(self) -> str:
        return f"MoveRequest #{self.pk} ({self.status})"


class MoveRequestItem(models.Model):
    request = models.ForeignKey(
        MoveRequest,
        on_delete=models.CASCADE,
        related_name="items",
    )
    sku_code = models.CharField(max_length=64, blank=True)
    barcode = models.CharField(max_length=64, blank=True)
    goods_type = models.CharField(max_length=32, blank=True)
    qty_requested = models.PositiveIntegerField(default=0)
    qty_planned = models.PositiveIntegerField(default=0)
    qty_done = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Строка запроса ричтрака"
        verbose_name_plural = "Строки запросов ричтрака"
        indexes = [
            models.Index(fields=["request", "sku_code"]),
            models.Index(fields=["barcode"]),
        ]

    def __str__(self) -> str:
        key = self.sku_code or self.barcode or "-"
        return f"{key}: {self.qty_requested}"


class MoveTask(models.Model):
    MODE_PALLET_FULL = "pallet_full"
    MODE_BOX_FULL = "box_full"
    MODE_BOX_PARTIAL = "box_partial"
    MOVE_MODE_CHOICES = [
        (MODE_PALLET_FULL, "Паллета целиком"),
        (MODE_BOX_FULL, "Короба целиком"),
        (MODE_BOX_PARTIAL, "Частичный отбор"),
    ]

    STATUS_CREATED = "created"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_DONE = "done"
    STATUS_CANCELED = "canceled"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_CREATED, "Создано"),
        (STATUS_IN_PROGRESS, "В работе"),
        (STATUS_DONE, "Выполнено"),
        (STATUS_CANCELED, "Отменено"),
        (STATUS_FAILED, "Ошибка"),
    ]

    request = models.ForeignKey(
        MoveRequest,
        on_delete=models.CASCADE,
        related_name="tasks",
    )
    pallet_code = models.CharField(max_length=128)
    from_zone = models.CharField(max_length=16, blank=True)
    from_row = models.PositiveIntegerField(null=True, blank=True)
    from_section = models.PositiveIntegerField(null=True, blank=True)
    from_tier = models.PositiveIntegerField(null=True, blank=True)
    from_cell = models.PositiveIntegerField(null=True, blank=True)
    to_zone = models.CharField(max_length=16, blank=True)
    to_row = models.PositiveIntegerField(null=True, blank=True)
    to_section = models.PositiveIntegerField(null=True, blank=True)
    to_tier = models.PositiveIntegerField(null=True, blank=True)
    to_cell = models.PositiveIntegerField(null=True, blank=True)
    move_mode = models.CharField(max_length=16, choices=MOVE_MODE_CHOICES, default=MODE_PALLET_FULL)
    qty_planned = models.PositiveIntegerField(default=0)
    qty_done = models.PositiveIntegerField(default=0)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_CREATED)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reachtruck_tasks",
    )
    assigned_to_name = models.CharField(max_length=255, blank=True)
    legacy_order_id = models.CharField(max_length=64, blank=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Задание ричтрака"
        verbose_name_plural = "Задания ричтрака"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["request", "status"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["pallet_code", "status"]),
        ]

    def __str__(self) -> str:
        return f"MoveTask #{self.pk} ({self.pallet_code})"
