import json
import re
from datetime import datetime, time, timedelta

from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.utils import timezone
from django.views.generic import TemplateView

from audit.models import OrderAuditEntry, log_order_action
from employees.models import Employee
from employees.access import RoleRequiredMixin, get_request_role, resolve_cabinet_url
from sku.models import Agency, SKU
from todo.models import Task


_IP_PREFIX_RE = re.compile(r"\bиндивидуальный предприниматель\b", re.IGNORECASE)


def _shorten_ip_name(name: str) -> str:
    if not name:
        return "-"
    normalized = _IP_PREFIX_RE.sub("ИП", name)
    return " ".join(normalized.split())


def _short_name(full_name: str) -> str:
    if not full_name:
        return "-"
    parts = [part for part in full_name.split() if part]
    if not parts:
        return "-"
    surname = parts[0]
    initials = "".join(f"{part[0].upper()}." for part in parts[1:3] if part)
    return f"{surname} {initials}".strip()


def _order_type_label(order_type: str) -> str:
    if not order_type:
        return "-"
    labels = {"receiving": "ЗП"}
    return labels.get(order_type, order_type)


def _order_title_label(order_type: str) -> str:
    if order_type == "receiving":
        return "Заявка на приемку"
    if order_type == "packing":
        return "Заявка на упаковку"
    return "Заявка"


def _journal_status_label(entry):
    payload = entry.payload or {}
    status = payload.get("status") or payload.get("submit_action")
    if status == "draft":
        return "Черновик"
    if status in {"sent_unconfirmed", "send", "submitted"}:
        return "На подтверждении"
    return _status_label_from_entry(entry)


def _is_status_entry(entry) -> bool:
    payload = entry.payload or {}
    if entry.action == "status":
        return True
    return bool(
        payload.get("status")
        or payload.get("status_label")
        or payload.get("submit_action")
    )


def _manager_label() -> str:
    manager = (
        Employee.objects.filter(role="manager", is_active=True)
        .order_by("full_name")
        .first()
    )
    return _short_name(manager.full_name) if manager else "-"


def _storekeeper_label() -> str:
    storekeeper = (
        Employee.objects.filter(role="storekeeper", is_active=True)
        .order_by("full_name")
        .first()
    )
    return _short_name(storekeeper.full_name) if storekeeper else "-"


def _manager_full_name() -> str:
    manager = (
        Employee.objects.filter(role="manager", is_active=True)
        .order_by("full_name")
        .first()
    )
    return manager.full_name.strip() if manager and manager.full_name else ""


def _journal_action_label(entry, manager_label: str, storekeeper_label: str) -> str:
    payload = entry.payload or {}
    status_value = (payload.get("status") or payload.get("submit_action") or "").lower()
    status_label = (payload.get("status_label") or "").lower()
    if status_value == "draft" or "черновик" in status_label:
        return "У клиента"
    if status_value in {"warehouse", "storekeeper"} or "склад" in status_label:
        if storekeeper_label and storekeeper_label != "-":
            return f"У кладовщика {storekeeper_label}"
        return "У кладовщика"
    if manager_label and manager_label != "-":
        return f"В работе у менеджера {manager_label}"
    return "В работе у менеджера"


def _status_label_from_entry(entry):
    payload = entry.payload or {}
    return payload.get("status_label") or payload.get("status") or "-"


def _current_status_entry(entries):
    for entry in reversed(entries):
        if _is_status_entry(entry):
            return entry
    return entries[-1] if entries else None


def _current_responsible_label(entry):
    if not entry:
        return "-"
    payload = entry.payload or {}
    status_value = (payload.get("status") or payload.get("submit_action") or "").lower()
    status_label = (payload.get("status_label") or "").lower()
    agency = entry.agency
    if status_value == "draft" or "черновик" in status_label:
        base = (agency.fio_agn or agency.agn_name) if agency else None
        base = _shorten_ip_name(base) if base else None
        return f"Клиент {base}" if base else "Клиент"
    if status_value in {"done", "completed", "closed", "finished"} or "выполн" in status_label:
        return "Выполнено"
    if any(token in status_label for token in ("склад", "прием", "приём")):
        return "Склад"
    manager = _manager_full_name()
    return f"Менеджер {manager}" if manager else "Менеджер"


def _actor_label(user=None, agency=None, client_view=False):
    if user:
        base = user.get_full_name() or user.get_username() or str(user)
        base = _shorten_ip_name(base)
        if getattr(user, "is_staff", False):
            return base
        return f"Клиент {base}"
    if client_view and agency:
        base = agency.fio_agn or agency.agn_name or "клиент"
        base = _shorten_ip_name(base)
        return f"Клиент {base}"
    if agency:
        base = agency.fio_agn or agency.agn_name or "клиент"
        base = _shorten_ip_name(base)
        return f"Клиент {base}"
    return "-"


def _history_actor_label(entry, client_view=False, client_label: str | None = None):
    if not entry:
        return "-"
    if entry.action == "create":
        if entry.agency:
            base = entry.agency.fio_agn or entry.agency.agn_name or "клиент"
            base = _shorten_ip_name(base)
            return f"Клиент {base}"
        if client_label and client_label != "-":
            return f"Клиент {client_label}"
        if client_view:
            return "Клиент"
    if entry.action == "update":
        if entry.user:
            base = entry.user.get_full_name() or entry.user.get_username() or str(entry.user)
            base = base.strip()
            if base:
                lowered = base.lower()
                if lowered in {"manager", "менеджер"}:
                    manager_full = _manager_full_name()
                    if manager_full:
                        return f"Менеджер {manager_full}"
                if len(base.split()) >= 2:
                    return f"Менеджер {base}"
                short = _short_name(base)
                if short and short.lower() not in {"manager", "менеджер"}:
                    return f"Менеджер {short}"
        manager_full = _manager_full_name()
        return f"Менеджер {manager_full}" if manager_full else "Менеджер"
    return _actor_label(entry.user, entry.agency, client_view=client_view)


def _history_action_label(entry):
    payload = entry.payload or {}
    act = (payload.get("act") or "").lower()
    if entry.action == "status" and act == "receiving":
        return payload.get("act_label") or "Акт приемки"
    if entry.action == "status" and act == "placement":
        return payload.get("act_label") or "Акт размещения"
    return entry.get_action_display()


