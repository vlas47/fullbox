from __future__ import annotations

from audit.models import log_order_action
from employees.access import get_request_role, is_staff_role
from sku.models import Agency

from .models import ShippingOrder
from .services import (
    close_logistician_task,
    close_manager_review_task,
    close_shipping_act_manager_task,
    close_storekeeper_task,
    create_pick_tasks as create_pick_tasks_for_order,
    ensure_manager_review_task,
    ensure_storekeeper_task,
    mark_storekeeper_task_in_progress,
    order_payload,
    release_order_reserves,
    reserve_order,
)

WRITE_ROLES = {
    "admin",
    "director",
    "head_manager",
    "manager",
    "storekeeper",
    "processing_head",
    "developer",
}
MANAGER_ROLES = {
    "admin",
    "director",
    "head_manager",
    "manager",
    "developer",
}
STOREKEEPER_ROLES = {
    "admin",
    "director",
    "storekeeper",
    "developer",
}
LOGISTICIAN_ROLES = {
    "admin",
    "director",
    "logistician",
    "developer",
}


def request_scope(request) -> tuple[str | None, str | None, Agency | None]:
    if not request.user.is_authenticated:
        return None, None, None
    role = get_request_role(request)
    if request.user.is_staff or is_staff_role(role):
        return "staff", role, None
    agency = Agency.objects.filter(portal_user=request.user, archived=False).first()
    if agency:
        return "client", "client", agency
    return None, role, None


def can_write(scope: str | None, role: str | None) -> bool:
    if scope == "client":
        return True
    if scope == "staff" and (role in WRITE_ROLES or role is None):
        return True
    return False


def is_manager_role(role: str | None) -> bool:
    return role in MANAGER_ROLES or role is None


def is_storekeeper_role(role: str | None) -> bool:
    return role in STOREKEEPER_ROLES or role is None


def is_logistician_role(role: str | None) -> bool:
    return role in LOGISTICIAN_ROLES or role is None


def can_edit_items(scope: str | None, role: str | None, order: ShippingOrder) -> bool:
    if order.is_closed():
        return False
    if scope == "client":
        return order.status == ShippingOrder.STATUS_DRAFT
    if scope == "staff" and is_manager_role(role):
        return order.status == ShippingOrder.STATUS_DRAFT
    return False


def can_edit_order_form(scope: str | None, role: str | None, order: ShippingOrder) -> bool:
    if order.is_closed():
        return False
    if scope == "staff" and is_manager_role(role):
        return order.status in {ShippingOrder.STATUS_DRAFT, ShippingOrder.STATUS_SUBMITTED}
    return False


def can_submit_for_approval(scope: str | None, role: str | None, order: ShippingOrder) -> bool:
    if order.status != ShippingOrder.STATUS_DRAFT or order.is_closed():
        return False
    if scope == "client":
        return True
    if scope == "staff" and is_manager_role(role):
        return True
    return False


def can_manager_approve(scope: str | None, role: str | None, order: ShippingOrder) -> bool:
    return (
        scope == "staff"
        and is_manager_role(role)
        and not order.is_closed()
        and order.status == ShippingOrder.STATUS_SUBMITTED
    )


def can_manager_reopen(scope: str | None, role: str | None, order: ShippingOrder) -> bool:
    return (
        scope == "staff"
        and is_manager_role(role)
        and not order.is_closed()
        and order.status in {ShippingOrder.STATUS_RESERVED, ShippingOrder.STATUS_STOREKEEPER_ACCEPTED}
    )


def can_storekeeper_accept(scope: str | None, role: str | None, order: ShippingOrder) -> bool:
    return (
        scope == "staff"
        and is_storekeeper_role(role)
        and not order.is_closed()
        and order.status == ShippingOrder.STATUS_RESERVED
    )


def can_storekeeper_pick(scope: str | None, role: str | None, order: ShippingOrder) -> bool:
    return (
        scope == "staff"
        and is_storekeeper_role(role)
        and not order.is_closed()
        and order.status == ShippingOrder.STATUS_STOREKEEPER_ACCEPTED
    )


