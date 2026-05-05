"""Client cabinet UI views and helper logic."""

import json
import re
from datetime import timedelta

from django.db import models
from django.db.models import Case, CharField, Count, F, Sum, Value, When
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.generic import ListView, CreateView, UpdateView, TemplateView, FormView
import uuid
from urllib.parse import urlencode

from employees.models import Employee
from employees.access import get_request_role, is_staff_role, resolve_cabinet_url
from fullbox.order_numbers import format_order_number
from marking.models import MarkingCode
from processing_app.views import _import_marking_codes
from sklad.models import WarehouseStockSnapshot
from sklad.services.warehouse_state import WarehouseGoodsStateResolver
from sklad.services.warehouse_transitions import WarehouseStateCode
from sku.models import Agency, SKU, SKUBarcode
from sku.views import SKUCreateView, SKUUpdateView, SKUDuplicateView
from todo.models import Task
from .forms import AgencyForm
from .services import (
    build_agency_form_context,
    build_dashboard_context,
    build_client_list_context,
    build_client_list_queryset,
    build_client_sku_duplicate_initial,
    build_client_sku_form_context,
    build_client_sku_list_context,
    build_client_sku_list_queryset,
    build_client_packing_form_context,
    build_client_receiving_form_context,
    build_marking_tools_context,
    fetch_party_by_inn,
    fetch_party_by_inn_response,
    import_marking_codes_response,
    resolve_client_order_redirect_response,
    submit_client_packing_order,
    submit_client_receiving_order,
    toggle_agency_archive_response,
)
from audit.models import OrderAuditEntry, agency_snapshot, log_agency_change, log_order_action


def _staff_allowed(request) -> bool:
    if not request.user.is_authenticated:
        return False
    role = get_request_role(request)
    return request.user.is_staff or is_staff_role(role)


def _get_client_for_request(request):
    if not request.user.is_authenticated:
        return None, False, False
    direct_client = Agency.objects.filter(portal_user=request.user).first()
    if direct_client:
        return direct_client, True, True
    staff_allowed = _staff_allowed(request)
    if not staff_allowed:
        return None, False, False
    client_id = request.GET.get("client") or request.GET.get("agency")
    if client_id:
        return Agency.objects.filter(pk=client_id).first(), False, True
    return None, False, True


def _check_agency_access(request, agency) -> bool:
    if not request.user.is_authenticated or not agency:
        return False
    direct_client = Agency.objects.filter(portal_user=request.user).first()
    if direct_client:
        return direct_client.id == agency.id
    return _staff_allowed(request)


def _manager_due_date(now):
    cutoff = now.replace(hour=15, minute=0, second=0, microsecond=0)
    if now < cutoff:
        return now
    return now + timedelta(days=1)


def _format_agency_value(value):
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    return str(value).strip() or "-"


def _describe_agency_changes(old_snapshot: dict, new_snapshot: dict) -> str:
    fields = [
        ("agn_name", "Название"),
        ("short_name", "Сокращенное название"),
        ("pref", "Префикс для кодов"),
        ("inn", "ИНН"),
        ("kpp", "КПП"),
        ("ogrn", "ОГРН"),
        ("phone", "Телефон"),
        ("email", "Email"),
        ("adres", "Юр. адрес"),
        ("fakt_adres", "Факт. адрес"),
        ("fio_agn", "Контактное лицо"),
        ("sign_oferta", "Оферта"),
        ("use_nds", "НДС"),
        ("contract_numb", "Номер договора"),
        ("contract_link", "Ссылка на договор"),
        ("archived", "Архив"),
    ]
    changes = []
    for key, label in fields:
        old_val = _format_agency_value(old_snapshot.get(key))
        new_val = _format_agency_value(new_snapshot.get(key))
        if old_val != new_val:
            changes.append(f"{label}: {old_val} -> {new_val}")
    if not changes:
        return "Изменений нет"
    return "Изменены реквизиты: " + "; ".join(changes)


