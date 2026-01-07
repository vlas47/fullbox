import json
import re
from datetime import timedelta

from django.db import models
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.generic import ListView, CreateView, UpdateView, TemplateView, FormView
import uuid

from employees.models import Employee
from employees.access import get_request_role, is_staff_role
from sku.models import Agency, SKU, SKUBarcode
from sku.views import SKUCreateView, SKUUpdateView, SKUDuplicateView
from todo.models import Task
from .forms import AgencyForm
from .services import fetch_party_by_inn
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
        ("pref", "Префикс"),
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
    labels = {"receiving": "ЗП"}
    return labels.get(order_type, order_type)


def _has_receiving_items(payload: dict) -> bool:
    items = payload.get("items") or []
    for item in items:
        for key in ("sku_code", "name", "qty", "size"):
            if str(item.get(key) or "").strip():
                return True
    return False


def _is_sent_to_manager(payload: dict) -> bool:
    status_value = (payload.get("status") or payload.get("submit_action") or "").lower()
    status_label = (payload.get("status_label") or "").lower()
    if status_value in {"sent_unconfirmed", "send", "submitted"}:
        return True
    return "подтверждени" in status_label


def _order_title_label(order_type: str, order_id: str, payload: dict | None = None) -> str:
    if order_type == "receiving":
        title = "Заявка на приемку"
        if payload and not _has_receiving_items(payload):
            title = "Заявка на приемку без указания товара"
        return f"{title} №{order_id}"
    if order_type == "packing":
        return f"Заявка на упаковку №{order_id}"
    return f"Заявка №{order_id}"


def _order_status_label(entry) -> str:
    payload = entry.payload or {}
    status_value = (payload.get("status") or payload.get("submit_action") or "").lower()
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
    )


def _is_draft_entry(entry) -> bool:
    payload = entry.payload or {}
    status_value = (payload.get("status") or payload.get("submit_action") or "").lower()
    status_label = (payload.get("status_label") or "").lower()
    return status_value == "draft" or "черновик" in status_label


def _order_detail_url(entry, client_id: int | None, client_view: bool) -> str:
    if client_view and entry.order_type == "receiving" and _is_draft_entry(entry) and client_id:
        return f"/orders/receiving/?client={client_id}&edit={entry.order_id}"
    suffix = f"?client={client_id}" if client_view and client_id else ""
    return f"/orders/{entry.order_type}/{entry.order_id}/{suffix}"


def _order_bucket(entry) -> str:
    payload = entry.payload or {}
    status_value = (payload.get("status") or payload.get("submit_action") or "").lower()
    status_label = (payload.get("status_label") or "").lower()
    if status_value == "draft" or "черновик" in status_label:
        return "client"
    if status_value in {"done", "completed", "closed", "finished"}:
        return "done"
    if status_value in {"warehouse", "on_warehouse"} or any(token in status_label for token in ("склад", "прием", "приём", "ожидании поставки")):
        return "warehouse"
    return "manager"


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


