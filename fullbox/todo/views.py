import re
from datetime import timedelta

from django.contrib import messages
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.db.models import Q

from audit.models import OrderAuditEntry, log_order_action
from employees.models import Employee
from employees.access import get_request_role
from orders import views as order_views
from .forms import TaskAttachmentForm, TaskCommentForm, TaskForm
from .models import Task, TaskAttachment


_RECEIVING_ROUTE_RE = re.compile(r"/orders/receiving/([^/]+)/")


def _extract_receiving_order_id(route: str | None) -> str | None:
    if not route:
        return None
    match = _RECEIVING_ROUTE_RE.search(route)
    if not match:
        return None
    return match.group(1)


def _send_receiving_to_warehouse(task, request) -> bool:
    order_id = _extract_receiving_order_id(task.route)
    if not order_id:
        return False
    latest = (
        OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving")
        .select_related("agency")
        .order_by("-created_at")
        .first()
    )
    if not latest:
        return False
    payload = dict(latest.payload or {})
    status_value = (payload.get("status") or payload.get("submit_action") or "").lower()
    status_label = (payload.get("status_label") or "").lower()
    if status_value in {"warehouse", "on_warehouse"} or "склад" in status_label:
        return True
    payload["status"] = "warehouse"
    payload["status_label"] = "На складе"
    log_order_action(
        "status",
        order_id=order_id,
        order_type="receiving",
        user=request.user if request.user.is_authenticated else None,
        agency=latest.agency,
        description="Подтверждено и отправлено на склад",
        payload=payload,
    )
    Task.objects.filter(
        route=f"/orders/receiving/{order_id}/",
        assigned_to__role="manager",
    ).exclude(status="done").update(status="done")
    storekeeper = (
        Employee.objects.filter(role="storekeeper", is_active=True)
        .order_by("full_name")
        .first()
    )
    if storekeeper:
        observer = Employee.objects.filter(
            user=request.user, is_active=True
        ).first()
        description = f"Клиент: {latest.agency.agn_name or latest.agency.inn or latest.agency.id}"
        Task.objects.create(
            title=f"Принять заявку на приемку товара №{order_id}",
            description=description,
            route=f"/orders/receiving/{order_id}/",
            assigned_to=storekeeper,
            observer=observer,
            created_by=request.user if request.user.is_authenticated else None,
            due_date=timezone.localtime() + timedelta(days=1),
        )
    return True


def _status_entry_from_list(entries):
    for entry in reversed(entries):
        payload = entry.payload or {}
        if entry.action == "status":
            return entry
        if payload.get("status") or payload.get("status_label") or payload.get("submit_action"):
            return entry
    return entries[-1] if entries else None


def _act_entry_from_entries(entries, act_type: str):
    for entry in reversed(entries or []):
        if (entry.payload or {}).get("act") == act_type:
            return entry
    return None


def _can_create_receiving_act(task) -> tuple[bool, str | None, list]:
    order_id = _extract_receiving_order_id(task.route)
    if not order_id:
        return False, None, []
    entries = list(
        OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving")
        .select_related("agency")
        .order_by("created_at")
    )
    if not entries:
        return False, order_id, []
    if any((entry.payload or {}).get("act") == "receiving" for entry in entries):
        return False, order_id, entries
    status_entry = _status_entry_from_list(entries)
    payload = status_entry.payload or {} if status_entry else {}
    status_value = (payload.get("status") or payload.get("submit_action") or "").lower()
    status_label = (payload.get("status_label") or "").lower()
    if status_value in {"warehouse", "on_warehouse"} or "склад" in status_label:
        return True, order_id, entries
    return False, order_id, entries




def task_list(request):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    role = get_request_role(request)
    if not role and not request.user.is_staff:
        return redirect("/")
    tasks = Task.objects.all()
    if role and not request.user.is_staff:
        tasks = tasks.filter(
            Q(assigned_to__role=role)
            | Q(observer__role=role)
            | Q(created_by=request.user)
        )
    return render(request, "todo/task_list.html", {"tasks": tasks})


def task_create(request):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    if request.method == "POST":
        form = TaskForm(request.POST)
        if form.is_valid():
            task = form.save(commit=False)
            if request.user.is_authenticated:
                task.created_by = request.user
            task.save()
            messages.success(request, "Задача создана")
            return redirect("todo:list")
    else:
        form = TaskForm()
    return render(request, "todo/task_form.html", {"form": form, "title": "Новая задача"})