def _create_manager_task(order_id, agency, request, submitted_at):
    if not agency:
        return
    manager = (
        Employee.objects.filter(role="manager", is_active=True)
        .order_by("full_name")
        .first()
    )
    if not manager:
        return
    description = f"Клиент: {agency.agn_name or agency.inn or agency.id}"
    Task.objects.create(
        title=f"Подтвердите заявку на приемку товара №{order_id}",
        description=description,
        route=f"/orders/receiving/{order_id}/",
        assigned_to=manager,
        created_by=request.user if request.user.is_authenticated else None,
        due_date=_manager_due_date(submitted_at),
    )


def _order_type_label(order_type: str) -> str:
    if not order_type:
        return "-"
    labels = {"receiving": "PR", "packing": "ЗУ", "processing": "OBR", "shipping": "OTG"}
    return labels.get(order_type, order_type)


def _display_order_number(order_type: str, order_id: str) -> str:
    return format_order_number(order_type, order_id)


def _has_receiving_items(payload: dict) -> bool:
    items = payload.get("items") or []
    for item in items:
        for key in ("sku_code", "name", "qty", "size"):
            if str(item.get(key) or "").strip():
                return True
    return False


def _entry_status_value(order_type: str | None, payload: dict | None) -> str:
    data = payload or {}
    if order_type == "shipping":
        return (data.get("shipping_state") or "").lower()
    return (data.get("status") or data.get("submit_action") or "").lower()


def _is_sent_to_manager(payload: dict) -> bool:
    status_value = _entry_status_value("receiving", payload)
    status_label = (payload.get("status_label") or "").lower()
    if status_value in {"sent_unconfirmed", "send", "submitted"}:
        return True
    return "подтверждени" in status_label


def _order_title_label(order_type: str, order_id: str, payload: dict | None = None) -> str:
    display_id = _display_order_number(order_type, order_id)
    if order_type == "receiving":
        title = "Заявка на приемку"
        if payload and not _has_receiving_items(payload):
            title = "Заявка на приемку без указания товара"
        return f"{title} №{display_id}"
    if order_type == "packing":
        return f"Заявка на упаковку №{display_id}"
    if order_type == "processing":
        if payload:
            status_value = (payload.get("status") or payload.get("submit_action") or "").lower()
            status_label = (payload.get("status_label") or "").lower()
            if status_value == "draft" or "черновик" in status_label:
                return "Черновик заявки на обработку"
        if str(order_id).startswith("draft-"):
            return "Черновик заявки на обработку"
        return f"Заявка на обработку №{display_id}"
    if order_type == "shipping":
        return f"Заявка на отгрузку №{display_id}"
    if order_type == "stock_move":
        return f"Складское задание №{display_id}"
    return f"Заявка №{display_id}"


def _shipping_trip_status(order_id: str | None) -> str:
    raw_number = str(order_id or "").strip()
    if not raw_number:
        return ""
    from logistics.models import LogisticsTripOrder

    trip_link = (
        LogisticsTripOrder.objects.select_related("trip")
        .filter(
            shipping_order__number=raw_number,
            trip__status__in={
                "draft",
                "planned",
                "loading",
                "departed",
                "completed",
            },
        )
        .order_by("-trip__updated_at", "-trip__created_at", "-id")
        .first()
    )
    return str(getattr(getattr(trip_link, "trip", None), "status", "") or "").strip()