def dashboard(request):
    """Простой кабинет клиента с плейсхолдерами ключевых разделов."""
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    selected_client, client_view, allowed = _get_client_for_request(request)
    if not allowed:
        return HttpResponseForbidden("Доступ запрещен")
    orders_panel_columns = []
    orders_panel_stats = []
    orders_panel_total = 0
    client_messages = []
    if selected_client:
        base_entries = (
            OrderAuditEntry.objects.filter(agency=selected_client)
            .order_by("-created_at")
        )
        order_ids = list(base_entries.values_list("order_id", flat=True).distinct())
        raw_entries = (
            OrderAuditEntry.objects.filter(order_id__in=order_ids)
            .order_by("-created_at")
        )
        latest_by_order = {}
        status_by_order = {}
        for entry in raw_entries:
            if entry.order_id not in latest_by_order:
                latest_by_order[entry.order_id] = entry
            if entry.order_id not in status_by_order and _is_status_entry(entry):
                status_by_order[entry.order_id] = entry
        selected_entries = [
            status_by_order.get(order_id, latest_entry)
            for order_id, latest_entry in latest_by_order.items()
        ]
        selected_entries.sort(key=lambda item: item.created_at, reverse=True)
        if not client_view:
            selected_entries = [entry for entry in selected_entries if not _is_draft_entry(entry)]
        order_titles = {
            entry.order_id: _order_title_label(entry.order_type, entry.order_id, entry.payload or {})
            for entry in selected_entries
        }
        buckets = {
            "client": {"label": "У клиента", "orders": []},
            "manager": {"label": "У менеджера", "orders": []},
            "warehouse": {"label": "На складе", "orders": []},
            "done": {"label": "Выполнена", "orders": []},
        }
        act_cards = []
        visible_order_ids = {entry.order_id for entry in selected_entries}
        for entry in selected_entries:
            bucket = _order_bucket(entry)
            buckets[bucket]["orders"].append(
                {
                    "order_id": entry.order_id,
                    "order_type": entry.order_type,
                    "type_label": _order_type_label(entry.order_type),
                    "title": _order_title_label(entry.order_type, entry.order_id, entry.payload or {}),
                    "status_label": _order_status_label(entry),
                    "created_at": entry.created_at,
                    "detail_url": _order_detail_url(entry, selected_client.id if selected_client else None, client_view),
                }
            )
            payload = entry.payload or {}
            act_label = payload.get("act_sent")
            if act_label and entry.order_type == "receiving":
                act_viewed = bool(payload.get("act_viewed"))
                if not act_viewed:
                    act_cards.append(
                        {
                            "bucket": "client",
                            "order_id": entry.order_id,
                            "order_type": entry.order_type,
                            "type_label": "Акт",
                            "title": f"{act_label} по заявке №{entry.order_id}",
                            "status_label": "Акт отправлен клиенту",
                            "created_at": entry.created_at,
                            "detail_url": f"/orders/receiving/{entry.order_id}/act/?client={selected_client.id}",
                            "attention": True,
                        }
                    )
        for card in act_cards:
            buckets[card["bucket"]]["orders"].append(card)
        orders_panel_columns = [
            {
                "status": key,
                "label": value["label"],
                "orders": value["orders"],
                "count": len(value["orders"]),
            }
            for key, value in buckets.items()
        ]
        orders_panel_stats = [
            {"status": column["status"], "label": column["label"], "count": column["count"]}
            for column in orders_panel_columns
        ]
        orders_panel_total = sum(column["count"] for column in orders_panel_columns)
        message_qs = OrderAuditEntry.objects.filter(
            agency=selected_client,
            action="update",
        ).order_by("-created_at")
        if not client_view:
            message_qs = message_qs.filter(order_id__in=visible_order_ids)
        client_messages = [
            {
                "created_at": entry.created_at,
                "text": _format_message_text(entry.description) or "Исправление заявки",
                "title": order_titles.get(
                    entry.order_id,
                    _order_title_label(entry.order_type, entry.order_id, entry.payload or {}),
                ),
                "detail_url": f"/orders/{entry.order_type}/{entry.order_id}/?client={selected_client.id}",
            }
            for entry in message_qs[:30]
        ]
    else:
        orders_panel_columns = [
            {"status": "client", "label": "У клиента", "orders": [], "count": 0},
            {"status": "manager", "label": "У менеджера", "orders": [], "count": 0},
            {"status": "warehouse", "label": "На складе", "orders": [], "count": 0},
            {"status": "done", "label": "Выполнена", "orders": [], "count": 0},
        ]
        orders_panel_stats = [
            {"status": column["status"], "label": column["label"], "count": column["count"]}
            for column in orders_panel_columns
        ]
    return render(
        request,
        "client_cabinet/dashboard.html",
        {
            "selected_client": selected_client,
            "client_filter_param": f"?agency={selected_client.id}" if selected_client else "",
            "client_view": client_view,
            "orders_panel_columns": orders_panel_columns,
            "orders_panel_stats": orders_panel_stats,
            "orders_panel_total": orders_panel_total,
            "client_messages": client_messages,
        },
    )


def receiving_redirect(request, pk: int):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    agency = Agency.objects.filter(pk=pk).first()
    if not agency:
        return redirect("/client/")
    if not _check_agency_access(request, agency):
        return HttpResponseForbidden("Доступ запрещен")
    return redirect(f"/orders/receiving/?client={pk}")


def packing_redirect(request, pk: int):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    agency = Agency.objects.filter(pk=pk).first()
    if not agency:
        return redirect("/client/")
    if not _check_agency_access(request, agency):
        return HttpResponseForbidden("Доступ запрещен")
    return redirect(f"/orders/packing/?client={pk}")