def _parse_qty_value(raw: str | None) -> int | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _item_key(sku: str | None, name: str | None, size: str | None) -> str:
    sku_part = (sku or "").strip().lower()
    name_part = (name or "").strip().lower()
    size_part = (size or "").strip().lower()
    return f"{sku_part}|{name_part}|{size_part}"


def _warehouse_status_from_entry(entry) -> bool:
    if not entry:
        return False
    payload = entry.payload or {}
    status_value = (payload.get("status") or payload.get("submit_action") or "").lower()
    status_label = (payload.get("status_label") or "").lower()
    return status_value in {"warehouse", "on_warehouse"} or "склад" in status_label


def _act_entry_from_entries(entries, act_type: str = "receiving"):
    for entry in reversed(entries or []):
        if (entry.payload or {}).get("act") == act_type:
            return entry
    return None


def _find_act_entry(entries, act_type: str, label_hint: str):
    entry = _act_entry_from_entries(entries, act_type)
    if entry:
        return entry
    for candidate in reversed(entries or []):
        label = ((candidate.payload or {}).get("act_label") or "").lower()
        if label_hint in label:
            return candidate
    return None


def _is_done_status(entry) -> bool:
    if not entry:
        return False
    payload = entry.payload or {}
    status_value = (payload.get("status") or payload.get("submit_action") or "").lower()
    status_label = (payload.get("status_label") or "").lower()
    if status_value in {"done", "completed", "closed", "finished"}:
        return True
    return "выполн" in status_label


def _send_act_to_client(order_id, entries, user) -> bool:
    if not entries:
        return False
    receiving_entry = _find_act_entry(entries, "receiving", "акт приемки")
    placement_entry = _find_act_entry(entries, "placement", "акт размещения")
    if not receiving_entry or not placement_entry:
        return False
    status_entry = _current_status_entry(entries)
    if _is_done_status(status_entry):
        return False
    latest = entries[-1]
    payload = dict(status_entry.payload or {}) if status_entry else {}
    payload["status"] = "done"
    payload["status_label"] = "Выполнена"
    act_label = (receiving_entry.payload or {}).get("act_label") or "Акт приемки"
    payload["act_sent"] = act_label
    log_order_action(
        "status",
        order_id=order_id,
        order_type="receiving",
        user=user if getattr(user, "is_authenticated", False) else None,
        agency=latest.agency,
        description="Акт отправлен клиенту",
        payload=payload,
    )
    log_order_action(
        "update",
        order_id=order_id,
        order_type="receiving",
        user=user if getattr(user, "is_authenticated", False) else None,
        agency=latest.agency,
        description=f"{act_label} отправлен клиенту",
        payload={"message": act_label},
    )
    Task.objects.filter(
        route=f"/orders/receiving/{order_id}/",
        assigned_to__role="manager",
    ).exclude(status="done").update(status="done")
    return True


def _manager_due_date(submitted_at):
    cutoff = submitted_at.replace(hour=14, minute=0, second=0, microsecond=0)
    if submitted_at <= cutoff:
        return submitted_at.replace(hour=18, minute=0, second=0, microsecond=0)
    next_day = submitted_at + timedelta(days=1)
    return next_day.replace(hour=13, minute=0, second=0, microsecond=0)


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


def _create_storekeeper_task(order_id, agency, request, submitted_at, observer=None):
    if not agency:
        return
    storekeeper = (
        Employee.objects.filter(role="storekeeper", is_active=True)
        .order_by("full_name")
        .first()
    )
    if not storekeeper:
        return
    description = f"Клиент: {agency.agn_name or agency.inn or agency.id}"
    Task.objects.create(
        title=f"Принять заявку на приемку товара №{order_id}",
        description=description,
        route=f"/orders/receiving/{order_id}/",
        assigned_to=storekeeper,
        observer=observer,
        created_by=request.user if request.user.is_authenticated else None,
        due_date=submitted_at + timedelta(days=1),
    )


def _create_manager_followup_task(order_id, agency, request, submitted_at, observer=None):
    if not agency:
        return
    manager = (
        Employee.objects.filter(role="manager", is_active=True)
        .order_by("full_name")
        .first()
    )
    if not manager:
        return
    title = f"Проверьте размещение по заявке на приемку товара №{order_id}"
    existing = Task.objects.filter(
        route=f"/orders/receiving/{order_id}/",
        assigned_to=manager,
        title=title,
    ).exclude(status="done")
    if existing.exists():
        return
    description = f"Клиент: {agency.agn_name or agency.inn or agency.id}"
    Task.objects.create(
        title=title,
        description=description,
        route=f"/orders/receiving/{order_id}/",
        assigned_to=manager,
        observer=observer,
        created_by=request.user if request.user.is_authenticated else None,
        due_date=_manager_due_date(submitted_at),
    )

def _latest_payload_from_entries(entries):
    for entry in reversed(entries):
        payload = entry.payload or {}
        if not payload:
            continue
        significant_keys = set(payload.keys()) - {
            "comment",
            "message",
            "status",
            "status_label",
            "submit_action",
        }
        if significant_keys:
            return payload
    return entries[-1].payload or {} if entries else {}


def _client_agency_from_request(request):
    if not request.user.is_authenticated:
        return None
    client_id = request.GET.get("client") or request.GET.get("agency")
    if not client_id:
        return None
    return Agency.objects.filter(pk=client_id, portal_user=request.user).first()


_PHONE_DIGITS_RE = re.compile(r"\D+")


def _is_valid_driver_phone(value: str) -> bool:
    if not value:
        return False
    digits = _PHONE_DIGITS_RE.sub("", value)
    if len(digits) != 11:
        return False
    return digits[0] in {"7", "8"}


def _min_receiving_eta(now):
    tz = timezone.get_current_timezone()
    next_day = (now + timedelta(days=1)).date()
    min_hour = 10
    if now.hour >= 18:
        min_hour = 13
    return timezone.make_aware(datetime.combine(next_day, time(min_hour, 0)), tz)


def _format_payload_value(value):
    if value is None or value == "":
        return "-"
    return str(value).strip()


def _place_type_label(value: str) -> str:
    text = _format_payload_value(value)
    if text == "-":
        return text
    labels = {
        "pallet": "Паллет",
        "box": "Короб",
        "bag": "Мешок",
    }
    return labels.get(text, text)


def _format_datetime_value(value):
    text = _format_payload_value(value)
    if text == "-":
        return text
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return text
    return parsed.strftime("%d.%m.%Y, %H:%M")


_ISO_DATETIME_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?\b")