def can_storekeeper_pack(scope: str | None, role: str | None, order: ShippingOrder) -> bool:
    return (
        scope == "staff"
        and is_storekeeper_role(role)
        and not order.is_closed()
        and order.status == ShippingOrder.STATUS_PICKING
    )


def can_storekeeper_manage_packing(
    scope: str | None,
    role: str | None,
    order: ShippingOrder,
    *,
    has_manageable_boxes: bool,
) -> bool:
    if not (
        scope == "staff"
        and is_storekeeper_role(role)
        and not order.is_closed()
        and order.status in {ShippingOrder.STATUS_PICKING, ShippingOrder.STATUS_PACKED}
    ):
        return False
    return has_manageable_boxes


def can_storekeeper_ship(scope: str | None, role: str | None, order: ShippingOrder) -> bool:
    return (
        scope == "staff"
        and is_storekeeper_role(role)
        and not order.is_closed()
        and order.status == ShippingOrder.STATUS_PACKED
    )


def can_cancel(scope: str | None, role: str | None, order: ShippingOrder) -> bool:
    if order.is_closed():
        return False
    if scope == "client":
        return order.status in {ShippingOrder.STATUS_DRAFT, ShippingOrder.STATUS_SUBMITTED}
    if scope == "staff" and is_manager_role(role):
        return True
    return False


def can_access_order(scope: str | None, client_agency: Agency | None, order: ShippingOrder) -> bool:
    if scope is None:
        return False
    if scope == "client" and client_agency is not None and order.agency_id != client_agency.id:
        return False
    return True


def _log_workflow_update(
    order: ShippingOrder,
    user,
    description: str,
    *,
    action: str = "update",
    extra: dict | None = None,
) -> None:
    log_order_action(
        action=action,
        order_id=order.number,
        order_type="shipping",
        user=user if getattr(user, "is_authenticated", False) else None,
        agency=order.agency,
        description=description,
        payload=order_payload(order, extra=extra),
    )


def submit_order_for_approval(order: ShippingOrder, user) -> None:
    order.status = ShippingOrder.STATUS_SUBMITTED
    order.save(update_fields=["status", "updated_at"])
    reserve_order(
        order,
        user,
        target_status=ShippingOrder.STATUS_SUBMITTED,
        log_description="Резерв выполнен автоматически при отправке клиентом",
    )
    ensure_manager_review_task(order, user)
    _log_workflow_update(
        order,
        user,
        "Заявка отправлена менеджеру на согласование",
        action="status",
    )


def approve_order_by_manager(order: ShippingOrder, user) -> None:
    reserve_order(order, user)
    close_manager_review_task(order)
    ensure_storekeeper_task(order, user)


def reopen_order_for_rework(order: ShippingOrder, user) -> None:
    release_order_reserves(order, user)
    close_manager_review_task(order)
    close_storekeeper_task(order)
    close_logistician_task(order)
    close_shipping_act_manager_task(order)


def accept_order_by_storekeeper(order: ShippingOrder, user) -> None:
    order.status = ShippingOrder.STATUS_STOREKEEPER_ACCEPTED
    order.save(update_fields=["status", "updated_at"])
    mark_storekeeper_task_in_progress(order)
    _log_workflow_update(
        order,
        user,
        "Кладовщик принял заявку в работу",
        action="status",
    )


def start_storekeeper_pick(
    order: ShippingOrder,
    user,
    *,
    requested_by_name: str = "",
    requested_by_role: str = "",
) -> list[str]:
    return create_pick_tasks_for_order(
        order,
        user,
        requested_by_name=requested_by_name,
        requested_by_role=requested_by_role,
    )


def cancel_order(order: ShippingOrder, user) -> None:
    release_order_reserves(order, user)
    order.status = ShippingOrder.STATUS_CANCELED
    order.save(update_fields=["status", "updated_at"])
    close_manager_review_task(order)
    close_storekeeper_task(order)
    close_logistician_task(order)
    close_shipping_act_manager_task(order)
    _log_workflow_update(
        order,
        user,
        "Заявка отменена",
        action="status",
    )