def _order_status_label(entry) -> str:
    payload = entry.payload or {}
    status_value = _entry_status_value(entry.order_type, payload)
    if (payload.get("act_client_response") or "").lower() == "confirmed":
        return "Выполнена"
    if entry.order_type == "shipping":
        from . import views as client_views

        trip_status = client_views._shipping_trip_status(getattr(entry, "order_id", ""))
        if trip_status == "departed":
            return "Загружено в машину"
        if trip_status == "completed":
            return "Выполнена"
        shipping_labels = {
            "draft": "Черновик",
            "submitted": "На проверке у менеджера",
            "reserved": "Согласована и передана в работу кладовщику",
            "storekeeper_accepted": "Принята в работу складом",
            "picking": "Доставка в зону отгрузки",
            "packed": "Короба на новых паллетах",
            "shipped": "Выполнена",
            "partial_shipped": "Выполнена",
            "canceled": "Отменена",
        }
        if status_value in shipping_labels:
            return shipping_labels[status_value]
    if entry.order_type == "processing":
        return WarehouseGoodsStateResolver.resolve_for_processing_order(
            order_id=str(getattr(entry, "order_id", "") or "").strip(),
            agency=getattr(entry, "agency", None),
            payload=payload,
        ).label_for("client")
    if status_value == "draft":
        return "Черновик"
    if status_value in {"done", "completed", "closed", "finished"}:
        return "Выполнена"
    if payload.get("act_sent"):
        return "Выполнена"
    if payload.get("act") == "placement":
        state = (payload.get("act_state") or "closed").lower()
        return "Размещение на складе" if state == "open" else "Товар принят и размещен на складе"
    status_label = (payload.get("status_label") or "").lower()
    if "взята в работу" in status_label:
        return "Взята в работу"
    if "товар принят" in status_label:
        return "Товар принят и размещен на складе"
    if status_value in {"sent_unconfirmed", "send", "submitted"} or "подтверж" in status_label:
        return "Ждет подтверждения"
    if status_value in {"warehouse", "on_warehouse"} or "ожидании поставки" in status_label or "на складе" in status_label:
        return "В ожидании поставки товара"
    return payload.get("status_label") or payload.get("status") or "-"


def _is_status_entry(entry) -> bool:
    payload = entry.payload or {}
    if entry.action == "status":
        return True
    return bool(
        payload.get("status")
        or payload.get("status_label")
        or payload.get("submit_action")
        or payload.get("shipping_state")
    )


def _is_draft_entry(entry) -> bool:
    payload = entry.payload or {}
    status_value = _entry_status_value(entry.order_type, payload)
    status_label = (payload.get("status_label") or "").lower()
    return status_value == "draft" or "черновик" in status_label


def _order_detail_url(entry, client_id: int | None, client_view: bool) -> str:
    if entry.order_type == "shipping":
        from shipping.models import ShippingOrder

        shipping_order = (
            ShippingOrder.objects.filter(number=str(entry.order_id or "").strip())
            .only("id")
            .first()
        )
        suffix = f"?client={client_id}" if client_view and client_id else ""
        if shipping_order:
            return f"/shipping/{shipping_order.id}/{suffix}"
        return f"/shipping/{suffix}"
    if client_view and entry.order_type == "receiving" and _is_draft_entry(entry) and client_id:
        return f"/orders/receiving/?client={client_id}&edit={entry.order_id}"
    if client_view and entry.order_type == "processing" and _is_draft_entry(entry) and client_id:
        return f"/orders/processing/?client={client_id}&order={entry.order_id}&status=draft"
    suffix = f"?client={client_id}" if client_view and client_id else ""
    return f"/orders/{entry.order_type}/{entry.order_id}/{suffix}"


def _order_bucket(entry) -> str:
    payload = entry.payload or {}
    status_value = _entry_status_value(entry.order_type, payload)
    status_label = (payload.get("status_label") or "").lower()
    if entry.order_type == "shipping":
        from . import views as client_views

        trip_status = client_views._shipping_trip_status(getattr(entry, "order_id", ""))
        if trip_status in {"departed", "completed"}:
            return "done"
        if status_value == "draft":
            return "client"
        if status_value == "submitted":
            return "manager"
        if status_value in {"reserved", "storekeeper_accepted", "picking", "packed"}:
            return "warehouse"
        if status_value in {"shipped", "partial_shipped", "canceled"}:
            return "done"
        return "manager"
    if entry.order_type == "processing":
        processing_state = WarehouseGoodsStateResolver.resolve_for_processing_order(
            order_id=str(getattr(entry, "order_id", "") or "").strip(),
            agency=getattr(entry, "agency", None),
            payload=payload,
        )
        if status_value == "draft" or "черновик" in status_label:
            return "client"
        if processing_state.code in {
            WarehouseStateCode.RESERVED_FOR_PROCESSING,
            WarehouseStateCode.MOVING_TO_PROCESSING,
            WarehouseStateCode.IN_PROCESSING_ZONE,
            WarehouseStateCode.PROCESSING_IN_PROGRESS,
        }:
            return "warehouse"
        if processing_state.code in {
            WarehouseStateCode.PLACED_AFTER_PROCESSING,
            WarehouseStateCode.STORED,
        }:
            return "done"
        return "manager"
    if status_value == "draft" or "черновик" in status_label:
        return "client"
    if (
        (payload.get("act_client_response") or "").lower() == "confirmed"
        or payload.get("act_sent")
        or status_value in {"done", "completed", "closed", "finished"}
        or any(
        token in status_label for token in ("выполн", "заверш", "закрыт", "утвержден")
        )
    ):
        return "done"
    if payload.get("act") == "placement":
        act_state = (payload.get("act_state") or "closed").lower()
        return "warehouse" if act_state == "open" else "done"
    if status_value in {"processing_in_work"} or any(
        token in status_label for token in ("взята в работу", "в работе")
    ):
        return "warehouse"
    if status_value in {"warehouse", "on_warehouse"} or any(token in status_label for token in ("склад", "прием", "приём", "ожидании поставки")):
        return "warehouse"
    return "manager"


