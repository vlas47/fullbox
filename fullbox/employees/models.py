from django.db import models


class Employee(models.Model):
    ROLE_CHOICES = [
        ('admin', 'Администратор'),
        ('director', 'Директор'),
        ('accountant', 'Бухгалтер'),
        ('head_manager', 'Главный менеджер'),
        ('manager', 'Менеджер'),
        ('storekeeper', 'Кладовщик'),
        ('picker', 'Сборщик'),
        ('developer', 'Разработчик'),
    ]

    full_name = models.CharField('ФИО', max_length=255)
    role = models.CharField('Роль', max_length=32, choices=ROLE_CHOICES)
    email = models.EmailField('Email', blank=True, null=True)
    phone = models.CharField('Телефон', max_length=32, blank=True, null=True)
    is_active = models.BooleanField('Активен', default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Сотрудник'
        verbose_name_plural = 'Сотрудники'
        ordering = ['full_name']

    def __str__(self):
        return f"{self.full_name} ({self.get_role_display()})"
