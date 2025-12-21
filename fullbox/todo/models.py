from django.db import models


class Task(models.Model):
    STATUS_CHOICES = [
        ("backlog", "Бэклог"),
        ("in_progress", "В работе"),
        ("done", "Готово"),
        ("blocked", "Заблокировано"),
    ]

    PRIORITY_CHOICES = [
        ("low", "Низкий"),
        ("normal", "Средний"),
        ("high", "Высокий"),
        ("urgent", "Срочный"),
    ]

    title = models.CharField("Название задачи", max_length=255)
    description = models.TextField("Описание", blank=True)
    status = models.CharField("Статус", max_length=32, choices=STATUS_CHOICES, default="backlog")
    priority = models.CharField("Приоритет", max_length=16, choices=PRIORITY_CHOICES, default="normal")
    due_date = models.DateTimeField("Дедлайн", blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "priority", "due_date", "-created_at"]
        verbose_name = "Задача"
        verbose_name_plural = "Задачи"

    def __str__(self) -> str:
        return self.title
