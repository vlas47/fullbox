from django import template
import re
from datetime import datetime

from django.db.models import Q
from django.utils import timezone

from employees.models import Employee
from employees.access import get_employee_for_user

from audit.models import OrderAuditEntry

from ..models import Task

register = template.Library()

ROLE_LABELS = dict(Employee.ROLE_CHOICES)
ALL_ROLES_KEY = "__all__"
STATUS_ORDER = ["backlog", "in_progress", "blocked", "done"]
STATUS_LABELS = dict(Task.STATUS_CHOICES)
_RECEIVING_ROUTE_RE = re.compile(r"/orders/receiving/([^/]+)/")
_IP_PREFIX_RE = re.compile(r"\bиндивидуальный предприниматель\b", re.IGNORECASE)


def _shorten_ip_name(name: str) -> str:
    if not name:
        return "-"
    normalized = _IP_PREFIX_RE.sub("ИП", name)
    return " ".join(normalized.split()) or "-"


@register.filter
def short_name(full_name: str) -> str:
    if not full_name:
        return "-"
    parts = [part for part in full_name.split() if part]
    if not parts:
        return "-"
    surname = parts[0]
    initials = "".join(f"{part[0].upper()}." for part in parts[1:3] if part)
    return f"{surname} {initials}".strip()


def _resolve_role(context, role):
    if role in ("all", "*", "any"):
        return ALL_ROLES_KEY
    if role:
        return role
    if context.get("role"):
        return context["role"]
    request = context.get("request")
    if request and request.user.is_authenticated:
        return request.user.username
    return None


def _resolve_attention_employee(context):
    request = context.get("request")
    if not request:
        return None
    employee = get_employee_for_user(request.user)
    if employee:
        return employee
    name = request.session.get("employee_name") if hasattr(request, "session") else None
    if name:
        return Employee.objects.filter(full_name=name, is_active=True).first()
    return None


def _normalize_limit(limit, default=6):
    try:
        return int(limit)
    except (TypeError, ValueError):
        return default


def _extract_receiving_order_id(route: str | None) -> str | None:
    if not route:
        return None
    match = _RECEIVING_ROUTE_RE.search(route)
    if not match:
        return None
    return match.group(1)


def _is_receiving_sign_task(route: str | None) -> bool:
    return bool(route and "/orders/receiving/" in route and "/act/print" in route)


def _is_status_entry(entry) -> bool:
    if entry.action == "status":
        return True
    payload = entry.payload or {}
    return bool(payload.get("status") or payload.get("status_label") or payload.get("submit_action") or payload.get("act"))


def _status_label_from_entry(entry) -> str:
    payload = entry.payload or {}
    client_response = (payload.get("act_client_response") or "").lower()
    if client_response == "confirmed":
        return "Акт приемки подтвержден клиентом"
    if client_response == "dispute":
        return "Клиент заявил разногласия по акту приемки"
    if payload.get("act_sent"):
        return "Акт отправлен клиенту" if not payload.get("act_viewed") else "Выполнена"
    if payload.get("act_storekeeper_signed") and not payload.get("act_manager_signed"):
        return "Принято складом, акт приемки отправлен менеджеру"
    if payload.get("act") == "placement":
        state = (payload.get("act_state") or "closed").lower()
        return "Размещение на складе" if state == "open" else "Товар принят и размещен на складе"
    status_value = (payload.get("status") or payload.get("submit_action") or "").lower()
    status_label = (payload.get("status_label") or "").lower()
    if "взята в работу" in status_label:
        return "Взята в работу"
    if status_value in {"sent_unconfirmed", "send", "submitted"} or "подтверждени" in status_label:
        return "Ждет подтверждения"
    if status_value in {"warehouse", "on_warehouse"} or "ожидании поставки" in status_label or "на складе" in status_label:
        return "В ожидании поставки товара"
    return payload.get("status_label") or payload.get("status") or "-"


