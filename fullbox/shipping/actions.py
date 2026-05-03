from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import ShippingOrder, ShippingOrderItem
from .services import release_order_reserves, reserve_order
from .workflow import (
    accept_order_by_storekeeper,
    approve_order_by_manager,
    cancel_order,
    reopen_order_for_rework,
    start_storekeeper_pick,
    submit_order_for_approval,
)


@dataclass(frozen=True)
class ShippingDetailActionPermissions:
    can_edit_items: bool
    can_submit_for_approval: bool
    can_manager_approve: bool
    can_manager_reopen: bool
    can_storekeeper_accept: bool
    can_storekeeper_pick: bool
    can_cancel: bool


@dataclass
class ShippingDetailActionResult:
    redirect_name: str = "shipping:detail"
    messages: list[tuple[str, str]] = field(default_factory=list)

    def add(self, level: str, text: str) -> None:
        self.messages.append((level, text))


def handle_shipping_detail_action(
    *,
    action: str,
    order: ShippingOrder,
    request,
    role: str | None,
    stock_rows_for_order: list[dict],
    permissions: ShippingDetailActionPermissions,
    parse_selected_stock_items: Callable,
    selected_stock_rows_with_boxes: Callable,
    parse_box_count_from_comment: Callable[[str | None], int],
    log_update: Callable,
) -> ShippingDetailActionResult:
    result = ShippingDetailActionResult()
    try:
        if action == "add_item":
            _handle_add_item(
                result=result,
                order=order,
                request=request,
                stock_rows_for_order=stock_rows_for_order,
                permissions=permissions,
                parse_selected_stock_items=parse_selected_stock_items,
                selected_stock_rows_with_boxes=selected_stock_rows_with_boxes,
                log_update=log_update,
            )
        elif action == "remove_item":
            _handle_remove_item(
                result=result,
                order=order,
                request=request,
                permissions=permissions,
                parse_box_count_from_comment=parse_box_count_from_comment,
                log_update=log_update,
            )
        elif action == "submit":
            if not permissions.can_submit_for_approval:
                result.add("error", "Отправка на согласование недоступна в текущем статусе.")
            elif order.status == ShippingOrder.STATUS_DRAFT:
                with transaction.atomic():
                    submit_order_for_approval(order, request.user)
                result.add("success", "Заявка отправлена менеджеру на согласование.")
        elif action == "reserve":
            if not permissions.can_manager_approve:
                result.add("error", "Согласование доступно только менеджеру.")
            else:
                approve_order_by_manager(order, request.user)
                result.add("success", "Заявка согласована менеджером и передана кладовщику.")
        elif action == "release_reserve":
            if not permissions.can_manager_reopen:
                result.add(
                    "error",
                    "Вернуть заявку на доработку может только менеджер после согласования.",
                )
            else:
                reopen_order_for_rework(order, request.user)
                result.add("success", "Заявка возвращена на доработку.")
        elif action == "accept_storekeeper":
            if not permissions.can_storekeeper_accept:
                result.add(
                    "error",
                    "Принять заявку в работу может только кладовщик после согласования менеджером.",
                )
            else:
                accept_order_by_storekeeper(order, request.user)
                result.add("success", "Заявка принята в работу складом.")
        elif action == "create_pick_tasks":
            if not permissions.can_storekeeper_pick:
                result.add(
                    "error",
                    "Создать задания ричтраку может только кладовщик после принятия заявки в работу.",
                )
            else:
                move_ids = start_storekeeper_pick(
                    order,
                    request.user,
                    requested_by_name=request.user.get_full_name() or request.user.username,
                    requested_by_role=role or "",
                )
                if move_ids:
                    result.add("success", f"Кладовщик передал задания ричтраку: {', '.join(move_ids)}.")
                else:
                    result.add("warning", "Нет позиций для создания заданий ричтраку.")
        elif action == "mark_packed":
            result.redirect_name = "shipping:packing"
        elif action == "cancel":
            if not permissions.can_cancel:
                result.add("error", "Отмена недоступна в текущем статусе/роли.")
            elif order.is_closed():
                result.add("warning", "Заявка уже закрыта.")
            else:
                cancel_order(order, request.user)
                result.add("success", "Заявка отменена.")
    except ValidationError as exc:
        result.add("error", "; ".join(exc.messages))
    except ValueError:
        result.add("error", "Некорректный формат количества.")
    return result


def _handle_add_item(
    *,
    result: ShippingDetailActionResult,
    order: ShippingOrder,
    request,
    stock_rows_for_order: list[dict],
    permissions: ShippingDetailActionPermissions,
    parse_selected_stock_items: Callable,
    selected_stock_rows_with_boxes: Callable,
    log_update: Callable,
) -> None:
    if not permissions.can_edit_items:
        result.add("error", "Редактирование позиций доступно только до согласования менеджером.")
        return
    selected_rows, selection_errors = parse_selected_stock_items(
        request,
        stock_rows_for_order,
        multiple=False,
    )
    if selection_errors:
        result.add("error", "; ".join(selection_errors))
        return
    _expanded_rows, added_box_count, _selection_errors = selected_stock_rows_with_boxes(
        request,
        stock_rows_for_order,
        multiple=False,
    )
    with transaction.atomic():
        row = selected_rows[0]
        item = ShippingOrderItem(**row)
        item.order = order
        item.save()
        order.expected_boxes = int(order.expected_boxes or 0) + int(max(added_box_count, 0))
        order.save(update_fields=["expected_boxes", "updated_at"])
        if order.status == ShippingOrder.STATUS_SUBMITTED:
            reserve_order(
                order,
                request.user,
                target_status=ShippingOrder.STATUS_SUBMITTED,
                log_description="Резерв обновлен после изменения позиций заявки",
            )
    log_update(order, request, f"Добавлена позиция {item.sku_code} ({item.qty_requested})")
    result.add("success", "Позиция добавлена.")


def _handle_remove_item(
    *,
    result: ShippingDetailActionResult,
    order: ShippingOrder,
    request,
    permissions: ShippingDetailActionPermissions,
    parse_box_count_from_comment: Callable[[str | None], int],
    log_update: Callable,
) -> None:
    if not permissions.can_edit_items:
        result.add("error", "Редактирование позиций доступно только до согласования менеджером.")
        return
    item_id = int(request.POST.get("item_id") or 0)
    item = order.items.filter(id=item_id).first()
    if not item:
        return
    with transaction.atomic():
        item_label = f"{item.sku_code}/{item.size or '-'}"
        removed_box_count = parse_box_count_from_comment(item.comment)
        item.delete()
        order.expected_boxes = max(int(order.expected_boxes or 0) - int(removed_box_count or 0), 0)
        order.save(update_fields=["expected_boxes", "updated_at"])
        if order.status == ShippingOrder.STATUS_SUBMITTED:
            if order.items.exists():
                reserve_order(
                    order,
                    request.user,
                    target_status=ShippingOrder.STATUS_SUBMITTED,
                    log_description="Резерв обновлен после изменения позиций заявки",
                )
            else:
                release_order_reserves(order, request.user)
    log_update(order, request, f"Удалена позиция {item_label}")
    result.add("success", "Позиция удалена.")
