import re
from uuid import uuid4

from django.conf import settings
from django.db import models

from employees.models import Employee
from head_manager.models import Carrier


class LogisticsTrip(models.Model):
    STATUS_DRAFT = "draft"
    STATUS_PLANNED = "planned"
    STATUS_LOADING = "loading"
    STATUS_DEPARTED = "departed"
    STATUS_COMPLETED = "completed"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Черновик"),
        (STATUS_PLANNED, "Спланирован"),
        (STATUS_LOADING, "Погрузка"),
        (STATUS_DEPARTED, "В рейсе"),
        (STATUS_COMPLETED, "Завершен"),
        (STATUS_CANCELED, "Отменен"),
    ]

    VEHICLE_FULFILLMENT = "fulfillment"
    VEHICLE_CLIENT = "client"
    VEHICLE_HIRED = "hired"
    VEHICLE_OTHER = "other"
    VEHICLE_CHOICES = [
        (VEHICLE_FULFILLMENT, "Транспорт Fullbox"),
        (VEHICLE_CLIENT, "Транспорт клиента"),
        (VEHICLE_HIRED, "Наемный транспорт"),
        (VEHICLE_OTHER, "Другое"),
    ]

    number = models.CharField("Номер рейса", max_length=32, unique=True, db_index=True)
    trip_date = models.DateField("Дата рейса", null=True, blank=True)
    status = models.CharField("Статус", max_length=32, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    vehicle_type = models.CharField("Тип транспорта", max_length=32, choices=VEHICLE_CHOICES, blank=True, default="")
    vehicle_name = models.CharField("Транспорт", max_length=128, blank=True)
    vehicle_number = models.CharField("Номер авто", max_length=32, blank=True)
    driver_name = models.CharField("Водитель", max_length=255, blank=True)
    driver_phone = models.CharField("Телефон водителя", max_length=32, blank=True)
    route_comment = models.TextField("Комментарий по маршруту", blank=True)
    loading_comment = models.TextField("Комментарий по погрузке", blank=True)
    assigned_logistician = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="logistics_trips",
        verbose_name="Логист",
        limit_choices_to={"role": "logistician"},
    )
    carrier = models.ForeignKey(
        Carrier,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="logistics_trips",
        verbose_name="Перевозчик",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_logistics_trips",
        verbose_name="Создал",
    )
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлен", auto_now=True)

    class Meta:
        verbose_name = "Рейс логистики"
        verbose_name_plural = "Рейсы логистики"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "trip_date"])]

    def __str__(self) -> str:
        return self.number


class LogisticsTripOrder(models.Model):
    trip = models.ForeignKey(
        LogisticsTrip,
        on_delete=models.CASCADE,
        related_name="orders",
        verbose_name="Рейс",
    )
    shipping_order = models.ForeignKey(
        "shipping.ShippingOrder",
        on_delete=models.PROTECT,
        related_name="logistics_links",
        verbose_name="Заявка на отгрузку",
    )
    loading_sequence = models.PositiveIntegerField("Очередь погрузки", default=0)
    delivery_sequence = models.PositiveIntegerField("Очередь доставки", default=0)
    comment = models.CharField("Комментарий", max_length=255, blank=True)
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлен", auto_now=True)

    class Meta:
        verbose_name = "Заявка в рейсе"
        verbose_name_plural = "Заявки в рейсе"
        ordering = ["loading_sequence", "delivery_sequence", "id"]
        constraints = [
            models.UniqueConstraint(fields=["trip", "shipping_order"], name="uniq_logistics_trip_shipping_order")
        ]

    def __str__(self) -> str:
        return f"{self.trip.number} / {self.shipping_order.number}"


_TRIP_NUMBER_RE = re.compile(r"^TRIP-(\d+)$", re.IGNORECASE)
_TRIP_RS_RE = re.compile(r"^(\d+)_RS$", re.IGNORECASE)
_TRIP_DRAFT_RE = re.compile(r"^DRAFT-[0-9A-F]{12,}$", re.IGNORECASE)


def trip_number_sequence_value(value: str | None) -> int:
    raw = str(value or "").strip()
    if not raw:
        return 0
    match = _TRIP_NUMBER_RE.match(raw)
    if match:
        return int(match.group(1))
    match = _TRIP_RS_RE.match(raw)
    if match:
        return int(match.group(1))
    return 0


def is_draft_trip_number(value: str | None) -> bool:
    return bool(_TRIP_DRAFT_RE.match(str(value or "").strip()))


def display_trip_number(value: str | None) -> str:
    sequence = trip_number_sequence_value(value)
    if sequence > 0:
        return f"{sequence}_RS"
    return str(value or "").strip()


def next_draft_trip_number() -> str:
    return f"DRAFT-{uuid4().hex[:16].upper()}"


def next_trip_number() -> str:
    numbers = LogisticsTrip.objects.values_list("number", flat=True)
    max_number = 0
    for raw in numbers:
        max_number = max(max_number, trip_number_sequence_value(raw))
    return f"{max_number + 1}_RS"
