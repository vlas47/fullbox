from django.db import models
from django.conf import settings


class AgentContext(models.Model):
    agent_id = models.CharField("ID агента", max_length=64, db_index=True)
    context_id = models.CharField("Контекст", max_length=64, unique=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="agent_contexts",
    )
    role = models.CharField("Роль", max_length=32, blank=True)
    order_id = models.IntegerField("Заявка", blank=True, null=True, db_index=True)
    box_id = models.CharField("Короб", max_length=64, blank=True)
    session_key = models.CharField("Сессия", max_length=64, blank=True)
    active = models.BooleanField("Активен", default=True)
    last_seen = models.DateTimeField("Последняя активность", blank=True, null=True)
    expires_at = models.DateTimeField("Истекает", blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Контекст агента"
        verbose_name_plural = "Контексты агента"
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return f"{self.agent_id} ({self.context_id})"


class DeviceAgent(models.Model):
    agent_id = models.CharField("ID агента", max_length=64, unique=True)
    name = models.CharField("Имя", max_length=128, blank=True)
    host = models.CharField("Хост", max_length=128, blank=True)
    version = models.CharField("Версия", max_length=32, blank=True)
    last_ip = models.GenericIPAddressField("IP", blank=True, null=True)
    last_seen = models.DateTimeField("Последняя активность", blank=True, null=True)
    meta = models.JSONField("Метаданные", default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Агент"
        verbose_name_plural = "Агенты"
        ordering = ["-last_seen", "-updated_at"]

    def __str__(self) -> str:
        return self.name or self.host or self.agent_id


class AgentCommand(models.Model):
    STATUS_PENDING = "pending"
    STATUS_DELIVERED = "delivered"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Ожидает"),
        (STATUS_DELIVERED, "Доставлено"),
        (STATUS_DONE, "Выполнено"),
        (STATUS_FAILED, "Ошибка"),
    ]

    agent_id = models.CharField("ID агента", max_length=64, blank=True, db_index=True)
    command = models.CharField("Команда", max_length=64)
    payload = models.JSONField("Параметры", default=dict, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    delivered_at = models.DateTimeField("Доставлено", blank=True, null=True)
    acked_at = models.DateTimeField("Подтверждено", blank=True, null=True)
    result = models.JSONField("Результат", default=dict, blank=True)
    error = models.TextField("Ошибка", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Команда агента"
        verbose_name_plural = "Команды агента"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.command} [{self.status}]"


class AgentEvent(models.Model):
    EVENT_SCAN = "scan"
    EVENT_PRINT = "print"
    EVENT_STATUS = "status"
    EVENT_ERROR = "error"
    EVENT_CHOICES = [
        (EVENT_SCAN, "Скан"),
        (EVENT_PRINT, "Печать"),
        (EVENT_STATUS, "Статус"),
        (EVENT_ERROR, "Ошибка"),
    ]

    agent_id = models.CharField("ID агента", max_length=64, db_index=True)
    context_id = models.CharField("Контекст", max_length=64, blank=True, db_index=True)
    context_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="agent_events",
    )
    context_role = models.CharField("Роль", max_length=32, blank=True)
    context_order_id = models.IntegerField("Заявка", blank=True, null=True, db_index=True)
    context_box_id = models.CharField("Короб", max_length=64, blank=True)
    event_type = models.CharField("Тип события", max_length=16, choices=EVENT_CHOICES)
    payload = models.JSONField("Данные", default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Событие агента"
        verbose_name_plural = "События агента"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.event_type} ({self.agent_id})"
