from django import template
from django.db.models import F, Q
from django.utils import timezone

from employees.models import Employee

from ..models import Task

register = template.Library()

ROLE_LABELS = dict(Employee.ROLE_CHOICES)
ALL_ROLES_KEY = "__all__"
STATUS_ORDER = ["backlog", "in_progress", "blocked", "done"]
STATUS_LABELS = dict(Task.STATUS_CHOICES)


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


def _normalize_limit(limit, default=6):
    try:
        return int(limit)
    except (TypeError, ValueError):
        return default


@register.inclusion_tag("todo/_task_panel.html", takes_context=True)
def task_panel(context, role=None, limit=6):
    role_key = _resolve_role(context, role)
    tasks_qs = Task.objects.select_related(
        "assigned_to",
        "created_by",
        "observer",
    )
    if role_key and role_key != ALL_ROLES_KEY:
        tasks_qs = tasks_qs.filter(
            Q(assigned_to__role=role_key)
            | Q(observer__role=role_key)
            | Q(created_by__username=role_key)
        )
    limit_value = _normalize_limit(limit)
    today = timezone.localdate()
    done_qs = tasks_qs.filter(status="done")
    open_qs = tasks_qs.exclude(status="done")
    status_map = {
        "backlog": open_qs.filter(due_date__date__lt=today),
        "in_progress": open_qs.filter(due_date__date=today),
        "blocked": open_qs.filter(due_date__date__gt=today),
        "done": done_qs,
    }
    totals = {status: status_map[status].count() for status in status_map}
    columns = []
    for status in STATUS_ORDER:
        status_tasks = status_map[status].order_by(
            F("due_date").asc(nulls_last=True),
            "-created_at",
        )[:limit_value]
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
    }