@register.inclusion_tag("todo/_task_panel.html", takes_context=True)
def task_panel(context, role=None, limit=6, show_meta=True, include_created_by=True):
    role_key = _resolve_role(context, role)
    tasks_qs = Task.objects.select_related(
        "assigned_to",
        "created_by",
        "observer",
    )
    if role_key and role_key != ALL_ROLES_KEY:
        role_filter = Q(assigned_to__role=role_key) | Q(observer__role=role_key)
        if include_created_by:
            role_filter |= Q(created_by__username=role_key)
        tasks_qs = tasks_qs.filter(role_filter)
    limit_value = _normalize_limit(limit)
    today = timezone.localdate()

    tasks = list(tasks_qs)
    receiving_by_order = {}
    other_tasks = []
    for task in tasks:
        order_id = _extract_receiving_order_id(task.route)
        if not order_id or _is_receiving_sign_task(task.route):
            other_tasks.append(task)
            continue
        existing = receiving_by_order.get(order_id)
        if not existing or task.updated_at > existing.updated_at:
            receiving_by_order[order_id] = task
    combined_tasks = other_tasks + list(receiving_by_order.values())

    done_tasks = [task for task in combined_tasks if task.status == "done"]
    open_tasks = [task for task in combined_tasks if task.status != "done"]
    open_routes = {task.route for task in open_tasks if task.route}
    done_tasks = [task for task in done_tasks if not task.route or task.route not in open_routes]

    def due_date_only(task):
        return task.due_date.date() if task.due_date else None

    status_map = {
        "backlog": [task for task in open_tasks if due_date_only(task) and due_date_only(task) < today],
        "in_progress": [task for task in open_tasks if due_date_only(task) == today],
        "blocked": [task for task in open_tasks if due_date_only(task) and due_date_only(task) > today],
        "done": done_tasks,
    }
    totals = {status: len(status_map[status]) for status in status_map}
    columns = []
    for status in STATUS_ORDER:
        if status == "done":
            status_tasks = sorted(
                status_map[status],
                key=lambda task: (task.updated_at, task.created_at),
                reverse=True,
            )[:limit_value]
        else:
            max_dt = datetime.max.replace(tzinfo=timezone.get_current_timezone())
            status_tasks = sorted(
                status_map[status],
                key=lambda task: (
                    task.due_date or max_dt,
                    -task.created_at.timestamp(),
                ),
            )[:limit_value]
        columns.append(
            {
                "status": status,
                "label": STATUS_LABELS.get(status, status),
                "count": totals.get(status, 0),
                "tasks": status_tasks,
            }
        )
    tasks = [task for column in columns for task in column["tasks"]]
    order_ids = {}
    for task in tasks:
        order_id = _extract_receiving_order_id(task.route)
        if order_id:
            order_ids[order_id] = True
    status_by_order = {}
    client_by_order = {}
    if order_ids:
        entries = (
            OrderAuditEntry.objects.filter(order_type="receiving", order_id__in=list(order_ids))
            .select_related("agency")
            .order_by("order_id", "-created_at")
        )
        for entry in entries:
            if entry.order_id not in client_by_order:
                if entry.agency:
                    name = entry.agency.agn_name or entry.agency.fio_agn or str(entry.agency)
                    client_by_order[entry.order_id] = _shorten_ip_name(name)
                else:
                    client_by_order[entry.order_id] = "-"
            if entry.order_id in status_by_order:
                continue
            if not _is_status_entry(entry):
                continue
            status_by_order[entry.order_id] = _status_label_from_entry(entry)
    for order_id in order_ids:
        label = status_by_order.get(order_id)
        if not label or label == "-":
            status_by_order[order_id] = "В ожидании поставки товара"
    for task in tasks:
        order_id = _extract_receiving_order_id(task.route)
        task.order_status_label = status_by_order.get(order_id) if order_id else None
        task.order_client_label = client_by_order.get(order_id) if order_id else None
    role_label = None
    if role_key == ALL_ROLES_KEY:
        role_label = "Все роли"
    elif role_key:
        role_label = ROLE_LABELS.get(role_key)
    attention_employee = _resolve_attention_employee(context)
    return {
        "task_panel_columns": columns,
        "task_panel_stats": [
            {
                "status": status,
                "label": STATUS_LABELS.get(status, status),
                "count": totals.get(status, 0),
            }
            for status in STATUS_ORDER
        ],
        "task_panel_total": sum(totals.values()),
        "task_panel_role": role_key,
        "task_panel_role_label": role_label,
        "task_panel_show_meta": show_meta,
        "task_panel_attention_employee_id": attention_employee.id if attention_employee else None,
    }
