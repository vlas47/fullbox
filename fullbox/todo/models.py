import os
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from employees.models import Employee


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