def _format_message_text(text: str) -> str:
    if not text:
        return ""

    def _replace(match):
        raw = match.group(0)
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return raw
        return parsed.strftime("%d.%m.%Y, %H:%M")

    return _ISO_DATETIME_RE.sub(_replace, text)


def _describe_payload_changes(old_payload, new_payload):
    changes = []
    fields = [
        ("eta_at", "Плановая дата/время"),
        ("expected_boxes", "Количество мест"),
        ("place_type", "Тип мест"),
        ("vehicle_number", "Номер авто"),
        ("driver_phone", "Телефон водителя"),
        ("comment", "Комментарий"),
    ]
    for key, label in fields:
        if key == "eta_at":
            old_val = _format_datetime_value((old_payload or {}).get(key))
            new_val = _format_datetime_value((new_payload or {}).get(key))
        elif key == "place_type":
            old_val = _place_type_label((old_payload or {}).get(key))
            new_val = _place_type_label((new_payload or {}).get(key))
        else:
            old_val = _format_payload_value((old_payload or {}).get(key))
            new_val = _format_payload_value((new_payload or {}).get(key))
        if old_val != new_val:
            changes.append(f"{label}: {old_val} → {new_val}")
    old_items = (old_payload or {}).get("items") or []
    new_items = (new_payload or {}).get("items") or []
    if old_items != new_items:
        changes.append(f"Состав поставки: {len(old_items)} → {len(new_items)} поз.")
    return changes


