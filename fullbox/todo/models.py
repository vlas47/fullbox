import os
import re
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from audit.models import OrderAuditEntry
from employees.models import Employee


_RECEIVING_ROUTE_RE = re.compile(r"/orders/receiving/([^/]+)/")
_STATUS_ONLY_KEYS = {"comment", "message", "status", "status_label", "submit_action"}


def _extract_receiving_order_id(route: str | None) -> str | None:
    if not route:
        return None
    match = _RECEIVING_ROUTE_RE.search(route)
    if not match:
        return None
    return match.group(1)


def _has_receiving_items(payload: dict) -> bool:
    items = payload.get("items") or []
    for item in items:
        for key in ("sku_code", "name", "qty", "size"):
            if str(item.get(key) or "").strip():
                return True
    return False


def _latest_payload_from_entries(entries) -> dict:
    for entry in reversed(entries):
        payload = entry.payload or {}
        if not payload:
            continue
        significant_keys = set(payload.keys()) - _STATUS_ONLY_KEYS
        if significant_keys:
            return payload
    return entries[-1].payload or {} if entries else {}


def _receiving_title_from_payload(payload: dict | None) -> str:
    title = "Заявка на приемку"
    if payload and not _has_receiving_items(payload):
        title = "Заявка на приемку без указания товара"
    return title


def _payload_for_receiving_order(order_id: str) -> dict:
    entries = list(
        OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving")
        .only("payload", "created_at")
        .order_by("created_at")
    )
    return _latest_payload_from_entries(entries)


def default_due_date():
    return timezone.now() + timedelta(days=1)


class Task(models.Model):
    STATUS_CHOICES = [
        ("backlog", "Просрочены"),
        ("in_progress", "Сегодня"),
        ("done", "Готово"),
        ("blocked", "Скоро"),
    ]

    PRIORITY_CHOICES = [
        ("low", "Низкий"),
        ("normal", "Средний"),
        ("high", "Высокий"),
        ("urgent", "Срочный"),
    ]

    title = models.CharField("Название задачи", max_length=255)
    description = models.TextField("Описание", blank=True)
    route = models.CharField("Маршрут", max_length=255, blank=True)
    assigned_to = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="tasks",
        verbose_name="Исполнитель",
    )
    observer = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="observed_tasks",
        verbose_name="Наблюдатель",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="created_tasks",
        verbose_name="Постановщик",
    )
    status = models.CharField("Статус", max_length=32, choices=STATUS_CHOICES, default="backlog")
    priority = models.CharField("Приоритет", max_length=16, choices=PRIORITY_CHOICES, default="normal")
    due_date = models.DateTimeField("Дедлайн", default=default_due_date)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "priority", "due_date", "-created_at"]
        verbose_name = "Задача"
        verbose_name_plural = "Задачи"

    def __str__(self) -> str:
        return self.title

    def display_title(self) -> str:
        cached = getattr(self, "_display_title_cache", None)
        if cached:
            return cached
        order_id = _extract_receiving_order_id(self.route)
        if order_id:
            payload = _payload_for_receiving_order(order_id)
            title = _receiving_title_from_payload(payload)
            self._display_title_cache = f"{title} №{order_id}"
            return self._display_title_cache
        self._display_title_cache = self.title
        return self._display_title_cache


class TaskComment(models.Model):
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="comments",
        verbose_name="Задача",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="task_comments",
        verbose_name="Автор",
    )
    body = models.TextField("Комментарий")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Комментарий"
        verbose_name_plural = "Комментарии"

    def __str__(self) -> str:
        return f"Комментарий #{self.pk}"


class TaskAttachment(models.Model):
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="attachments",
        verbose_name="Задача",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="task_attachments",
        verbose_name="Кто загрузил",
    )
    file = models.FileField("Файл", upload_to="task_files/")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]
        verbose_name = "Файл задачи"
        verbose_name_plural = "Файлы задач"

    def __str__(self) -> str:
        return f"Файл #{self.pk}"

    @property
    def filename(self) -> str:
        return os.path.basename(self.file.name)