def _receiving_act_needs_client_attention(act_entry, status_entry) -> bool:
    act_payload = dict(act_entry.payload or {}) if act_entry and isinstance(act_entry.payload, dict) else {}
    status_payload = dict(status_entry.payload or {}) if status_entry and isinstance(status_entry.payload, dict) else {}
    if not act_payload.get("act_sent"):
        return False
    client_response = str(
        status_payload.get("act_client_response")
        or act_payload.get("act_client_response")
        or ""
    ).strip().lower()
    if client_response in {"confirmed", "dispute"}:
        return False
    if bool(status_payload.get("act_viewed") or act_payload.get("act_viewed")):
        return False
    return True


_ISO_DATETIME_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?\b")


def _format_message_text(text: str) -> str:
    if not text:
        return ""

    def _replace(match):
        raw = match.group(0)
        try:
            parsed = timezone.datetime.fromisoformat(raw)
        except ValueError:
            return raw
        return parsed.strftime("%d.%m.%Y, %H:%M")

    return _ISO_DATETIME_RE.sub(_replace, text)


def _verification_qty(value) -> int:
    try:
        return max(int(str(value).strip()), 0)
    except (TypeError, ValueError):
        return 0


def _received_total_for_agency(agency: Agency) -> tuple[int, int]:
    latest_receiving_payloads: dict[str, dict] = {}
    qs = (
        OrderAuditEntry.objects.filter(
            agency=agency,
            order_type="receiving",
            payload__act="receiving",
        )
        .only("order_id", "payload", "created_at")
        .order_by("-created_at")
    )
    for entry in qs:
        if entry.order_id in latest_receiving_payloads:
            continue
        latest_receiving_payloads[entry.order_id] = entry.payload or {}

    total = 0
    for payload in latest_receiving_payloads.values():
        for item in payload.get("act_items") or []:
            qty = _verification_qty(item.get("actual_qty"))
            if qty <= 0:
                qty = _verification_qty(item.get("qty"))
            total += qty
    return total, len(latest_receiving_payloads)


def _shipped_total_for_agency(agency: Agency) -> tuple[int, int]:
    from shipping.models import ShippingOrder, ShippingOrderItem

    shipped_statuses = [
        ShippingOrder.STATUS_SHIPPED,
        ShippingOrder.STATUS_PARTIAL,
    ]
    shipped_orders_qs = ShippingOrder.objects.filter(agency=agency, status__in=shipped_statuses)
    shipped_total = (
        ShippingOrderItem.objects.filter(order__in=shipped_orders_qs)
        .aggregate(total=Sum("qty_shipped"))
        .get("total")
        or 0
    )
    shipped_orders_count = shipped_orders_qs.count()
    return int(shipped_total), int(shipped_orders_count)


def _stock_total_for_agency(agency: Agency) -> int:
    stock_total = (
        WarehouseStockSnapshot.objects.filter(
            agency=agency,
            is_archived=False,
        )
        .exclude(warehouse_state_code=WarehouseStateCode.PROCESSING_IN_PROGRESS.value)
        .aggregate(total=Sum("qty"))
        .get("total")
        or 0
    )
    return int(stock_total)