class OrdersHomeView(RoleRequiredMixin, TemplateView):
    template_name = 'orders/index.html'
    allowed_roles = ("manager", "storekeeper", "head_manager", "director", "admin")

    def dispatch(self, request, *args, **kwargs):
        client_agency = _client_agency_from_request(request)
        if client_agency:
            request._client_agency = client_agency
            return TemplateView.dispatch(self, request, *args, **kwargs)
        return super().dispatch(request, *args, **kwargs)

    @staticmethod
    def _next_order_number() -> str:
        last = (
            OrderAuditEntry.objects.filter(order_type="receiving")
            .order_by("-created_at")
            .first()
        )
        if not last or not last.order_id:
            return "1"
        match = re.search(r"(\d+)$", last.order_id)
        if not match:
            count = OrderAuditEntry.objects.filter(order_type="receiving").count()
            return str(count + 1)
        return str(int(match.group(1)) + 1)

    def get(self, request, *args, **kwargs):
        status = (request.GET.get("status") or "").lower()
        ok = request.GET.get("ok") == "1"
        submitted = kwargs.get("submitted") or (ok and status != "draft")
        draft_saved = ok and status == "draft"
        error = kwargs.get("error")
        ctx = self.get_context_data(
            submitted=submitted,
            draft_saved=draft_saved,
            error=error,
            **kwargs,
        )
        return self.render_to_response(ctx)

    def post(self, request, *args, **kwargs):
        active_tab = kwargs.get("tab", "journal")
        if active_tab != "receiving":
            return redirect("/orders/")

        client_agency = getattr(request, "_client_agency", None) or _client_agency_from_request(request)
        if client_agency:
            agency = client_agency
        else:
            agency_id = request.POST.get("agency_id")
            agency = Agency.objects.filter(pk=agency_id).first()
        if not agency:
            return self.get(request, error="Выберите клиента.")

        eta_raw = (request.POST.get("eta_at") or "").strip()
        if not eta_raw:
            return self.get(request, error="Укажите плановую дату и время прибытия.")
        try:
            eta_value = datetime.fromisoformat(eta_raw)
        except ValueError:
            return self.get(request, error="Некорректная дата/время прибытия.")
        if timezone.is_naive(eta_value):
            eta_value = timezone.make_aware(eta_value, timezone.get_current_timezone())
        eta_value = timezone.localtime(eta_value)
        min_eta = _min_receiving_eta(timezone.localtime())
        if eta_value < min_eta:
            return self.get(
                request,
                error="Плановая дата/время не может быть раньше следующего дня. После 18:00 — не раньше 13:00.",
            )

        expected_boxes_raw = (request.POST.get("expected_boxes") or "").strip()
        place_type = (request.POST.get("place_type") or "").strip()
        vehicle_number = (request.POST.get("vehicle_number") or "").strip()
        driver_phone = (request.POST.get("driver_phone") or "").strip()
        try:
            expected_boxes_value = int(expected_boxes_raw)
        except (TypeError, ValueError):
            expected_boxes_value = 0
        if expected_boxes_value <= 0:
            return self.get(request, error="Укажите количество мест.")
        if not place_type:
            return self.get(request, error="Выберите тип мест.")
        if not vehicle_number:
            return self.get(request, error="Укажите номер авто.")
        if not _is_valid_driver_phone(driver_phone):
            return self.get(request, error="Введите корректный телефон водителя.")

        sku_codes = request.POST.getlist("sku_code[]")
        sku_ids = request.POST.getlist("sku_id[]")
        names = request.POST.getlist("item_name[]")
        sizes = request.POST.getlist("size[]")
        qtys = request.POST.getlist("qty[]")
        position_comments = request.POST.getlist("position_comment[]")
        items = []
        row_count = max(
            len(sku_codes),
            len(qtys),
            len(names),
            len(position_comments),
            len(sku_ids),
            len(sizes),
        )
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
                    "size": sizes[idx] if idx < len(sizes) else "",
                    "qty": qty,
                    "comment": position_comments[idx] if idx < len(position_comments) else "",
                }
            )

        submit_action = request.POST.get("submit_action")
        edit_order_id = request.POST.get("edit_order_id")
        role = get_request_role(request)
        if edit_order_id and role != "manager":
            return self.get(request, error="Недостаточно прав для исправления заявки")
        old_payload = {}
        if edit_order_id:
            previous_entries = list(
                OrderAuditEntry.objects.filter(
                    order_id=edit_order_id,
                    order_type="receiving",
                ).order_by("created_at")
            )
            old_payload = _latest_payload_from_entries(previous_entries)
        status_value = "draft" if submit_action == "draft" else "sent_unconfirmed"
        status_label = "Черновик" if status_value == "draft" else "Новая заявка"
        if edit_order_id and old_payload:
            preserved_status = old_payload.get("status") or old_payload.get("submit_action")
            preserved_label = old_payload.get("status_label")
            if preserved_status:
                status_value = preserved_status
            if preserved_label:
                status_label = preserved_label
        payload = {
            "eta_at": eta_raw,
            "expected_boxes": expected_boxes_raw,
            "place_type": place_type,
            "vehicle_number": vehicle_number,
            "driver_phone": driver_phone,
            "comment": request.POST.get("comment"),
            "submit_action": submit_action,
            "status": status_value,
            "status_label": status_label,
            "items": items,
            "documents": [f.name for f in request.FILES.getlist("documents")],
        }
        if edit_order_id:
            changes = _describe_payload_changes(old_payload, payload)
            if changes:
                description = f"Исправление заявки №{edit_order_id}: " + "; ".join(changes)
            else:
                description = f"Исправление заявки №{edit_order_id}: без изменений"
            log_order_action(
                "update",
                order_id=edit_order_id,
                order_type="receiving",
                user=request.user if request.user.is_authenticated else None,
                agency=agency,
                description=description,
                payload=payload,
            )
            return redirect(f"/orders/receiving/{edit_order_id}/")

        action_label = "черновик" if submit_action == "draft" else "заявка"
        order_id = self._next_order_number()
        log_order_action(
            "create",
            order_id=order_id,
            order_type="receiving",
            user=request.user if request.user.is_authenticated else None,
            agency=agency,
            description=f"Заявка на приемку №{order_id} ({action_label})",
            payload=payload,
        )
        if submit_action != "draft" and self.request.GET.get("client"):
            _create_manager_task(order_id, agency, request, timezone.localtime())
        return redirect(
            f"/orders/receiving/?client={agency.id}&ok=1&status={status_value}&order={order_id}"
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        active_tab = kwargs.get("tab", "journal")
        ctx["active_tab"] = active_tab
        ctx["submitted"] = kwargs.get("submitted", False)
        ctx["draft_saved"] = kwargs.get("draft_saved", False)
        ctx["error"] = kwargs.get("error")
        ctx["status_label"] = ctx.get("status_label", "Подготовка заявки")
        ctx["order_number"] = ctx.get("order_number", "")
        ctx["cabinet_url"] = resolve_cabinet_url(get_request_role(self.request))

        client_id = self.request.GET.get("client")
        agency_id = self.request.GET.get("agency")
        agency_key = client_id or agency_id
        client_agency = getattr(self.request, "_client_agency", None) or _client_agency_from_request(self.request)
        agency = client_agency or (Agency.objects.filter(pk=agency_key).first() if agency_key else None)
        client_view = bool(client_agency)
        ctx["agency"] = agency
        ctx["client_view"] = client_view

        if active_tab == "journal":
            raw_entries = OrderAuditEntry.objects.select_related("user", "agency").order_by("-created_at")
            if agency:
                raw_entries = raw_entries.filter(agency=agency)
            raw_entries = list(raw_entries)
            manager_label = _manager_label()
            storekeeper_label = _storekeeper_label()
            latest_by_order = {}
            status_by_order = {}
            for entry in raw_entries:
                if entry.order_id not in latest_by_order:
                    latest_by_order[entry.order_id] = entry
                if entry.order_id not in status_by_order and _is_status_entry(entry):
                    status_by_order[entry.order_id] = entry
            latest_entries = [
                status_by_order.get(order_id, latest_entry)
                for order_id, latest_entry in latest_by_order.items()
            ]
            latest_entries.sort(key=lambda item: item.created_at, reverse=True)
            ctx["entries"] = [
                {
                    "created_at": entry.created_at,
                    "order_type": entry.order_type,
                    "type_label": _order_type_label(entry.order_type),
                    "order_id": entry.order_id,
                    "action_label": _journal_action_label(entry, manager_label, storekeeper_label),
                    "status_label": _journal_status_label(entry),
                    "agency": entry.agency,
                    "client_label": _shorten_ip_name(
                        (entry.agency.agn_name or str(entry.agency)) if entry.agency else "-"
                    ),
                    "detail_url": f"/orders/{entry.order_type}/{entry.order_id}/"
                    + (f"?client={entry.agency.id}" if client_view and entry.agency else ""),
                }
                for entry in latest_entries
            ]
        if active_tab == "receiving":
            ctx["agencies"] = Agency.objects.order_by("agn_name")
            ctx["current_time"] = timezone.localtime()
            ctx["min_past_hours"] = 0
            status = self.request.GET.get("status")
            status_label = "Подготовка заявки"
            if status == "draft":
                status_label = "Черновик"
            elif status == "sent_unconfirmed":
                status_label = "Новая заявка"
            ctx["status_label"] = status_label
            ctx["order_number"] = self.request.GET.get("order", "")

            edit_order_id = self.request.GET.get("edit")
            if edit_order_id:
                role = get_request_role(self.request)
                if role != "manager":
                    ctx["error"] = "Недостаточно прав для исправления заявки"
                else:
                    entries = list(
                        OrderAuditEntry.objects.filter(
                            order_id=edit_order_id,
                            order_type="receiving",
                        )
                        .select_related("agency")
                        .order_by("created_at")
                    )
                    if entries:
                        edit_payload = _latest_payload_from_entries(entries)
                        if not agency:
                            agency = entries[-1].agency
                            ctx["agency"] = agency
                        ctx["edit_mode"] = True
                        ctx["edit_order_id"] = edit_order_id
                        ctx["edit_payload"] = edit_payload
                        ctx["status_label"] = "Исправление заявки"
                        ctx["order_number"] = edit_order_id

            sku_options = []
            if agency:
                skus = (
                    SKU.objects.filter(agency=agency, deleted=False)
                    .prefetch_related("barcodes")
                    .order_by("sku_code")
                )
                for sku in skus:
                    barcodes = [barcode.value for barcode in sku.barcodes.all()]
                    size_map = {}
                    for barcode in sku.barcodes.all():
                        size_value = (barcode.size or "").strip()
                        if not size_value:
                            continue
                        size_map.setdefault(size_value, []).append(barcode.value)
                    sku_options.append(
                        {
                            "id": sku.id,
                            "code": sku.sku_code,
                            "name": sku.name,
                            "barcodes_joined": "|".join(barcodes),
                            "sizes_json": json.dumps(size_map, ensure_ascii=True),
                        }
                    )
            ctx["sku_options"] = sku_options
        return ctx


class OrdersDetailView(RoleRequiredMixin, TemplateView):
    template_name = "orders/detail.html"
    order_type = "receiving"
    allowed_roles = ("manager", "storekeeper", "head_manager", "director", "admin")

    def dispatch(self, request, *args, **kwargs):
        client_agency = _client_agency_from_request(request)
        if client_agency:
            request._client_agency = client_agency
            return TemplateView.dispatch(self, request, *args, **kwargs)
        return super().dispatch(request, *args, **kwargs)

    @staticmethod
    def _payload_from_entries(entries):
        for entry in reversed(entries):
            payload = entry.payload or {}
            if not payload:
                continue
            significant_keys = set(payload.keys()) - {
                "comment",
                "message",
                "status",
                "status_label",
                "submit_action",
            }
            if significant_keys:
                return payload
        return entries[-1].payload or {} if entries else {}

    def post(self, request, *args, **kwargs):
        order_id = kwargs.get("order_id")
        if not order_id:
            return redirect("/orders/")
        action = request.POST.get("action")
        if action == "send_to_warehouse":
            latest = (
                OrderAuditEntry.objects.filter(order_id=order_id, order_type=self.order_type)
                .select_related("agency")
                .order_by("-created_at")
                .first()
            )
            if not latest:
                return redirect("/orders/")
            payload = dict(latest.payload or {})
            payload["status"] = "warehouse"
            payload["status_label"] = "На складе"
            log_order_action(
                "status",
                order_id=order_id,
                order_type=self.order_type,
                user=request.user if request.user.is_authenticated else None,
                agency=latest.agency if latest else None,
                description="Подтверждено и отправлено на склад",
                payload=payload,
            )
            Task.objects.filter(
                route=f"/orders/receiving/{order_id}/",
                assigned_to__role="manager",
            ).exclude(status="done").update(status="done")
            observer = Employee.objects.filter(
                user=request.user, is_active=True
            ).first()
            _create_storekeeper_task(
                order_id,
                latest.agency,
                request,
                timezone.localtime(),
                observer=observer,
            )
            client_param = request.GET.get("client")
            suffix = f"?client={client_param}" if client_param else ""
            return redirect(f"/orders/{self.order_type}/{order_id}/{suffix}")
        if action == "send_act_to_client":
            entries = list(
                OrderAuditEntry.objects.filter(order_id=order_id, order_type=self.order_type)
                .select_related("agency")
                .order_by("created_at")
            )
            if not entries:
                return redirect("/orders/")
            role = get_request_role(request)
            if role not in {"manager", "head_manager", "director", "admin"}:
                return redirect("/orders/")
            _send_act_to_client(order_id, entries, request.user)
            client_param = request.GET.get("client")
            suffix = f"?client={client_param}" if client_param else ""
            return redirect(f"/orders/{self.order_type}/{order_id}/{suffix}")
        if action == "create_receiving_act":
            return redirect(f"/orders/{self.order_type}/{order_id}/act/")
        comment = (request.POST.get("comment") or "").strip()
        if comment:
            latest = (
                OrderAuditEntry.objects.filter(order_id=order_id, order_type=self.order_type)
                .select_related("agency")
                .order_by("-created_at")
                .first()
            )
            log_order_action(
                "comment",
                order_id=order_id,
                order_type=self.order_type,
                user=request.user if request.user.is_authenticated else None,
                agency=latest.agency if latest else None,
                description=comment,
                payload={"comment": comment},
            )
        client_param = request.GET.get("client")
        suffix = f"?client={client_param}" if client_param else ""
        return redirect(f"/orders/{self.order_type}/{order_id}/{suffix}")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        order_id = kwargs.get("order_id")
        client_agency = getattr(self.request, "_client_agency", None) or _client_agency_from_request(self.request)
        client_view = bool(client_agency)
        entries = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type=self.order_type)
            .select_related("user", "agency")
            .order_by("created_at")
        )
        entries_list = list(entries)
        latest = entries_list[-1] if entries_list else None
        status_entry = _current_status_entry(entries_list)
        payload = self._payload_from_entries(entries_list)
        ctx["order_id"] = order_id
        ctx["order_type"] = self.order_type
        ctx["order_title"] = _order_title_label(self.order_type)
        ctx["agency"] = latest.agency if latest else client_agency
        ctx["client_view"] = client_view
        ctx["status_label"] = _status_label_from_entry(status_entry) if status_entry else "-"
        ctx["responsible"] = _current_responsible_label(status_entry)
        ctx["cabinet_url"] = resolve_cabinet_url(get_request_role(self.request))
        can_send_to_warehouse = False
        role = get_request_role(self.request)
        ctx["can_edit_order"] = bool(role == "manager" and not client_view)
        can_create_receiving_act = False
        if status_entry and not client_view and role == "manager":
            payload = status_entry.payload or {}
            status_value = (payload.get("status") or payload.get("submit_action") or "").lower()
            status_label = (payload.get("status_label") or "").lower()
            can_send_to_warehouse = status_value in {"sent_unconfirmed", "send", "submitted"}
            if "склад" in status_label or status_value in {"warehouse", "on_warehouse"}:
                can_send_to_warehouse = False
            if status_value == "draft":
                can_send_to_warehouse = False
        if status_entry and not client_view and role == "storekeeper":
            payload = status_entry.payload or {}
            status_value = (payload.get("status") or payload.get("submit_action") or "").lower()
            status_label = (payload.get("status_label") or "").lower()
            has_receiving_act = any(
                (entry.payload or {}).get("act") == "receiving" for entry in entries_list
            )
            if not has_receiving_act and (
                status_value in {"warehouse", "on_warehouse"} or "склад" in status_label
            ):
                can_create_receiving_act = True
        ctx["can_send_to_warehouse"] = can_send_to_warehouse
        ctx["can_create_receiving_act"] = can_create_receiving_act
        client_label = "-"
        if latest and latest.agency:
            name = latest.agency.agn_name or latest.agency.fio_agn or str(latest.agency)
            client_label = _shorten_ip_name(name)
        ctx["client_label"] = client_label
        ctx["meta"] = {
            "eta_at": _format_datetime_value(payload.get("eta_at")),
            "expected_boxes": payload.get("expected_boxes"),
            "place_type": _place_type_label(payload.get("place_type")),
            "vehicle_number": payload.get("vehicle_number"),
            "driver_phone": payload.get("driver_phone"),
            "comment": payload.get("comment"),
        }
        ctx["items"] = payload.get("items") or []
        participants = []
        seen = set()
        for entry in entries_list:
            label = _actor_label(entry.user, entry.agency, client_view=client_view)
            if label in seen:
                continue
            seen.add(label)
            participants.append(label)
        ctx["participants"] = participants
        comments = [
            {
                "created_at": entry.created_at,
                "actor_label": _actor_label(entry.user, entry.agency, client_view=client_view),
                "text": entry.description or (entry.payload or {}).get("comment") or "-",
            }
            for entry in entries_list
            if entry.action == "comment"
        ]
        comments.reverse()
        ctx["comments"] = comments
        ctx["history"] = [
            {
                "created_at": entry.created_at,
                "action_label": _history_action_label(entry),
                "status_label": _status_label_from_entry(entry),
                "description": _format_message_text(entry.description),
                "actor_label": _history_actor_label(
                    entry,
                    client_view=client_view,
                    client_label=client_label,
                ),
            }
            for entry in reversed(entries_list)
            if entry.action != "comment"
        ]
        act_entry = _find_act_entry(entries_list, "receiving", "акт приемки")
        placement_entry = _find_act_entry(entries_list, "placement", "акт размещения")
        ctx["act_entry"] = act_entry
        ctx["placement_act_entry"] = placement_entry
        ctx["has_receiving_act"] = bool(act_entry)
        ctx["has_placement_act"] = bool(placement_entry)
        if act_entry:
            ctx["act_label"] = (act_entry.payload or {}).get("act_label") or "Акт приемки"
        else:
            ctx["act_label"] = ""
        if placement_entry:
            ctx["placement_act_label"] = (placement_entry.payload or {}).get("act_label") or "Акт размещения"
        else:
            ctx["placement_act_label"] = ""
        role = get_request_role(self.request)
        ctx["can_create_placement_act"] = bool(
            role == "storekeeper" and act_entry and not placement_entry
        )
        ctx["can_send_act_to_client"] = bool(
            role in {"manager", "head_manager", "director", "admin"}
            and act_entry
            and placement_entry
            and not _is_done_status(status_entry)
            and not client_view
        )
        return ctx


