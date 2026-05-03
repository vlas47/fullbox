from __future__ import annotations

from datetime import time, timedelta

from django import forms
from django.utils import timezone

from sku.models import Agency
from sku.models import Market

from .models import ShippingOrder, ShippingOrderItem, ShippingTransportNote

NEXT_DAY_DEADLINE_HOUR = 11
NEXT_DAY_DEADLINE_ERROR = "Заявки на отгрузку на следующий день принимаются только до 11:00 текущего дня"
WORKDAY_START_HOUR = 8
WORKDAY_END_HOUR = 19
WORKDAY_HOURS_ERROR = "Плановое время отгрузки должно быть в рабочем окне с 08:00 до 19:00."


def _marketplace_key(market: Market | None) -> str:
    text = str(getattr(market, "name", "") or "").strip().lower()
    if text in {"ozon"}:
        return "ozon"
    if text in {"wb", "wildberries", "wildberries (wb)"}:
        return "wb"
    if text in {"yandex", "yandex market", "yandex.market", "яндекс", "яндекс маркет"}:
        return "yandex"
    if text in {"sber", "сбер", "сбермегамаркет", "sbermegamarket"}:
        return "sber"
    return ""


def parse_items_raw(raw_text: str) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    errors: list[str] = []
    lines = [line.strip() for line in str(raw_text or "").splitlines() if line.strip()]
    for index, line in enumerate(lines, start=1):
        parts = [part.strip() for part in line.split(";")]
        if len(parts) < 6:
            errors.append(
                f"Строка {index}: ожидается формат "
                "`артикул;наименование;размер;штрихкод;тип товара;кол-во`."
            )
            continue
        sku_code, name, size, barcode, goods_type, qty_text = parts[:6]
        if not sku_code:
            errors.append(f"Строка {index}: артикул обязателен.")
            continue
        if not name:
            errors.append(f"Строка {index}: наименование обязательно.")
            continue
        try:
            qty = int(qty_text)
        except ValueError:
            errors.append(f"Строка {index}: количество должно быть целым числом.")
            continue
        if qty <= 0:
            errors.append(f"Строка {index}: количество должно быть больше нуля.")
            continue
        rows.append(
            {
                "sku_code": sku_code,
                "name": name,
                "size": size,
                "barcode": barcode,
                "goods_type": goods_type,
                "qty_requested": qty,
            }
        )
    return rows, errors