def _processing_in_progress_total_for_agency(agency: Agency) -> int:
    total = (
        WarehouseStockSnapshot.objects.filter(
            agency=agency,
            warehouse_state_code=WarehouseStateCode.PROCESSING_IN_PROGRESS.value,
            is_archived=False,
        )
        .aggregate(total=Sum("processing_reserved_qty"))
        .get("total")
        or 0
    )
    if total:
        return int(total)
    fallback_total = (
        WarehouseStockSnapshot.objects.filter(
            agency=agency,
            warehouse_state_code=WarehouseStateCode.PROCESSING_IN_PROGRESS.value,
            is_archived=False,
        )
        .aggregate(total=Sum("qty"))
        .get("total")
        or 0
    )
    return int(fallback_total)


def _inventory_check_for_agency(agency: Agency | None) -> dict | None:
    if not agency:
        return None
    received_total, receiving_orders_count = _received_total_for_agency(agency)
    shipped_total, shipped_orders_count = _shipped_total_for_agency(agency)
    stock_total = _stock_total_for_agency(agency)
    processing_in_progress_total = _processing_in_progress_total_for_agency(agency)
    owned_total = int(stock_total) + int(processing_in_progress_total)
    expected_stock = int(received_total) - int(shipped_total)
    discrepancy = int(owned_total) - expected_stock
    is_ok = discrepancy == 0
    return {
        "received_total": int(received_total),
        "receiving_orders_count": int(receiving_orders_count),
        "shipped_total": int(shipped_total),
        "shipped_orders_count": int(shipped_orders_count),
        "expected_stock": int(expected_stock),
        "stock_total": int(stock_total),
        "processing_in_progress_total": int(processing_in_progress_total),
        "owned_total": int(owned_total),
        "discrepancy": int(discrepancy),
        "status_label": "Сходится" if is_ok else "Есть расхождение",
        "status_tone": "ok" if is_ok else "warn",
        "computed_at": timezone.localtime(),
    }


def dashboard(request):
    """Простой кабинет клиента с плейсхолдерами ключевых разделов."""
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    selected_client, client_view, allowed = _get_client_for_request(request)
    if not allowed:
        return HttpResponseForbidden("Доступ запрещен")
    run_inventory_check = request.GET.get("verify") == "1"
    context = build_dashboard_context(
        request=request,
        selected_client=selected_client,
        client_view=client_view,
        run_inventory_check=run_inventory_check,
    )
    return render(
        request,
        "client_cabinet/dashboard.html",
        context,
    )


def marking_tools(request):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    selected_client, _client_view, allowed = _get_client_for_request(request)
    if not allowed:
        return HttpResponseForbidden("Доступ запрещен")
    if not selected_client:
        return redirect("/client/")
    context = build_marking_tools_context(selected_client=selected_client)
    return render(
        request,
        "client_cabinet/marking_tools.html",
        context,
    )


def marking_import(request):
    if request.method != "POST":
        return HttpResponseForbidden("Доступ запрещен")
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    selected_client, _client_view, allowed = _get_client_for_request(request)
    if not allowed or not selected_client:
        return HttpResponseForbidden("Доступ запрещен")
    return import_marking_codes_response(request=request, selected_client=selected_client)


def receiving_redirect(request, pk: int):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    return resolve_client_order_redirect_response(request=request, pk=pk, destination="receiving")


def packing_redirect(request, pk: int):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    return resolve_client_order_redirect_response(request=request, pk=pk, destination="packing")


def shipping_redirect(request, pk: int):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    return resolve_client_order_redirect_response(request=request, pk=pk, destination="shipping")