def task_update(request, pk):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    task = get_object_or_404(Task, pk=pk)
    if not (request.user.is_authenticated and task.created_by_id == request.user.id):
        messages.error(request, "Редактировать задачу может только постановщик")
        return redirect("todo:detail", pk=task.pk)
    if request.method == "POST":
        form = TaskForm(request.POST, instance=task)
        if form.is_valid():
            form.save()
            messages.success(request, "Задача обновлена")
            return redirect("todo:list")
    else:
        form = TaskForm(instance=task)
    return render(request, "todo/task_form.html", {"form": form, "title": f"Редактирование: {task.title}"})


def task_delete(request, pk):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    task = get_object_or_404(Task, pk=pk)
    if request.method == "POST":
        task.delete()
        messages.success(request, "Задача удалена")
        return redirect("todo:list")
    return render(request, "todo/task_delete_confirm.html", {"task": task})


def task_detail(request, pk):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    task = get_object_or_404(Task, pk=pk)
    role = get_request_role(request)
    if not request.user.is_staff:
        allowed = False
        if role:
            if task.assigned_to and task.assigned_to.role == role:
                allowed = True
            if task.observer and task.observer.role == role:
                allowed = True
        if task.created_by_id == request.user.id:
            allowed = True
        if not allowed:
            return redirect("/")
    can_complete = bool(
        role
        and task.assigned_to
        and role == task.assigned_to.role
    )
    can_create_receiving_act = False
    receiving_act_url = None
    can_create_placement_act = False
    placement_act_url = None
    placement_act_exists = False
    if can_complete and task.assigned_to and task.assigned_to.role == "storekeeper":
        can_create_receiving_act, order_id, entries = _can_create_receiving_act(task)
        if order_id:
            receiving_act = _act_entry_from_entries(entries, "receiving")
            placement_act = _act_entry_from_entries(entries, "placement")
            placement_act_exists = placement_act is not None
            if receiving_act and not placement_act:
                can_create_placement_act = True
                placement_act_url = f"/orders/receiving/{order_id}/placement/"
        if can_create_receiving_act and order_id:
            receiving_act_url = f"/orders/receiving/{order_id}/act/"
    comment_form = TaskCommentForm()
    attachment_form = TaskAttachmentForm()
    can_edit = request.user.is_authenticated and task.created_by_id == request.user.id
    employee_role = get_request_role(request)
    return_url = "/"
    if employee_role == "storekeeper":
        return_url = "/sklad/"
    elif employee_role == "manager":
        return_url = "/team-manager/"
    elif employee_role:
        return_url = f"/cabinet/{employee_role}/"

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "complete":
            if can_complete:
                sent = False
                if task.assigned_to and task.assigned_to.role == "manager":
                    sent = _send_receiving_to_warehouse(task, request)
                task.status = "done"
                task.save(update_fields=["status", "updated_at"])
                if sent:
                    messages.success(request, "Заявка отправлена на склад")
                else:
                    messages.success(request, "Задача отмечена как выполненная")
            else:
                messages.error(request, "Недостаточно прав для завершения задачи")
            return redirect("todo:detail", pk=task.pk)
        if action == "send_act_to_client":
            order_id = _extract_receiving_order_id(task.route)
            if order_id:
                entries = list(
                    OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving")
                    .select_related("agency")
                    .order_by("created_at")
                )
            else:
                entries = []
            if entries and role in {"manager", "head_manager", "director", "admin"}:
                if order_views._send_act_to_client(order_id, entries, request.user):
                    messages.success(request, "Акт отправлен клиенту")
                else:
                    messages.error(request, "Не удалось отправить акт клиенту")
            else:
                messages.error(request, "Недостаточно прав для отправки акта")
            return redirect("todo:detail", pk=task.pk)
        if action == "rework":
            if not can_edit:
                messages.error(request, "Вернуть на доработку может только постановщик")
                return redirect("todo:detail", pk=task.pk)
            if task.status != "done":
                messages.error(request, "Вернуть на доработку можно только выполненную задачу")
                return redirect("todo:detail", pk=task.pk)
            comment_form = TaskCommentForm(request.POST)
            if comment_form.is_valid():
                comment = comment_form.save(commit=False)
                comment.task = task
                if request.user.is_authenticated:
                    comment.author = request.user
                comment.save()
                task.status = "in_progress"
                task.save(update_fields=["status", "updated_at"])
                messages.success(request, "Задача возвращена на доработку")
                return redirect("todo:detail", pk=task.pk)
        elif action == "comment":
            comment_form = TaskCommentForm(request.POST)
            if comment_form.is_valid():
                comment = comment_form.save(commit=False)
                comment.task = task
                if request.user.is_authenticated:
                    comment.author = request.user
                comment.save()
                messages.success(request, "Комментарий добавлен")
                return redirect("todo:detail", pk=task.pk)
        elif action == "attach":
            attachment_form = TaskAttachmentForm(request.POST, request.FILES)
            if attachment_form.is_valid():
                files = request.FILES.getlist("files")
                if not files:
                    messages.error(request, "Файлы не выбраны")
                else:
                    for uploaded_file in files:
                        TaskAttachment.objects.create(
                            task=task,
                            uploaded_by=request.user if request.user.is_authenticated else None,
                            file=uploaded_file,
                        )
                    messages.success(request, "Файлы загружены")
                    return redirect("todo:detail", pk=task.pk)
        else:
            messages.error(request, "Неизвестное действие")

    comments = task.comments.select_related("author").order_by("-created_at")
    attachments = task.attachments.select_related("uploaded_by").order_by("-uploaded_at")
    order_context = None
    receiving_act_exists = False
    placement_act_exists = False
    receiving_act_label = ""
    placement_act_label = ""
    receiving_act_open_url = None
    placement_act_open_url = None
    can_send_act_to_client = False
    order_id = _extract_receiving_order_id(task.route)
    if order_id:
        entries = list(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving")
            .select_related("user", "agency")
            .order_by("created_at")
        )
        if entries:
            latest = entries[-1]
            status_entry = order_views._current_status_entry(entries)
            payload = order_views._latest_payload_from_entries(entries)
            client_label = "-"
            if latest and latest.agency:
                name = latest.agency.agn_name or latest.agency.fio_agn or str(latest.agency)
                client_label = order_views._shorten_ip_name(name)
            participants = []
            seen = set()
            for entry in entries:
                label = order_views._actor_label(entry.user, entry.agency, client_view=False)
                if label in seen:
                    continue
                seen.add(label)
                participants.append(label)
            def find_act_entry(act_type, label_hint):
                entry = order_views._act_entry_from_entries(entries, act_type)
                if entry:
                    return entry
                for candidate in reversed(entries):
                    label = ((candidate.payload or {}).get("act_label") or "").lower()
                    if label_hint in label:
                        return candidate
                return None

            receiving_entry = find_act_entry("receiving", "акт приемки")
            placement_entry = find_act_entry("placement", "акт размещения")
            receiving_act_exists = bool(receiving_entry)
            placement_act_exists = bool(placement_entry)
            receiving_act_label = (
                (receiving_entry.payload or {}).get("act_label") if receiving_entry else ""
            ) or "Акт приемки"
            placement_act_label = (
                (placement_entry.payload or {}).get("act_label") if placement_entry else ""
            ) or "Акт размещения"
            receiving_act_open_url = f"/orders/receiving/{order_id}/act/"
            placement_act_open_url = f"/orders/receiving/{order_id}/placement/"
            can_send_act_to_client = bool(
                role in {"manager", "head_manager", "director", "admin"}
                and receiving_entry
                and placement_entry
                and not order_views._is_done_status(status_entry)
            )

            order_context = {
                "order_id": order_id,
                "status_label": order_views._status_label_from_entry(status_entry)
                if status_entry
                else "-",
                "responsible": order_views._current_responsible_label(status_entry)
                if status_entry
                else "-",
                "client_label": client_label,
                "meta": {
                    "eta_at": order_views._format_datetime_value(payload.get("eta_at")),
                    "expected_boxes": payload.get("expected_boxes"),
                    "place_type": order_views._place_type_label(payload.get("place_type")),
                    "vehicle_number": payload.get("vehicle_number"),
                    "driver_phone": payload.get("driver_phone"),
                    "comment": payload.get("comment"),
                },
                "items": payload.get("items") or [],
                "participants": participants,
                "history": [
                    {
                        "created_at": entry.created_at,
                        "action_label": order_views._history_action_label(entry),
                        "status_label": order_views._status_label_from_entry(entry),
                        "description": order_views._format_message_text(entry.description),
                        "actor_label": order_views._history_actor_label(
                            entry,
                            client_view=False,
                            client_label=client_label,
                        ),
                    }
                    for entry in reversed(entries)
                    if entry.action != "comment"
                ],
            }
    context = {
        "task": task,
        "comments": comments,
        "attachments": attachments,
        "comment_form": comment_form,
        "attachment_form": attachment_form,
        "can_complete": can_complete,
        "can_create_receiving_act": can_create_receiving_act,
        "receiving_act_url": receiving_act_url,
        "can_create_placement_act": can_create_placement_act,
        "placement_act_url": placement_act_url,
        "placement_act_exists": placement_act_exists,
        "receiving_act_exists": receiving_act_exists,
        "receiving_act_label": receiving_act_label,
        "placement_act_label": placement_act_label,
        "receiving_act_open_url": receiving_act_open_url,
        "placement_act_open_url": placement_act_open_url,
        "can_send_act_to_client": can_send_act_to_client,
        "can_edit": can_edit,
        "return_url": return_url,
        "order_context": order_context,
    }
    return render(request, "todo/task_detail.html", context)