class ClientListView(ListView):
    model = Agency
    paginate_by = 20
    template_name = "client_cabinet/clients_list.html"
    context_object_name = "items"
    view_modes = ("table", "cards")
    sort_fields = {
        "name": "agn_name",
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
        "inn": "inn",
        "pref": "pref",
        "email": "email",
        "phone": "phone",
    }
    default_sort = "name"

    def get_queryset(self):
        if not _staff_allowed(self.request):
            return Agency.objects.none()
        qs = super().get_queryset()
        archived_param = self.request.GET.get("archived")
        if archived_param == "1":
            qs = qs.filter(archived=True)
        elif archived_param == "0":
            qs = qs.filter(archived=False)

        search = (self.request.GET.get("q") or "").strip()
        if search:
            qs = qs.filter(
                models.Q(agn_name__icontains=search)
                | models.Q(inn__icontains=search)
                | models.Q(pref__icontains=search)
                | models.Q(email__icontains=search)
                | models.Q(phone__icontains=search)
            )

        filter_field = self.request.GET.get("filter_field")
        filter_value = (self.request.GET.get("filter_value") or "").strip()
        if filter_field in self.filter_fields and filter_value:
            lookup = self.filter_fields[filter_field]
            qs = qs.filter(**{f"{lookup}__icontains": filter_value})

        sort_key = self.request.GET.get("sort", self.default_sort)
        direction = self.request.GET.get("dir", "asc")
        sort_field = self.sort_fields.get(sort_key, self.sort_fields[self.default_sort])
        order_by = f"-{sort_field}" if direction == "desc" else sort_field
        return qs.order_by(order_by)

    def build_sort_url(self, field: str, direction: str) -> str:
        params = self.request.GET.copy()
        if "view" not in params:
            params["view"] = "table"
        params["sort"] = field
        params["dir"] = direction
        return f"?{params.urlencode()}"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        view = self.request.GET.get("view", "table")
        if view not in self.view_modes:
            view = "table"
        ctx["view_mode"] = view

        current_sort = self.request.GET.get("sort", self.default_sort)
        current_dir = "desc" if self.request.GET.get("dir") == "desc" else "asc"
        sort_info = {}
        for field in self.sort_fields:
            is_current = current_sort == field
            next_dir = "desc" if is_current and current_dir == "asc" else "asc"
            sort_info[field] = {
                "url": self.build_sort_url(field, next_dir),
                "active": is_current,
                "dir": current_dir if is_current else "",
                "next_dir": next_dir,
            }
        ctx["current_sort"] = current_sort
        ctx["current_dir"] = current_dir
        ctx["sort_info"] = sort_info
        ctx["filter_field"] = self.request.GET.get("filter_field") or ""
        ctx["filter_value"] = self.request.GET.get("filter_value") or ""
        ctx["archived_param"] = self.request.GET.get("archived") or ""
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
        staff_view = _staff_allowed(self.request)
        ctx["mode"] = getattr(self, "mode", "edit")
        ctx["title"] = getattr(self, "title", "Клиент")
        ctx["submit_label"] = getattr(self, "submit_label", "Сохранить")
        ctx["staff_view"] = staff_view
        ctx["cancel_url"] = "/client/" if staff_view else "/client/dashboard/"
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
    if not _staff_allowed(request):
        return HttpResponseForbidden("Доступ запрещен")
    agency = get_object_or_404(Agency, pk=pk)
    agency.archived = not agency.archived
    agency.save(update_fields=["archived"])
    action_label = "Архивирован клиент" if agency.archived else "Разархивирован клиент"
    log_agency_change(
        "update",
        agency,
        user=request.user if request.user.is_authenticated else None,
        description=f"{action_label}: {agency.agn_name or agency.inn or agency.id}",
        snapshot=agency_snapshot(agency),
    )
    next_url = request.GET.get("next") or reverse("client-list")
    return redirect(next_url)


def fetch_by_inn(request):
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "error": "Доступ запрещен"}, status=403)
    direct_client = Agency.objects.filter(portal_user=request.user).first()
    if not _staff_allowed(request) and not direct_client:
        return JsonResponse({"ok": False, "error": "Доступ запрещен"}, status=403)
    inn = (request.GET.get("inn") or "").strip()
    if not inn:
        return JsonResponse({"ok": False, "error": "ИНН обязателен"}, status=400)
    try:
        data = fetch_party_by_inn(inn)
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=502)
    if not data:
        return JsonResponse({"ok": False, "error": "Данные не найдены"}, status=404)
    return JsonResponse({"ok": True, "data": data})