class ReceivingActView(RoleRequiredMixin, TemplateView):
    template_name = "orders/receiving_act.html"
    order_type = "receiving"
    allowed_roles = ("storekeeper", "head_manager", "director", "admin")

    def dispatch(self, request, *args, **kwargs):
        client_agency = _client_agency_from_request(request)
        if client_agency:
            order_id = kwargs.get("order_id")
            entries = self._load_entries(order_id)
            if not entries:
                return HttpResponseForbidden("Доступ запрещен")
            if not any(entry.agency_id == client_agency.id for entry in entries if entry.agency_id):
                return HttpResponseForbidden("Доступ запрещен")
            if not self._act_entry(entries):
                return HttpResponseForbidden("Доступ запрещен")
            if request.method != "GET":
                return HttpResponseForbidden("Доступ запрещен")
            request._client_agency = client_agency
            return TemplateView.dispatch(self, request, *args, **kwargs)
        return super().dispatch(request, *args, **kwargs)

    def _load_entries(self, order_id):
        return list(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type=self.order_type)
            .select_related("user", "agency")
            .order_by("created_at")
        )

    def _act_entry(self, entries):
        return _act_entry_from_entries(entries, "receiving")

    def _can_create(self, entries):
        if not entries:
            return False
        if self._act_entry(entries):
            return False
        status_entry = _current_status_entry(entries)
        return _warehouse_status_from_entry(status_entry)

    def get(self, request, *args, **kwargs):
        ok = request.GET.get("ok") == "1"
        error = request.GET.get("error")
        ctx = self.get_context_data(ok=ok, error=error, **kwargs)
        return self.render_to_response(ctx)

    def post(self, request, *args, **kwargs):
        order_id = kwargs.get("order_id")
        entries = self._load_entries(order_id)
        if not entries:
            return redirect("/orders/")
        if not self._can_create(entries):
            return redirect(f"/orders/receiving/{order_id}/act/?error=1")
        payload = _latest_payload_from_entries(entries)
        items = payload.get("items") or []
        if not items:
            return redirect(f"/orders/receiving/{order_id}/act/?error=1")
        actual_raw = request.POST.getlist("actual_qty[]")
        eta_raw = (request.POST.get("eta_at") or "").strip()
        vehicle_number = (request.POST.get("vehicle_number") or "").strip()
        if not eta_raw:
            return redirect(f"/orders/receiving/{order_id}/act/?error=1")
        try:
            eta_value = datetime.fromisoformat(eta_raw)
        except ValueError:
            return redirect(f"/orders/receiving/{order_id}/act/?error=1")
        if timezone.is_naive(eta_value):
            eta_value = timezone.make_aware(eta_value, timezone.get_current_timezone())
        eta_value = timezone.localtime(eta_value)
        if not vehicle_number:
            return redirect(f"/orders/receiving/{order_id}/act/?error=1")
        act_items = []
        has_mismatch = False
        for idx, item in enumerate(items):
            raw_value = actual_raw[idx] if idx < len(actual_raw) else ""
            actual_value = _parse_qty_value(raw_value)
            if actual_value is None:
                return redirect(f"/orders/receiving/{order_id}/act/?error=1")
            planned_value = _parse_qty_value(item.get("qty")) or 0
            if planned_value != actual_value:
                has_mismatch = True
            act_items.append(
                {
                    "sku_code": item.get("sku_code"),
                    "name": item.get("name"),
                    "size": item.get("size"),
                    "planned_qty": item.get("qty"),
                    "actual_qty": actual_value,
                    "comment": item.get("comment"),
                }
            )
        extra_names = request.POST.getlist("extra_name[]")
        extra_sizes = request.POST.getlist("extra_size[]")
        extra_planned = request.POST.getlist("extra_planned_qty[]")
        extra_actual = request.POST.getlist("extra_actual_qty[]")
        extra_count = max(len(extra_names), len(extra_sizes), len(extra_planned), len(extra_actual))
        for idx in range(extra_count):
            name = (extra_names[idx] if idx < len(extra_names) else "").strip()
            size = (extra_sizes[idx] if idx < len(extra_sizes) else "").strip()
            planned_raw = extra_planned[idx] if idx < len(extra_planned) else ""
            actual_raw_value = extra_actual[idx] if idx < len(extra_actual) else ""
            if not name and not size and not planned_raw and not actual_raw_value:
                continue
            if not name:
                return redirect(f"/orders/receiving/{order_id}/act/?error=1")
            planned_value = _parse_qty_value(planned_raw)
            if planned_value is None:
                planned_value = 0
            actual_value = _parse_qty_value(actual_raw_value)
            if actual_value is None:
                return redirect(f"/orders/receiving/{order_id}/act/?error=1")
            if planned_value != actual_value:
                has_mismatch = True
            act_items.append(
                {
                    "sku_code": "",
                    "name": name,
                    "size": size,
                    "planned_qty": planned_value,
                    "actual_qty": actual_value,
                    "comment": "",
                    "extra": True,
                }
            )
        status_entry = _current_status_entry(entries)
        latest = entries[-1]
        act_payload = dict((status_entry.payload or {}) if status_entry else {})
        if not act_payload.get("status"):
            act_payload["status"] = "warehouse"
        if not act_payload.get("status_label"):
            act_payload["status_label"] = "На складе"
        act_payload["eta_at"] = eta_value.isoformat()
        act_payload["vehicle_number"] = vehicle_number
        act_payload["act"] = "receiving"
        act_payload["act_label"] = (
            "Акт приемки с расхождениями" if has_mismatch else "Акт приемки"
        )
        act_payload["act_mismatch"] = has_mismatch
        act_payload["act_items"] = act_items
        log_order_action(
            "status",
            order_id=order_id,
            order_type=self.order_type,
            user=request.user if request.user.is_authenticated else None,
            agency=latest.agency,
            description="Создан акт приемки",
            payload=act_payload,
        )
        return redirect(f"/orders/receiving/{order_id}/placement/?ok=1")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        order_id = kwargs.get("order_id")
        entries = self._load_entries(order_id)
        latest = entries[-1] if entries else None
        client_agency = getattr(self.request, "_client_agency", None)
        client_view = bool(client_agency)
        status_entry = _current_status_entry(entries)
        payload = _latest_payload_from_entries(entries)
        items = payload.get("items") or []
        act_entry = self._act_entry(entries)
        act_items = (act_entry.payload or {}).get("act_items") if act_entry else []
        if act_entry:
            act_label = (act_entry.payload or {}).get("act_label") or "Акт приемки"
        else:
            act_label = "Акт приемки"
        can_submit = self._can_create(entries)
        if client_view:
            can_submit = False
        display_items = []
        base_items = act_items or items
        for idx, item in enumerate(base_items):
            actual_value = "" if not act_items else item.get("actual_qty")
            if act_items:
                planned_value = item.get("planned_qty")
                name = item.get("name")
                size = item.get("size")
            else:
                planned_value = item.get("qty")
                name = item.get("name")
                size = item.get("size")
            display_items.append(
                {
                    "sku_code": item.get("sku_code") or "",
                    "name": name or "-",
                    "size": size or "-",
                    "planned_qty": planned_value if planned_value not in (None, "") else "-",
                    "actual_qty": actual_value if actual_value is not None else "",
                    "comment": item.get("comment") or "",
                }
            )
        client_label = "-"
        if latest and latest.agency:
            name = latest.agency.agn_name or latest.agency.fio_agn or str(latest.agency)
            client_label = _shorten_ip_name(name)
        arrival_value = payload.get("eta_at")
        if act_entry:
            act_payload = act_entry.payload or {}
            arrival_value = act_payload.get("eta_at") or arrival_value
        vehicle_value = payload.get("vehicle_number")
        driver_phone_value = payload.get("driver_phone")
        if act_entry:
            act_payload = act_entry.payload or {}
            vehicle_value = act_payload.get("vehicle_number") or vehicle_value
            driver_phone_value = act_payload.get("driver_phone") or driver_phone_value
        arrival_input = ""
        if arrival_value:
            try:
                parsed = datetime.fromisoformat(str(arrival_value))
                if timezone.is_naive(parsed):
                    parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
                parsed = timezone.localtime(parsed)
                arrival_input = parsed.strftime("%Y-%m-%dT%H:%M")
            except (TypeError, ValueError):
                arrival_input = ""
        cabinet_url = resolve_cabinet_url(get_request_role(self.request))
        if client_view and client_agency:
            cabinet_url = f"/client/dashboard/?client={client_agency.id}"
        driver_phone = _format_payload_value(driver_phone_value)
        ctx.update(
            {
                "order_id": order_id,
                "client_label": client_label,
                "status_label": _status_label_from_entry(status_entry) if status_entry else "-",
                "cabinet_url": cabinet_url,
                "client_view": client_view,
                "client_param": client_agency.id if client_agency else "",
                "arrival_at": _format_datetime_value(arrival_value),
                "arrival_value": _format_datetime_value(arrival_value),
                "arrival_input": arrival_input,
                "vehicle_number": vehicle_value or "-",
                "vehicle_value": vehicle_value or "",
                "driver_phone": driver_phone,
                "can_submit": can_submit,
                "can_add_items": can_submit,
                "act_exists": bool(act_entry),
                "act_label": act_label,
                "items": display_items,
                "ok": kwargs.get("ok", False),
                "error": kwargs.get("error"),
            }
        )
        return ctx