class ClientListView(ListView):
    model = Agency
    paginate_by = 20
    template_name = "client_cabinet/clients_list.html"
    context_object_name = "items"
    view_modes = ("table", "cards")
    sort_fields = {
        "name": "agn_name",
        "short_name": "short_name",
        "pref": "pref",
        "inn": "inn",
        "email": "email",
        "phone": "phone",
        "use_nds": "use_nds",
        "sign_oferta": "sign_oferta",
        "id": "id",
    }
    filter_fields = {
        "agn_name": "agn_name",
        "short_name": "short_name",
        "inn": "inn",
        "pref": "pref",
        "email": "email",
        "phone": "phone",
    }
    default_sort = "name"

    def get_queryset(self):
        return build_client_list_queryset(
            request=self.request,
            base_queryset=super().get_queryset(),
            sort_fields=self.sort_fields,
            filter_fields=self.filter_fields,
            default_sort=self.default_sort,
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            build_client_list_context(
                request=self.request,
                items=ctx.get("items"),
                view_modes=self.view_modes,
                sort_fields=self.sort_fields,
                default_sort=self.default_sort,
            )
        )
        return ctx

    def dispatch(self, request, *args, **kwargs):
        if not _staff_allowed(request):
            return HttpResponseForbidden("Доступ запрещен")
        return super().dispatch(request, *args, **kwargs)


class AgencyFormMixin:
    model = Agency
    form_class = AgencyForm
    template_name = "client_cabinet/clients_form.html"
    success_url = "/client/"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            build_agency_form_context(
                request=self.request,
                mode=getattr(self, "mode", "edit"),
                title=getattr(self, "title", "Клиент"),
                submit_label=getattr(self, "submit_label", "Сохранить"),
            )
        )
        return ctx


class ClientCreateView(AgencyFormMixin, CreateView):
    mode = "create"
    title = "Создание клиента"
    submit_label = "Создать"

    def dispatch(self, request, *args, **kwargs):
        if not _staff_allowed(request):
            return HttpResponseForbidden("Доступ запрещен")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        log_agency_change(
            "create",
            self.object,
            user=self.request.user if self.request.user.is_authenticated else None,
            description=f"Создан клиент: {self.object.agn_name or self.object.inn or self.object.id}",
            snapshot=agency_snapshot(self.object),
        )
        return response


class ClientUpdateView(AgencyFormMixin, UpdateView):
    mode = "edit"
    title = "Редактирование клиента"
    submit_label = "Сохранить"

    def dispatch(self, request, *args, **kwargs):
        agency = Agency.objects.filter(pk=kwargs.get("pk")).first()
        if not agency:
            return redirect("/client/")
        if not _check_agency_access(request, agency):
            return HttpResponseForbidden("Доступ запрещен")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        old_snapshot = agency_snapshot(self.get_object())
        response = super().form_valid(form)
        new_snapshot = agency_snapshot(self.object)
        description = _describe_agency_changes(old_snapshot, new_snapshot)
        log_agency_change(
            "update",
            self.object,
            user=self.request.user if self.request.user.is_authenticated else None,
            description=description,
            snapshot=new_snapshot,
        )
        return response

    def get_success_url(self):
        if _staff_allowed(self.request):
            return super().get_success_url()
        return "/client/dashboard/"


def archive_toggle(request, pk: int):
    return toggle_agency_archive_response(request=request, pk=pk)


def fetch_by_inn(request):
    return fetch_party_by_inn_response(request=request)


class ClientSKUListView(ListView):
    model = SKU
    paginate_by = 20
    template_name = "client_cabinet/client_sku_list.html"
    context_object_name = "items"
    view_modes = ("table", "cards")

    def dispatch(self, request, *args, **kwargs):
        self.agency = Agency.objects.filter(pk=self.kwargs.get("pk")).first()
        if not self.agency:
            return redirect("/client/")
        if not _check_agency_access(request, self.agency):
            return HttpResponseForbidden("Доступ запрещен")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return build_client_sku_list_queryset(request=self.request, agency=self.agency)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            build_client_sku_list_context(
                request=self.request,
                agency=self.agency,
                items=ctx.get("items"),
                view_modes=self.view_modes,
            )
        )
        return ctx


class ClientSKUFormMixin:
    template_name = "client_cabinet/client_sku_form.html"

    def get_success_url(self):
        client_id = self.kwargs.get("pk")
        return f"/client/{client_id}/sku/"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(build_client_sku_form_context(agency=getattr(self, "agency", None)))
        return ctx


