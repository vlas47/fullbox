from __future__ import annotations

from dataclasses import dataclass

from django.contrib import messages
from django.db.models import Prefetch, Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from audit.models import OrderAuditEntry, log_order_action
from employees.models import Employee
from logistics.models import LogisticsTrip, LogisticsTripOrder
from shipping.selectors import shipping_ui_status_label
from .models import Task, TaskAttachment


def _views():
    from . import views as todo_views

    return todo_views


def build_trip_context(task: Task) -> dict | None:
    trip_pk = _views()._extract_logistics_trip_pk(task.route)
    if trip_pk is None:
        return None
    trip = (
        LogisticsTrip.objects.select_related("assigned_logistician", "created_by")
        .prefetch_related(
            Prefetch(
                "orders",
                queryset=LogisticsTripOrder.objects.select_related(
                    "shipping_order",
                    "shipping_order__agency",
                    "shipping_order__marketplace",
                ).order_by("loading_sequence", "delivery_sequence", "id"),
            )
        )
        .filter(pk=trip_pk)
        .first()
    )
    if trip is None:
        return None
    trip_orders = list(trip.orders.all())
    order_numbers = [item.shipping_order.number for item in trip_orders if item.shipping_order and item.shipping_order.number]
    packing_payloads = _views()._trip_packing_payload_map(order_numbers)
    rows: list[dict] = []
    participants: list[str] = []
    if trip.assigned_logistician:
        participants.append(f"Логист: {trip.assigned_logistician.full_name}")
    storekeeper_name = task.assigned_to.full_name if task.assigned_to else ""
    if storekeeper_name:
        participants.append(f"Кладовщик: {storekeeper_name}")
    creator_name = getattr(task.created_by, "get_full_name", lambda: "")() or getattr(task.created_by, "username", "")
    if creator_name:
        participants.append(f"Постановщик: {creator_name}")
    for index, item in enumerate(trip_orders, start=1):
        order = item.shipping_order
        packing_payload = packing_payloads.get(order.number) or {}
        rows.append(
            {
                "route_position": index,
                "display_number": _views().format_order_number("shipping", order.number),
                "agency_name": _views()._short_agency_name(getattr(order.agency, "agn_name", "")) or "-",
                "destination_label": str(order.destination_warehouse or "").strip() or "-",
                "slot_date_label": order.slot_date.strftime("%d.%m.%Y") if order.slot_date else "-",
                "pallet_count": int(packing_payload.get("pallet_count") or 0),
                "box_count": int(packing_payload.get("delivered_box_count") or order.expected_boxes or 0),
                "status_label": shipping_ui_status_label(order),
                "comment": item.comment or "-",
            }
        )
    return {
        "trip": trip,
        "trip_display_number": _views()._trip_public_number(trip),
        "status_label": _views()._trip_status_label(trip),
        "participants": participants,
        "orders": rows,
        "total_pallets": sum(row["pallet_count"] for row in rows),
        "total_boxes": sum(row["box_count"] for row in rows),
        "vehicle_name": trip.vehicle_name or "-",
        "vehicle_number": trip.vehicle_number or "-",
        "driver_name": trip.driver_name or "-",
        "driver_phone": trip.driver_phone or "-",
        "route_comment": trip.route_comment or "-",
        "loading_comment": trip.loading_comment or "-",
    }


def send_receiving_to_warehouse(task: Task, request) -> bool:
    order_id = _views()._extract_receiving_order_id(task.route)
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
    if (
        status_value in {"warehouse", "on_warehouse"}
        or "склад" in status_label
        or "ожидании поставки" in status_label
    ):
        return True
    payload["status"] = "warehouse"
    payload["status_label"] = "В ожидании поставки товара"
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
            due_date=timezone.localtime() + _views().timedelta(days=1),
        )
    return True


def status_entry_from_list(entries):
    for entry in reversed(entries):
        payload = entry.payload or {}
        if entry.action == "status":
            return entry
        if payload.get("status") or payload.get("status_label") or payload.get("submit_action"):
            return entry
    return entries[-1] if entries else None