class PlacementActView(RoleRequiredMixin, TemplateView):
    template_name = "orders/placement_act.html"
    order_type = "receiving"
    allowed_roles = ("storekeeper", "head_manager", "director", "admin", "manager")

    def _load_entries(self, order_id):
        return list(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type=self.order_type)
            .select_related("user", "agency")
            .order_by("created_at")
        )

    def _receiving_act_entry(self, entries):
        return _act_entry_from_entries(entries, "receiving")

    def _placement_act_entry(self, entries):
        return _act_entry_from_entries(entries, "placement")

    def _can_create(self, entries):
        if not entries:
            return False
        if not self._receiving_act_entry(entries):
            return False
        if self._placement_act_entry(entries):
            return False
        return True

    def get(self, request, *args, **kwargs):
        ok = request.GET.get("ok") == "1"
        error = request.GET.get("error")
        ctx = self.get_context_data(ok=ok, error=error, **kwargs)
        return self.render_to_response(ctx)

    def post(self, request, *args, **kwargs):
        order_id = kwargs.get("order_id")
        entries = self._load_entries(order_id)
        if not entries:
            return redirect("/orders/")
        role = get_request_role(request)
        if role != "storekeeper":
            return redirect(f"/orders/receiving/{order_id}/placement/")
        if not self._can_create(entries):
            return redirect(f"/orders/receiving/{order_id}/placement/?error=1")
        receiving_act = self._receiving_act_entry(entries)
        act_items = (receiving_act.payload or {}).get("act_items") if receiving_act else []
        if not act_items:
            return redirect(f"/orders/receiving/{order_id}/placement/?error=1")
        box_raw = request.POST.getlist("box_qty[]")
        pallet_raw = request.POST.getlist("pallet_qty[]")
        placement_items = []
        for idx, item in enumerate(act_items):
            box_value = _parse_qty_value(box_raw[idx] if idx < len(box_raw) else "")
            pallet_value = _parse_qty_value(pallet_raw[idx] if idx < len(pallet_raw) else "")
            if box_value is None:
                box_value = 0
            if pallet_value is None:
                pallet_value = 0
            placement_items.append(
                {
                    "sku_code": item.get("sku_code"),
                    "name": item.get("name"),
                    "size": item.get("size"),
                    "actual_qty": item.get("actual_qty"),
                    "box_qty": box_value,
                    "pallet_qty": pallet_value,
                    "comment": item.get("comment"),
                }
            )
        boxes_raw = request.POST.get("boxes_json") or "[]"
        pallets_raw = request.POST.get("pallets_json") or "[]"
        try:
            boxes_data = json.loads(boxes_raw)
            pallets_data = json.loads(pallets_raw)
        except json.JSONDecodeError:
            return redirect(f"/orders/receiving/{order_id}/placement/?error=1")
        totals = {}
        def add_total(item, qty, field):
            key = _item_key(item.get("sku"), item.get("name"), item.get("size"))
            entry = totals.setdefault(key, {"box": 0, "pallet": 0, "total": 0})
            entry[field] += qty
            entry["total"] += qty
        if isinstance(boxes_data, list):
            for box in boxes_data:
                for item in (box or {}).get("items", []) or []:
                    qty = _parse_qty_value(item.get("qty")) or 0
                    add_total(item, qty, "box")
        if isinstance(pallets_data, list):
            for pallet in pallets_data:
                for item in (pallet or {}).get("items", []) or []:
                    qty = _parse_qty_value(item.get("qty")) or 0
                    add_total(item, qty, "pallet")
        if totals:
            placement_items = []
            for item in act_items:
                key = _item_key(item.get("sku_code"), item.get("name"), item.get("size"))
                entry = totals.get(key, {"box": 0, "pallet": 0, "total": 0})
                actual_qty = _parse_qty_value(item.get("actual_qty")) or 0
                if entry["total"] > actual_qty:
                    return redirect(f"/orders/receiving/{order_id}/placement/?error=1")
                placement_items.append(
                    {
                        "sku_code": item.get("sku_code"),
                        "name": item.get("name"),
                        "size": item.get("size"),
                        "actual_qty": actual_qty,
                        "box_qty": entry["box"],
                        "pallet_qty": entry["pallet"],
                        "comment": item.get("comment"),
                    }
                )
        status_entry = _current_status_entry(entries)
        latest = entries[-1]
        act_payload = dict((status_entry.payload or {}) if status_entry else {})
        if not act_payload.get("status"):
            act_payload["status"] = "warehouse"
        if not act_payload.get("status_label"):
            act_payload["status_label"] = "На складе"
        act_payload["act"] = "placement"
        act_payload["act_label"] = "Акт размещения"
        act_payload["act_items"] = placement_items
        act_payload["act_boxes"] = boxes_data if isinstance(boxes_data, list) else []
        act_payload["act_pallets"] = pallets_data if isinstance(pallets_data, list) else []
        log_order_action(
            "status",
            order_id=order_id,
            order_type=self.order_type,
            user=request.user if request.user.is_authenticated else None,
            agency=latest.agency,
            description="Создан акт размещения",
            payload=act_payload,
        )
        Task.objects.filter(
            route=f"/orders/receiving/{order_id}/",
            assigned_to__role="storekeeper",
        ).exclude(status="done").update(status="done")
        observer = Employee.objects.filter(user=request.user, is_active=True).first()
        _create_manager_followup_task(
            order_id,
            latest.agency,
            request,
            timezone.localtime(),
            observer=observer,
        )
        return redirect(f"/orders/receiving/{order_id}/placement/?ok=1")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        order_id = kwargs.get("order_id")
        entries = self._load_entries(order_id)
        latest = entries[-1] if entries else None
        status_entry = _current_status_entry(entries)
        receiving_act = self._receiving_act_entry(entries)
        placement_act = self._placement_act_entry(entries)
        receiving_items = (receiving_act.payload or {}).get("act_items") if receiving_act else []
        placement_items = (placement_act.payload or {}).get("act_items") if placement_act else []
        display_items = []
        source_items = placement_items or receiving_items
        for item in source_items:
            display_items.append(
                {
                    "sku_code": item.get("sku_code") or "",
                    "name": item.get("name") or "-",
                    "size": item.get("size") or "-",
                    "actual_qty": item.get("actual_qty") or 0,
                    "box_qty": item.get("box_qty") or 0,
                    "pallet_qty": item.get("pallet_qty") or 0,
                }
            )
        catalog_items = []
        remaining_items = []
        for item in receiving_items:
            catalog_items.append(
                {
                    "sku_code": item.get("sku_code") or "",
                    "name": item.get("name") or "",
                    "size": item.get("size") or "",
                }
            )
            remaining_items.append(
                {
                    "sku_code": item.get("sku_code") or "",
                    "name": item.get("name") or "",
                    "size": item.get("size") or "",
                    "actual_qty": item.get("actual_qty") or 0,
                }
            )
        client_label = "-"
        if latest and latest.agency:
            name = latest.agency.agn_name or latest.agency.fio_agn or str(latest.agency)
            client_label = _shorten_ip_name(name)
        role = get_request_role(self.request)
        can_submit = self._can_create(entries) and role == "storekeeper"
        boxes_data = (placement_act.payload or {}).get("act_boxes") if placement_act else []
        pallets_data = (placement_act.payload or {}).get("act_pallets") if placement_act else []
        ctx.update(
            {
                "order_id": order_id,
                "client_label": client_label,
                "status_label": _status_label_from_entry(status_entry) if status_entry else "-",
                "cabinet_url": resolve_cabinet_url(get_request_role(self.request)),
                "can_submit": can_submit,
                "act_exists": bool(placement_act),
                "items": display_items,
                "catalog_items": catalog_items,
                "remaining_items": remaining_items,
                "boxes_data": boxes_data,
                "pallets_data": pallets_data,
                "ok": kwargs.get("ok", False),
                "error": kwargs.get("error"),
            }
        )
        return ctx