class ClientSKUCreateView(ClientSKUFormMixin, SKUCreateView):
    def dispatch(self, request, *args, **kwargs):
        self.agency = Agency.objects.filter(pk=self.kwargs.get("pk")).first()
        if not self.agency:
            return redirect("/client/")
        if not _check_agency_access(request, self.agency):
            return HttpResponseForbidden("Доступ запрещен")
        return super().dispatch(request, *args, **kwargs)

    def get_initial(self):
        initial = super().get_initial()
        initial["agency"] = self.agency.id
        return initial

    def form_valid(self, form):
        form.instance.agency = self.agency
        return super().form_valid(form)


class ClientSKUUpdateView(ClientSKUFormMixin, SKUUpdateView):
    pk_url_kwarg = "sku_id"

    def dispatch(self, request, *args, **kwargs):
        self.agency = Agency.objects.filter(pk=self.kwargs.get("pk")).first()
        if not self.agency:
            return redirect("/client/")
        if not _check_agency_access(request, self.agency):
            return HttpResponseForbidden("Доступ запрещен")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        qs = super().get_queryset()
        return qs.filter(agency=self.agency)


class ClientSKUDuplicateView(ClientSKUFormMixin, SKUDuplicateView):
    pk_url_kwarg = "sku_id"

    def dispatch(self, request, *args, **kwargs):
        self.agency = Agency.objects.filter(pk=self.kwargs.get("pk")).first()
        if not self.agency:
            return redirect("/client/")
        if not _check_agency_access(request, self.agency):
            return HttpResponseForbidden("Доступ запрещен")
        return super().dispatch(request, *args, **kwargs)

    def get_initial(self):
        return build_client_sku_duplicate_initial(
            agency=self.agency,
            sku_id=self.kwargs.get("sku_id"),
        )

    def get_queryset(self):
        qs = super().get_queryset()
        return qs.filter(agency=self.agency)


class ClientOrderFormView(TemplateView):
    template_name = "client_cabinet/client_order_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.agency = Agency.objects.filter(pk=self.kwargs.get("pk")).first()
        if not self.agency:
            return redirect("/client/")
        if not _check_agency_access(request, self.agency):
            return HttpResponseForbidden("Доступ запрещен")
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        # Пока без сохранения: имитация отправки.
        return self.get(request, submitted=True)

    def get(self, request, *args, **kwargs):
        submitted = kwargs.get("submitted") or request.GET.get("ok") == "1"
        ctx = self.get_context_data(submitted=submitted)
        return self.render_to_response(ctx)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["agency"] = self.agency
        ctx["submitted"] = kwargs.get("submitted", False)
        ctx["client_view"] = True
        return ctx


class ClientPackingCreateView(TemplateView):
    template_name = "client_cabinet/client_packing_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.agency = Agency.objects.filter(pk=self.kwargs.get("pk")).first()
        if not self.agency:
            return redirect("/client/")
        if not _check_agency_access(request, self.agency):
            return HttpResponseForbidden("Доступ запрещен")
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        submit_client_packing_order(request=request, agency=self.agency)
        return self.get(request, submitted=True)

    def get(self, request, *args, **kwargs):
        submitted = kwargs.get("submitted") or request.GET.get("ok") == "1"
        ctx = self.get_context_data(submitted=submitted)
        return self.render_to_response(ctx)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            build_client_packing_form_context(
                agency=self.agency,
                submitted=kwargs.get("submitted", False),
            )
        )
        return ctx


class ClientReceivingCreateView(TemplateView):
    template_name = "client_cabinet/client_receiving_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.agency = Agency.objects.filter(pk=self.kwargs.get("pk")).first()
        if not self.agency:
            return redirect("/client/")
        if not _check_agency_access(request, self.agency):
            return HttpResponseForbidden("Доступ запрещен")
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        result = submit_client_receiving_order(request=request, agency=self.agency)
        if result["redirect_to_dashboard"]:
            return redirect(f"/client/dashboard/?client={self.agency.id}")
        return self.get(request, submitted=True)

    def get(self, request, *args, **kwargs):
        submitted = kwargs.get("submitted") or request.GET.get("ok") == "1"
        ctx = self.get_context_data(submitted=submitted)
        return self.render_to_response(ctx)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            build_client_receiving_form_context(
                agency=self.agency,
                submitted=kwargs.get("submitted", False),
            )
        )
        return ctx