def act_entry_from_entries(entries, act_type: str):
    for entry in reversed(entries or []):
        if (entry.payload or {}).get("act") == act_type:
            return entry
    return None


def resolve_return_url(request) -> str:
    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if candidate and url_has_allowed_host_and_scheme(
        url=candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    cabinet_url = _views().resolve_cabinet_url(_views().get_request_role(request))
    if cabinet_url != "/":
        return cabinet_url
    return reverse("todo:list")


def can_create_receiving_act(task) -> tuple[bool, str | None, list]:
    order_id = _views()._extract_receiving_order_id(task.route)
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
    status_entry = status_entry_from_list(entries)
    payload = status_entry.payload or {} if status_entry else {}
    status_value = (payload.get("status") or payload.get("submit_action") or "").lower()
    status_label = (payload.get("status_label") or "").lower()
    if (
        status_value in {"warehouse", "on_warehouse"}
        or "склад" in status_label
        or "ожидании поставки" in status_label
    ):
        return True, order_id, entries
    return False, order_id, entries


def build_task_list_context(tasks) -> dict:
    return {"tasks": tasks}


def build_task_list_queryset(*, request, role):
    tasks = Task.objects.all()
    if role and not request.user.is_staff:
        tasks = tasks.filter(
            Q(assigned_to__role=role)
            | Q(observer__role=role)
            | Q(created_by=request.user)
        )
    return tasks


def can_access_task(*, request, task: Task, role=None) -> bool:
    if request.user.is_staff:
        return True
    current_role = role if role is not None else _views().get_request_role(request)
    if current_role:
        if task.assigned_to and task.assigned_to.role == current_role:
            return True
        if task.observer and task.observer.role == current_role:
            return True
    return task.created_by_id == request.user.id


def handle_task_create(request):
    form = _views().TaskForm(request.POST)
    if form.is_valid():
        task = form.save(commit=False)
        if request.user.is_authenticated:
            task.created_by = request.user
        task.save()
        messages.success(request, "Задача создана")
        return redirect(resolve_return_url(request)), form
    return None, form


def handle_task_update(request, task: Task):
    form = _views().TaskForm(request.POST, instance=task)
    if form.is_valid():
        form.save()
        messages.success(request, "Задача обновлена")
        return redirect(resolve_return_url(request)), form
    return None, form


def handle_task_delete(request, task: Task):
    task.delete()
    messages.success(request, "Задача удалена")
    return redirect("todo:list")


@dataclass
class TaskDetailState:
    can_complete: bool
    can_create_receiving_act: bool
    receiving_act_url: str | None
    can_create_placement_act: bool
    placement_act_url: str | None
    placement_act_exists: bool
    receiving_act_exists: bool
    receiving_act_label: str
    placement_act_label: str
    receiving_act_open_url: str | None
    placement_act_open_url: str | None
    can_send_act_to_client: bool
    can_edit: bool
    return_url: str
    order_context: dict | None
    trip_context: dict | None


def build_task_detail_state(*, request, task: Task) -> TaskDetailState:
    role = _views().get_request_role(request)
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
    receiving_act_exists = False
    receiving_act_label = ""
    placement_act_label = ""
    receiving_act_open_url = None
    placement_act_open_url = None
    can_send_act_to_client = False
    if can_complete and task.assigned_to and task.assigned_to.role == "storekeeper":
        can_create_receiving_act, order_id, entries = can_create_receiving_act_helper(task)
        if order_id:
            receiving_act = act_entry_from_entries(entries, "receiving")
            placement_act = act_entry_from_entries(entries, "placement")
            placement_act_exists = placement_act is not None
            if receiving_act and not placement_act:
                can_create_placement_act = True
                placement_act_url = f"/orders/receiving/{order_id}/placement/"
        if can_create_receiving_act and order_id:
            receiving_act_url = f"/orders/receiving/{order_id}/flow/"

    comment_form = _views().TaskCommentForm()
    attachment_form = _views().TaskAttachmentForm()
    can_edit = request.user.is_authenticated and task.created_by_id == request.user.id
    employee_role = _views().get_request_role(request)
    return_url = "/"
    if employee_role == "storekeeper":
        return_url = "/sklad/"
    elif employee_role == "manager":
        return_url = "/team-manager/"
    elif employee_role:
        return_url = f"/cabinet/{employee_role}/"

    trip_context = build_trip_context(task)
    order_context = None
    order_id = _views()._extract_receiving_order_id(task.route)
    if order_id:
        entries = list(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving")
            .select_related("user", "agency")
            .order_by("created_at")
        )
        if entries:
            latest = entries[-1]
            status_entry = _views().order_views._current_status_entry(entries)
            payload = _views().order_views._latest_payload_from_entries(entries)
            client_label = "-"
            if latest and latest.agency:
                name = latest.agency.agn_name or latest.agency.fio_agn or str(latest.agency)
                client_label = _views().order_views._shorten_ip_name(name)
            participants = []
            seen = set()
            for entry in entries:
                label = _views().order_views._actor_label(entry.user, entry.agency, client_view=False)
                if label in seen:
                    continue
                seen.add(label)
                participants.append(label)

            def find_act_entry(act_type, label_hint):
                entry = _views().order_views._act_entry_from_entries(entries, act_type)
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
                and not _views().order_views._is_done_status(status_entry)
            )

            order_context = {
                "order_id": order_id,
                "status_label": _views().order_views._status_label_from_entry(status_entry)
                if status_entry
                else "-",
                "responsible": _views().order_views._current_responsible_label(status_entry)
                if status_entry
                else "-",
                "client_label": client_label,
                "meta": {
                    "eta_at": _views().order_views._format_datetime_value(payload.get("eta_at")),
                    "expected_boxes": payload.get("expected_boxes"),
                    "place_type": _views().order_views._place_type_label(payload.get("place_type")),
                    "vehicle_number": payload.get("vehicle_number"),
                    "driver_phone": payload.get("driver_phone"),
                    "comment": payload.get("comment"),
                },
                "items": payload.get("items") or [],
                "participants": participants,
                "history": [
                    {
                        "created_at": entry.created_at,
                        "action_label": _views().order_views._history_action_label(entry),
                        "status_label": _views().order_views._status_label_from_entry(entry),
                        "description": _views().order_views._format_message_text(entry.description),
                        "actor_label": _views().order_views._history_actor_label(
                            entry,
                            client_view=False,
                            client_label=client_label,
                        ),
                    }
                    for entry in reversed(entries)
                    if entry.action != "comment"
                ],
            }
            if order_context["status_label"] in ("-", "", None):
                order_context["status_label"] = "В ожидании поставки товара"

    return TaskDetailState(
        can_complete=can_complete,
        can_create_receiving_act=can_create_receiving_act,
        receiving_act_url=receiving_act_url,
        can_create_placement_act=can_create_placement_act,
        placement_act_url=placement_act_url,
        placement_act_exists=placement_act_exists,
        receiving_act_exists=receiving_act_exists,
        receiving_act_label=receiving_act_label,
        placement_act_label=placement_act_label,
        receiving_act_open_url=receiving_act_open_url,
        placement_act_open_url=placement_act_open_url,
        can_send_act_to_client=can_send_act_to_client,
        can_edit=can_edit,
        return_url=return_url,
        order_context=order_context,
        trip_context=trip_context,
    )


