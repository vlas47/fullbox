from django import template
from django.db.models import F, Q
from django.utils import timezone

from employees.models import Employee
from employees.access import get_employee_for_user

from ..models import Task

register = template.Library()

ROLE_LABELS = dict(Employee.ROLE_CHOICES)
ALL_ROLES_KEY = "__all__"
STATUS_ORDER = ["backlog", "in_progress", "blocked", "done"]
STATUS_LABELS = dict(Task.STATUS_CHOICES)


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
    done_qs = tasks_qs.filter(status="done")
    open_qs = tasks_qs.exclude(status="done")
    open_routes = open_qs.exclude(route="").values_list("route", flat=True)
    done_qs = done_qs.exclude(route__in=open_routes)
    status_map = {
        "backlog": open_qs.filter(due_date__date__lt=today),
        "in_progress": open_qs.filter(due_date__date=today),
        "blocked": open_qs.filter(due_date__date__gt=today),
        "done": done_qs,
    }
    totals = {status: status_map[status].count() for status in status_map}
    columns = []
    for status in STATUS_ORDER:
        if status == "done":
            order_by = ["-updated_at", "-created_at"]
        else:
            order_by = [F("due_date").asc(nulls_last=True), "-created_at"]
        status_tasks = status_map[status].order_by(*order_by)[:limit_value]
        columns.append(
            {
                "status": status,
                "label": STATUS_LABELS.get(status, status),
                "count": totals.get(status, 0),
                "tasks": status_tasks,
            }
        )
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