class ClientSKUListView(ListView):
    model = SKU
    paginate_by = 20
    template_name = "client_cabinet/client_sku_list.html"
    context_object_name = "items"
    view_modes = ("cards", "table")

    def dispatch(self, request, *args, **kwargs):
        self.agency = Agency.objects.filter(pk=self.kwargs.get("pk")).first()
        if not self.agency:
            return redirect("/client/")
        if not _check_agency_access(request, self.agency):
            return HttpResponseForbidden("Доступ запрещен")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        qs = (
            SKU.objects.filter(deleted=False, agency=self.agency)
            .select_related("market", "agency")
            .prefetch_related("barcodes", "photos")
        )
        search = (self.request.GET.get("q") or "").strip()
        if search:
            qs = qs.filter(
                models.Q(sku_code__icontains=search)
                | models.Q(name__icontains=search)
                | models.Q(barcodes__value__icontains=search)
            ).distinct()
        filter_sku = (self.request.GET.get("filter_sku") or "").strip()
        if filter_sku:
            qs = qs.filter(sku_code__icontains=filter_sku)
        filter_name = (self.request.GET.get("filter_name") or "").strip()
        if filter_name:
            qs = qs.filter(name__icontains=filter_name)
        filter_brand = (self.request.GET.get("filter_brand") or "").strip()
        if filter_brand:
            escaped = re.escape(filter_brand)
            qs = qs.filter(brand__iregex=rf"^\s*{escaped}\s*$")
        filter_market = (self.request.GET.get("filter_market") or "").strip()
        if filter_market:
            escaped = re.escape(filter_market)
            qs = qs.filter(market__name__iregex=rf"^\s*{escaped}\s*$")
        filter_size = (self.request.GET.get("filter_size") or "").strip()
        if filter_size:
            match = re.fullmatch(r"\d+(?:[.,]0+)?", filter_size)
            if match:
                num = re.match(r"\d+", filter_size).group(0)
                size_pattern = rf"^\s*{re.escape(num)}(?:[.,]0+)?\s*$"
            else:
                size_pattern = rf"^\s*{re.escape(filter_size)}\s*$"
            qs = qs.filter(
                models.Q(size__iregex=size_pattern)
                | models.Q(barcodes__size__iregex=size_pattern)
            ).distinct()
        filter_barcode = (self.request.GET.get("filter_barcode") or "").strip()
        if filter_barcode:
            qs = qs.filter(barcodes__value__icontains=filter_barcode).distinct()
        filter_date = (self.request.GET.get("filter_date") or "").strip()
        if filter_date:
            parsed_date = None
            match = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", filter_date)
            if match:
                parsed_date = f"{match.group(3)}-{match.group(2)}-{match.group(1)}"
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", filter_date):
                parsed_date = filter_date
            if parsed_date:
                qs = qs.filter(updated_at__date=parsed_date)
        return qs.order_by("-updated_at")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        view = self.request.GET.get("view", "cards")
        if view not in self.view_modes:
            view = "cards"
        ctx["agency"] = self.agency
        ctx["search_value"] = self.request.GET.get("q", "")
        ctx["view_mode"] = view
        ctx["filter_values"] = {
            "sku": self.request.GET.get("filter_sku", ""),
            "name": self.request.GET.get("filter_name", ""),
            "brand": self.request.GET.get("filter_brand", ""),
            "market": self.request.GET.get("filter_market", ""),
            "size": self.request.GET.get("filter_size", ""),
            "barcode": self.request.GET.get("filter_barcode", ""),
            "date": self.request.GET.get("filter_date", ""),
        }
        ctx["client_view"] = True
        params = self.request.GET.copy()
        if "page" in params:
            params.pop("page")
        if "view" in params:
            params.pop("view")
        ctx["filter_query"] = params.urlencode()

        def normalize_option(value):
            if value is None:
                return None
            text = re.sub(r"\s+", " ", str(value).strip())
            return text or None

        def normalize_size(value):
            text = normalize_option(value)
            if not text:
                return None
            match = re.fullmatch(r"(\d+)(?:[.,]0+)?", text)
            if match:
                return match.group(1)
            return text

        brand_seen = {}
        for value in (
            SKU.objects.filter(agency=self.agency, deleted=False)
            .exclude(brand__isnull=True)
            .exclude(brand__exact="")
            .values_list("brand", flat=True)
            .distinct()
        ):
            normalized = normalize_option(value)
            if not normalized:
                continue
            key = normalized.lower()
            brand_seen.setdefault(key, normalized)
        ctx["brand_options"] = sorted(brand_seen.values(), key=lambda value: value.lower())

        market_seen = {}
        for value in (
            SKU.objects.filter(agency=self.agency, deleted=False)
            .exclude(market__name__isnull=True)
            .exclude(market__name__exact="")
            .values_list("market__name", flat=True)
            .distinct()
        ):
            normalized = normalize_option(value)
            if not normalized:
                continue
            key = normalized.lower()
            market_seen.setdefault(key, normalized)
        ctx["market_options"] = sorted(market_seen.values(), key=lambda value: value.lower())

        size_values = set()
        for value in (
            SKU.objects.filter(agency=self.agency, deleted=False)
            .exclude(size__isnull=True)
            .exclude(size__exact="")
            .values_list("size", flat=True)
        ):
            normalized = normalize_size(value)
            if normalized:
                size_values.add(normalized)
        for value in (
            SKUBarcode.objects.filter(sku__agency=self.agency, sku__deleted=False)
            .exclude(size__isnull=True)
            .exclude(size__exact="")
            .values_list("size", flat=True)
        ):
            normalized = normalize_size(value)
            if normalized:
                size_values.add(normalized)
        size_order = [
            "XXXS",
            "XXS",
            "XS",
            "S",
            "M",
            "L",
            "XL",
            "XXL",
            "XXXL",
            "XXXXL",
        ]

        def size_sort_key(value):
            text = str(value or "").strip()
            upper = text.upper()
            match = re.match(r"^(\d+(?:[.,]\d+)?)", upper)
            if match:
                return (0, float(match.group(1).replace(",", ".")), upper)
            if upper in size_order:
                return (1, size_order.index(upper), upper)
            return (2, upper)

        ctx["size_options"] = sorted(size_values, key=size_sort_key)
        for item in ctx.get("items", []):
            size_map = {}
            for barcode in item.barcodes.all():
                size_value = (barcode.size or "").strip()
                if not size_value:
                    continue
                size_map.setdefault(size_value, []).append(barcode.value)
            item.size_map_json = json.dumps(size_map, ensure_ascii=True)
        return ctx