def can_create_receiving_act_helper(task):
    return can_create_receiving_act(task)


def handle_task_detail_post(*, request, task: Task, state: TaskDetailState):
    role = _views().get_request_role(request)
    comment_form = _views().TaskCommentForm()
    attachment_form = _views().TaskAttachmentForm()
    action = request.POST.get("action")
    if action == "complete":
        if state.can_complete:
            sent = False
            if task.assigned_to and task.assigned_to.role == "manager":
                sent = send_receiving_to_warehouse(task, request)
            task.status = "done"
            task.save(update_fields=["status", "updated_at"])
            if sent:
                messages.success(request, "Заявка отправлена на склад")
            else:
                messages.success(request, "Задача отмечена как выполненная")
        else:
            messages.error(request, "Недостаточно прав для завершения задачи")
        return redirect("todo:detail", pk=task.pk), comment_form, attachment_form
    if action == "send_act_to_client":
        order_id = _views()._extract_receiving_order_id(task.route)
        if order_id:
            entries = list(
                OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving")
                .select_related("agency")
                .order_by("created_at")
            )
        else:
            entries = []
        if entries and role in {"manager", "head_manager", "director", "admin"}:
            if _views().order_views._send_act_to_client(order_id, entries, request.user):
                messages.success(request, "Акт отправлен клиенту")
            else:
                messages.error(request, "Не удалось отправить акт клиенту")
        else:
            messages.error(request, "Недостаточно прав для отправки акта")
        return redirect("todo:detail", pk=task.pk), comment_form, attachment_form
    if action == "rework":
        if not state.can_edit:
            messages.error(request, "Вернуть на доработку может только постановщик")
            return redirect("todo:detail", pk=task.pk), comment_form, attachment_form
        if task.status != "done":
            messages.error(request, "Вернуть на доработку можно только выполненную задачу")
            return redirect("todo:detail", pk=task.pk), comment_form, attachment_form
        comment_form = _views().TaskCommentForm(request.POST)
        if comment_form.is_valid():
            comment = comment_form.save(commit=False)
            comment.task = task
            if request.user.is_authenticated:
                comment.author = request.user
            comment.save()
            task.status = "in_progress"
            task.save(update_fields=["status", "updated_at"])
            messages.success(request, "Задача возвращена на доработку")
            return redirect("todo:detail", pk=task.pk), comment_form, attachment_form
        return None, comment_form, attachment_form
    if action == "comment":
        comment_form = _views().TaskCommentForm(request.POST)
        if comment_form.is_valid():
            comment = comment_form.save(commit=False)
            comment.task = task
            if request.user.is_authenticated:
                comment.author = request.user
            comment.save()
            messages.success(request, "Комментарий добавлен")
            return redirect("todo:detail", pk=task.pk), comment_form, attachment_form
        return None, comment_form, attachment_form
    if action == "attach":
        attachment_form = _views().TaskAttachmentForm(request.POST, request.FILES)
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
                return redirect("todo:detail", pk=task.pk), comment_form, attachment_form
        return None, comment_form, attachment_form
    messages.error(request, "Неизвестное действие")
    return None, comment_form, attachment_form


