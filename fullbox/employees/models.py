from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import models

from PIL import Image


class Employee(models.Model):
    ROLE_CHOICES = [
        ('admin', 'Администратор'),
        ('director', 'Директор'),
        ('accountant', 'Бухгалтер'),
        ('head_manager', 'Главный менеджер'),
        ('processing_head', 'Руководитель участка обработки'),
        ('manager', 'Менеджер'),
        ('storekeeper', 'Кладовщик'),
        ('picker', 'Сборщик'),
        ('developer', 'Разработчик'),
    ]

    full_name = models.CharField('ФИО', max_length=255)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="employee_profile",
        verbose_name="Пользователь",
    )
    role = models.CharField('Роль', max_length=32, choices=ROLE_CHOICES)
    email = models.EmailField('Email', blank=True, null=True)
    phone = models.CharField('Телефон', max_length=32, blank=True, null=True)
    facsimile = models.ImageField('Факсимиле', upload_to='facsimiles/', blank=True, null=True)
    is_active = models.BooleanField('Активен', default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Сотрудник'
        verbose_name_plural = 'Сотрудники'
        ordering = ['full_name']

    def __str__(self):
        return f"{self.full_name} ({self.get_role_display()})"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._original_facsimile_name = self.facsimile.name if self.facsimile else ""

    def save(self, *args, **kwargs):
        facsimile_changed = bool(self.facsimile) and (
            not self.pk
            or not getattr(self.facsimile, "_committed", True)
            or self.facsimile.name != self._original_facsimile_name
        )
        if facsimile_changed:
            normalized = self._normalize_facsimile(self.facsimile)
            base_name = Path(self.facsimile.name).stem or "facsimile"
            self.facsimile.save(f"{base_name}.png", normalized, save=False)
        super().save(*args, **kwargs)
        self._original_facsimile_name = self.facsimile.name if self.facsimile else ""

    @staticmethod
    def _normalize_facsimile(facsimile_file):
        target_size = (320, 120)
        resample = getattr(Image, "Resampling", Image).LANCZOS

        facsimile_file.seek(0)
        image = Image.open(facsimile_file)
        if image.format != "PNG":
            raise ValueError("Факсимиле должно быть в формате PNG.")

        image = image.convert("RGBA")
        image.thumbnail(target_size, resample)

        canvas = Image.new("RGBA", target_size, (255, 255, 255, 0))
        offset = (
            (target_size[0] - image.width) // 2,
            (target_size[1] - image.height) // 2,
        )
        canvas.paste(image, offset, image)

        out = BytesIO()
        canvas.save(out, format="PNG")
        return ContentFile(out.getvalue())