class ShippingOrderForm(forms.ModelForm):
    wb_transit_warehouse = forms.BooleanField(required=False)

    class Meta:
        model = ShippingOrder
        fields = [
            "agency",
            "slot_date",
            "slot_time",
            "eta_at",
            "shipping_barcode",
            "marketplace",
            "wb_supply_barcode",
            "wb_transit_warehouse",
            "transit_address",
            "destination_warehouse",
            "supply_type",
            "vehicle_type",
            "vehicle_number",
            "driver_phone",
            "comment",
        ]
        widgets = {
            "slot_date": forms.DateInput(attrs={"type": "date", "id": "slot_date"}),
            "slot_time": forms.TimeInput(attrs={"type": "time", "id": "slot_time", "step": 300}, format="%H:%M"),
            "eta_at": forms.HiddenInput(attrs={"id": "eta_at"}),
            "shipping_barcode": forms.TextInput(attrs={"placeholder": "Введите ШК поставки"}),
            "wb_supply_barcode": forms.TextInput(attrs={"placeholder": "Введите номер поставки"}),
            "transit_address": forms.TextInput(attrs={"placeholder": "Введите транзитный адрес"}),
            "destination_warehouse": forms.TextInput(attrs={"placeholder": "Введите склад назначения"}),
            "vehicle_number": forms.TextInput(attrs={"placeholder": "A123BC77"}),
            "driver_phone": forms.TextInput(attrs={"inputmode": "tel", "placeholder": "+7 900 000-00-00"}),
            "comment": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(
        self,
        *args,
        agency_queryset=None,
        locked_agency: Agency | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        qs = agency_queryset if agency_queryset is not None else Agency.objects.filter(archived=False)
        self.fields["agency"].queryset = qs.order_by("agn_name")
        if locked_agency is not None:
            self.fields["agency"].initial = locked_agency
            self.fields["agency"].disabled = True
        self.fields["eta_at"].required = True
        self.fields["slot_date"].required = False
        self.fields["slot_time"].required = False
        self.fields["marketplace"].queryset = Market.objects.order_by("name")
        self.fields["marketplace"].required = True
        self.fields["shipping_barcode"].required = True
        self.fields["wb_supply_barcode"].required = True
        self.fields["transit_address"].required = False
        self.fields["destination_warehouse"].required = True
        self.fields["supply_type"].required = True
        self.fields["vehicle_type"].required = True
        self.fields["vehicle_number"].required = False
        self.fields["driver_phone"].required = False
        self.fields["comment"].required = False
        self.fields["destination_warehouse"].widget.attrs["list"] = "destination-warehouse-options"
        self.fields["transit_address"].widget.attrs["list"] = "transit-address-options"

    def clean(self):
        cleaned_data = super().clean()
        eta_at = cleaned_data.get("eta_at")
        cleaned_data["shipping_barcode"] = str(cleaned_data.get("shipping_barcode") or "").strip()
        cleaned_data["wb_supply_barcode"] = str(cleaned_data.get("wb_supply_barcode") or "").strip()
        cleaned_data["transit_address"] = str(cleaned_data.get("transit_address") or "").strip()
        cleaned_data["destination_warehouse"] = str(cleaned_data.get("destination_warehouse") or "").strip()
        slot_date = cleaned_data.get("slot_date")
        slot_time = cleaned_data.get("slot_time")
        vehicle_type = str(cleaned_data.get("vehicle_type") or "").strip()
        vehicle_number = str(cleaned_data.get("vehicle_number") or "").strip()
        driver_phone = str(cleaned_data.get("driver_phone") or "").strip()
        has_transit = bool(cleaned_data.get("wb_transit_warehouse"))
        marketplace = cleaned_data.get("marketplace")
        marketplace_key = _marketplace_key(marketplace)

        cleaned_data["vehicle_type"] = vehicle_type
        cleaned_data["vehicle_number"] = vehicle_number
        cleaned_data["driver_phone"] = driver_phone
        cleaned_data["wb_transit_warehouse"] = has_transit

        if not cleaned_data["shipping_barcode"]:
            self.add_error("shipping_barcode", "Укажите ШК поставки.")

        if marketplace is None:
            self.add_error("marketplace", "Выберите маркетплейс.")

        if not cleaned_data["wb_supply_barcode"]:
            self.add_error("wb_supply_barcode", "Укажите номер поставки.")

        if not cleaned_data["destination_warehouse"]:
            self.add_error("destination_warehouse", "Укажите склад назначения.")

        if not str(cleaned_data.get("supply_type") or "").strip():
            self.add_error("supply_type", "Выберите тип поставки.")

        if has_transit and not cleaned_data["transit_address"]:
            self.add_error("transit_address", "Укажите транзитный адрес.")
        if not has_transit:
            cleaned_data["transit_address"] = ""

        if not vehicle_type:
            self.add_error("vehicle_type", "Выберите транспорт.")

        if driver_phone:
            digits = "".join(ch for ch in driver_phone if ch.isdigit())
            if len(digits) != 11 or digits[0] not in {"7", "8"}:
                self.add_error("driver_phone", "Введите корректный телефон водителя.")

        if slot_time and not slot_date:
            self.add_error("slot_date", "Сначала укажите дату слота.")

        if marketplace_key == "ozon":
            if not slot_date:
                self.add_error("slot_date", "Для Ozon укажите дату слота.")
            if not slot_time:
                self.add_error("slot_time", "Для Ozon укажите время слота.")
        elif not slot_date:
            cleaned_data["slot_time"] = None

        if eta_at is None:
            self.add_error("eta_at", "Заполните плановую дату и время отгрузки.")
        else:
            if timezone.is_naive(eta_at):
                eta_at = timezone.make_aware(eta_at, timezone.get_current_timezone())
                cleaned_data["eta_at"] = eta_at
            now_local = timezone.localtime()
            eta_local = eta_at.astimezone(timezone.get_current_timezone())
            next_day = now_local.date() + timedelta(days=1)
            eta_date = eta_local.date()
            if eta_date < next_day:
                self.add_error("eta_at", "Плановая дата отгрузки должна быть не раньше следующего дня.")
            elif eta_date == next_day and now_local.time() >= time(hour=NEXT_DAY_DEADLINE_HOUR):
                self.add_error("eta_at", NEXT_DAY_DEADLINE_ERROR)
            eta_time = eta_local.time()
            if eta_time < time(hour=WORKDAY_START_HOUR) or eta_time > time(hour=WORKDAY_END_HOUR):
                self.add_error("eta_at", WORKDAY_HOURS_ERROR)
        return cleaned_data


class ShippingOrderItemForm(forms.ModelForm):
    class Meta:
        model = ShippingOrderItem
        fields = [
            "sku_code",
            "name",
            "size",
            "barcode",
            "goods_type",
            "qty_requested",
            "comment",
        ]

    def clean_qty_requested(self):
        qty = int(self.cleaned_data.get("qty_requested") or 0)
        if qty <= 0:
            raise forms.ValidationError("Количество должно быть больше нуля.")
        return qty


class ShippingTransportNoteForm(forms.ModelForm):
    class Meta:
        model = ShippingTransportNote
        fields = [
            "document_number",
            "document_date",
            "shipper_name",
            "shipper_inn",
            "shipper_address",
            "shipper_phone",
            "consignee_name",
            "consignee_inn",
            "consignee_address",
            "consignee_phone",
            "carrier_name",
            "carrier_inn",
            "carrier_address",
            "carrier_phone",
            "loading_address",
            "unloading_address",
            "cargo_name",
            "cargo_package_count",
            "cargo_package_type",
            "cargo_weight_kg",
            "cargo_declared_value",
            "accompanying_documents",
            "special_instructions",
            "transportation_conditions",
            "delivery_notes",
            "driver_name",
            "driver_phone",
            "vehicle_number",
            "trailer_number",
            "service_cost",
        ]
        widgets = {
            "document_date": forms.DateInput(attrs={"type": "date"}),
            "shipper_address": forms.Textarea(attrs={"rows": 3}),
            "consignee_address": forms.Textarea(attrs={"rows": 3}),
            "carrier_address": forms.Textarea(attrs={"rows": 3}),
            "loading_address": forms.Textarea(attrs={"rows": 3}),
            "unloading_address": forms.Textarea(attrs={"rows": 3}),
            "cargo_name": forms.Textarea(attrs={"rows": 4}),
            "accompanying_documents": forms.Textarea(attrs={"rows": 3}),
            "special_instructions": forms.Textarea(attrs={"rows": 3}),
            "transportation_conditions": forms.Textarea(attrs={"rows": 3}),
            "delivery_notes": forms.Textarea(attrs={"rows": 3}),
            "cargo_weight_kg": forms.NumberInput(attrs={"step": "0.001", "min": "0"}),
            "cargo_declared_value": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "service_cost": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        placeholders = {
            "document_number": "Например, ТН-0001",
            "shipper_name": "ООО Ромашка",
            "shipper_inn": "7700000000",
            "shipper_phone": "+7 900 000-00-00",
            "consignee_name": "Склад назначения / маркетплейс",
            "consignee_inn": "7700000000",
            "consignee_phone": "+7 900 000-00-00",
            "carrier_name": "FullBox",
            "carrier_inn": "7700000000",
            "carrier_phone": "+7 499 450-35-55",
            "cargo_package_type": "Короб / паллета",
            "driver_name": "ФИО водителя",
            "driver_phone": "+7 900 000-00-00",
            "vehicle_number": "A123BC77",
            "trailer_number": "При наличии",
        }
        for name, placeholder in placeholders.items():
            if name in self.fields:
                self.fields[name].widget.attrs.setdefault("placeholder", placeholder)
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.Textarea):
                field.widget.attrs.setdefault("placeholder", "Заполните поле")

    def clean(self):
        cleaned_data = super().clean()
        for name, value in list(cleaned_data.items()):
            if isinstance(value, str):
                cleaned_data[name] = value.strip()
        return cleaned_data