def build_task_detail_context(*, request, task: Task, comment_form=None, attachment_form=None) -> dict:
    state = build_task_detail_state(request=request, task=task)
    comments = task.comments.select_related("author").order_by("-created_at")
    attachments = task.attachments.select_related("uploaded_by").order_by("-uploaded_at")
    return {
        "task": task,
        "comments": comments,
        "attachments": attachments,
        "comment_form": comment_form or _views().TaskCommentForm(),
        "attachment_form": attachment_form or _views().TaskAttachmentForm(),
        "can_complete": state.can_complete,
        "can_create_receiving_act": state.can_create_receiving_act,
        "receiving_act_url": state.receiving_act_url,
        "can_create_placement_act": state.can_create_placement_act,
        "placement_act_url": state.placement_act_url,
        "placement_act_exists": state.placement_act_exists,
        "receiving_act_exists": state.receiving_act_exists,
        "receiving_act_label": state.receiving_act_label,
        "placement_act_label": state.placement_act_label,
        "receiving_act_open_url": state.receiving_act_open_url,
        "placement_act_open_url": state.placement_act_open_url,
        "can_send_act_to_client": state.can_send_act_to_client,
        "can_edit": state.can_edit,
        "return_url": state.return_url,
        "order_context": state.order_context,
        "trip_context": state.trip_context,
    }