class ClientSKUFormMixin:
    template_name = "client_cabinet/client_sku_form.html"

    def get_success_url(self):
        client_id = self.kwargs.get("pk")
        return f"/client/{client_id}/sku/"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["agency"] = getattr(self, "agency", None)
        ctx["client_view"] = True
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
        sku_id = self.kwargs.get("sku_id")
        orig = get_object_or_404(SKU, pk=sku_id, agency=self.agency)

        initial = {
            "name": orig.name,
            "brand": orig.brand,
            "agency": self.agency.id,
            "market": orig.market_id,
            "color": orig.color,
            "color_ref": orig.color_ref_id,
            "size": orig.size,
            "name_print": orig.name_print,
            "img": orig.img,
            "img_comment": orig.img_comment,
            "gender": orig.gender,
            "season": orig.season,
            "additional_name": orig.additional_name,
            "composition": orig.composition,
            "made_in": orig.made_in,
            "cr_product_date": orig.cr_product_date,
            "end_product_date": orig.end_product_date,
            "sign_akciz": orig.sign_akciz,
            "tovar_category": orig.tovar_category,
            "use_nds": orig.use_nds,
            "vid_tovar": orig.vid_tovar,
            "type_tovar": orig.type_tovar,
            "stor_unit": orig.stor_unit_id,
            "weight_kg": orig.weight_kg,
            "volume": orig.volume,
            "length_mm": orig.length_mm,
            "width_mm": orig.width_mm,
            "height_mm": orig.height_mm,
            "honest_sign": orig.honest_sign,
            "description": orig.description,
            "source": orig.source,
            "source_reference": None,
            "deleted": False,
        }
        base_code = f"{orig.sku_code}-copy"
        candidate = base_code
        counter = 1
        while SKU.objects.filter(sku_code=candidate).exists():
            candidate = f"{base_code}{counter}"
            counter += 1
        initial["sku_code"] = candidate
        return initial

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
        payload = {
            "email": request.POST.get("email"),
            "fio": request.POST.get("fio"),
            "org": request.POST.get("org"),
            "plan_date": request.POST.get("plan_date"),
            "marketplaces": request.POST.getlist("mp[]"),
            "mp_other": request.POST.get("mp_other"),
            "subject": request.POST.get("subject"),
            "total_qty": request.POST.get("total_qty"),
            "box_mode": request.POST.get("box_mode"),
            "box_mode_other": request.POST.get("box_mode_other"),
            "tasks": request.POST.getlist("tasks[]"),
            "tasks_other": request.POST.get("tasks_other"),
            "marking": request.POST.get("marking"),
            "marking_other": request.POST.get("marking_other"),
            "ship_as": request.POST.get("ship_as"),
            "ship_other": request.POST.get("ship_other"),
            "has_distribution": request.POST.get("has_distribution"),
            "comments": request.POST.get("comments"),
            "files_report": [f.name for f in request.FILES.getlist("files_report")],
            "files_distribution": [f.name for f in request.FILES.getlist("files_distribution")],
            "files_cz": [f.name for f in request.FILES.getlist("files_cz")],
        }
        order_id = f"pack-{uuid.uuid4().hex[:8]}"
        log_order_action(
            "create",
            order_id=order_id,
            order_type="packing",
            user=request.user if request.user.is_authenticated else None,
            agency=self.agency,
            description="Заявка на упаковку",
            payload=payload,
        )
        return self.get(request, submitted=True)

    def get(self, request, *args, **kwargs):
        submitted = kwargs.get("submitted") or request.GET.get("ok") == "1"
        ctx = self.get_context_data(submitted=submitted)
        return self.render_to_response(ctx)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["agency"] = self.agency
        ctx["submitted"] = kwargs.get("submitted", False)
        ctx["current_time"] = timezone.now()
        ctx["client_view"] = True
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
        # TODO: сохранить заявку; пока только отображаем успех и пишем в аудит
        sku_codes = request.POST.getlist("sku_code[]")
        sku_ids = request.POST.getlist("sku_id[]")
        names = request.POST.getlist("item_name[]")
        qtys = request.POST.getlist("qty[]")
        position_comments = request.POST.getlist("position_comment[]")
        items = []
        row_count = max(len(sku_codes), len(qtys), len(names), len(position_comments), len(sku_ids))
        for idx in range(row_count):
            sku_code = sku_codes[idx] if idx < len(sku_codes) else ""
            qty = qtys[idx] if idx < len(qtys) else ""
            if not sku_code and not qty:
                continue
            items.append(
                {
                    "sku_id": sku_ids[idx] if idx < len(sku_ids) else "",
                    "sku_code": sku_code,
                    "name": names[idx] if idx < len(names) else "",
                    "qty": qty,
                    "comment": position_comments[idx] if idx < len(position_comments) else "",
                }
            )
        submit_action = request.POST.get("submit_action")
        status_value = "draft" if submit_action == "draft" else "sent_unconfirmed"
        status_label = "Черновик" if status_value == "draft" else "Ждет подтверждения"
        payload = {
            "eta_at": request.POST.get("eta_at"),
            "expected_boxes": request.POST.get("expected_boxes"),
            "comment": request.POST.get("comment"),
            "submit_action": submit_action,
            "status": status_value,
            "status_label": status_label,
            "items": items,
            "documents": [f.name for f in request.FILES.getlist("documents")],
        }
        action_label = "черновик" if submit_action == "draft" else "заявка"
        order_id = f"rcv-{uuid.uuid4().hex[:8]}"
        log_order_action(
            "create",
            order_id=order_id,
            order_type="receiving",
            user=request.user if request.user.is_authenticated else None,
            agency=self.agency,
            description=f"Заявка на приемку ({action_label})",
            payload=payload,
        )
        if submit_action != "draft":
            _create_manager_task(order_id, self.agency, request, timezone.localtime())
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
        skus = (
            SKU.objects.filter(agency=self.agency, deleted=False)
            .prefetch_related("barcodes")
            .order_by("sku_code")
        )
        sku_options = []
        for sku in skus:
            barcodes = [barcode.value for barcode in sku.barcodes.all()]
            sku_options.append(
                {
                    "id": sku.id,
                    "code": sku.sku_code,
                    "name": sku.name,
                    "barcodes_joined": "|".join(barcodes),
                }
            )
        ctx["sku_options"] = sku_options
        ctx["current_time"] = timezone.localtime()
        ctx["min_past_hours"] = 0
        return ctx
