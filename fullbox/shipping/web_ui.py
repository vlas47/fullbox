from __future__ import annotations

"""Shipping UI helpers and request handlers."""

from collections import defaultdict
import json
import logging
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_http_methods

from audit.models import log_order_action
from employees.access import resolve_cabinet_url
from fullbox.order_numbers import format_order_number
from labels.utils import build_print_status_snapshot, refresh_print_agent_printers
from sklad.services.stock_availability import StockAvailabilityService
from sklad.services.warehouse_stock_rows import snapshot_stock_rows
from sku.models import Agency

from .actions import ShippingDetailActionPermissions, handle_shipping_detail_action
from .dispatch import (
    shipping_dispatch_stage as _shipping_dispatch_stage,
    sign_dispatch_act_logistician,
    sign_dispatch_act_manager,
)
from .forms import (
    NEXT_DAY_DEADLINE_ERROR,
    NEXT_DAY_DEADLINE_HOUR,
    ShippingTransportNoteForm,
    WORKDAY_END_HOUR,
    WORKDAY_HOURS_ERROR,
    WORKDAY_START_HOUR,
    ShippingOrderForm,
)
from .marketplace_warehouses import load_marketplace_warehouse_catalog
from .models import ShippingOrder, ShippingOrderAttachment, ShippingOrderItem
from .packing import (
    _shipping_box_row_key,
    _shipping_boxes_from_packing_payload,
    _shipping_delivered_boxes,
    _shipping_manageable_packing_boxes,
    _shipping_packing_initial_state,
    _shipping_packing_summary,
    shipping_packing_slips_data,
    save_shipping_packing,
)
from .return_act import render_return_act_doc, return_act_doc_filename
from .selectors import (
    active_shipping_attachments as _active_shipping_attachments,
    build_shipping_detail_context,
    build_shipping_dispatch_context,
    shipping_attachment_names as _shipping_attachment_names,
    shipping_reachtruck_metrics as _shipping_reachtruck_metrics,
    shipping_ui_status_label as _shipping_ui_status_label,
)
from .services import (
    build_shipping_return_act_response,
    build_shipping_transport_note_docx_response,
    build_shipping_transport_note_pdf_response,
    build_shipping_detail_page_context,
    build_shipping_list_page_context,
    download_shipping_attachment,
    ensure_manager_review_task,
    handle_shipping_dispatch_act_request,
    handle_shipping_dispatch_sign_logistician_request,
    handle_shipping_dispatch_sign_manager_request,
    handle_shipping_create_request,
    handle_shipping_documents_request,
    handle_shipping_packing_request,
    handle_shipping_packing_slips_request,
    handle_shipping_packing_slips_status_request,
    next_shipping_number,
    order_payload,
    reserve_order,
    shipping_pick_readiness,
    shipping_available_items,
)
from .transport_note import (
    build_transport_note_preview_context,
    can_access_transport_note,
    get_or_create_transport_note,
    render_transport_note_docx,
    render_transport_note_pdf,
    transport_note_docx_filename,
    transport_note_filename,
)
from .workflow import (
    can_access_order as _can_access_order,
    can_cancel as _can_cancel,
    can_edit_items as _can_edit_items,
    can_edit_order_form as _can_edit_order_form,
    can_manager_approve as _can_manager_approve,
    can_manager_reopen as _can_manager_reopen,
    can_storekeeper_manage_packing,
    can_storekeeper_accept as _can_storekeeper_accept,
    can_storekeeper_pack as _can_storekeeper_pack,
    can_storekeeper_pick as _can_storekeeper_pick,
    can_submit_for_approval as _can_submit_for_approval,
    can_write as _can_write,
    is_logistician_role as _is_logistician_role,
    is_manager_role as _is_manager_role,
    is_storekeeper_role as _is_storekeeper_role,
    request_scope as _request_scope,
)

_BOX_COUNT_COMMENT_RE = re.compile(r"коробов:\s*(\d+)", re.IGNORECASE)
logger = logging.getLogger(__name__)


def _to_int(value) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _parse_box_count_from_comment(comment: str | None) -> int:
    match = _BOX_COUNT_COMMENT_RE.search(str(comment or ""))
    if not match:
        return 0
    return _to_int(match.group(1))


def _display_shipping_number(number: str | None) -> str:
    return format_order_number("shipping", number)


def _stock_row_identity(*, sku_code: str, name: str, size: str, barcode: str, goods_type: str) -> tuple[str, str, str, str, str]:
    return (
        str(sku_code or "").strip(),
        str(name or "").strip(),
        str(size or "").strip(),
        str(barcode or "").strip(),
        str(goods_type or "").strip(),
    )


def _selected_box_values_from_request(request) -> dict[str, str]:
    all_keys = request.POST.getlist("stock_key_all[]") or request.POST.getlist("stock_key_all")
    all_boxes = request.POST.getlist("stock_boxes[]") or request.POST.getlist("stock_boxes")
    values: dict[str, str] = {}
    for index, raw_key in enumerate(all_keys):
        key = str(raw_key or "").strip()
        if not key or key in values:
            continue
        values[key] = str(all_boxes[index] if index < len(all_boxes) else "").strip()
    return values


def _selected_box_values_for_order(order: ShippingOrder, stock_rows: list[dict]) -> dict[str, str]:
    rows_by_identity: dict[tuple[str, str, str, str, str], list[dict]] = defaultdict(list)
    for row in stock_rows:
        rows_by_identity[
            _stock_row_identity(
                sku_code=row.get("sku_code") or "",
                name=row.get("name") or "",
                size=row.get("size") or "",
                barcode=row.get("barcode") or "",
                goods_type=row.get("goods_type") or "",
            )
        ].append(row)

    selected_by_key: dict[str, str] = {}
    mixed_group_boxes: dict[str, int] = {}
    for item in order.items.order_by("id"):
        matches = rows_by_identity.get(
            _stock_row_identity(
                sku_code=item.sku_code,
                name=item.name,
                size=item.size,
                barcode=item.barcode,
                goods_type=item.goods_type,
            ),
            [],
        )
        if not matches:
            continue
        parsed_boxes = _parse_box_count_from_comment(item.comment)
        selected_row = None
        for row in matches:
            box_qty = int(row.get("box_qty") or 0)
            if box_qty <= 0:
                continue
            candidate_boxes = parsed_boxes
            if candidate_boxes <= 0 and int(item.qty_requested or 0) % box_qty == 0:
                candidate_boxes = int(item.qty_requested or 0) // box_qty
            if candidate_boxes <= 0:
                continue
            if candidate_boxes * box_qty == int(item.qty_requested or 0):
                parsed_boxes = candidate_boxes
                selected_row = row
                break
            if selected_row is None:
                parsed_boxes = candidate_boxes
                selected_row = row
        if selected_row is None or parsed_boxes <= 0:
            continue
        mixed_group = str(selected_row.get("mixed_group") or "").strip()
        if selected_row.get("is_mixed_box") and mixed_group:
            mixed_group_boxes[mixed_group] = max(int(mixed_group_boxes.get(mixed_group) or 0), int(parsed_boxes))
        else:
            selected_by_key[str(selected_row["key"])] = str(int(parsed_boxes))

    for row in stock_rows:
        mixed_group = str(row.get("mixed_group") or "").strip()
        if row.get("is_mixed_box") and mixed_group and mixed_group in mixed_group_boxes:
            selected_by_key[str(row["key"])] = str(int(mixed_group_boxes[mixed_group]))
    return selected_by_key


def _with_selected_boxes(stock_rows: list[dict], selected_boxes: dict[str, str] | None = None) -> list[dict]:
    selected_boxes = selected_boxes or {}
    prepared: list[dict] = []
    for row in stock_rows:
        cloned = dict(row)
        selected_value = selected_boxes.get(str(row.get("key") or ""), "0")
        cloned["selected_boxes"] = str(selected_value or "0")
        prepared.append(cloned)
    return prepared


def _order_box_count(order: ShippingOrder) -> int:
    expected = int(order.expected_boxes or 0)
    if expected > 0:
        return expected
    return sum(_parse_box_count_from_comment(item.comment) for item in order.items.all())


def _can_storekeeper_manage_packing(scope: str | None, role: str | None, order: ShippingOrder) -> bool:
    return can_storekeeper_manage_packing(
        scope,
        role,
        order,
        has_manageable_boxes=bool(_shipping_manageable_packing_boxes(order)),
    )


def _save_shipping_attachments(order: ShippingOrder, request) -> list[str]:
    uploaded_names: list[str] = []
    for uploaded_file in request.FILES.getlist("documents"):
        if not uploaded_file or not getattr(uploaded_file, "name", ""):
            continue
        attachment = ShippingOrderAttachment.objects.create(
            order=order,
            uploaded_by=request.user if request.user.is_authenticated else None,
            file=uploaded_file,
        )
        uploaded_names.append(attachment.filename)
    return uploaded_names


def _log_update(order: ShippingOrder, request, description: str, *, action: str = "update", extra: dict | None = None) -> None:
    log_order_action(
        action=action,
        order_id=order.number,
        order_type="shipping",
        user=request.user if request.user.is_authenticated else None,
        agency=order.agency,
        description=description,
        payload=order_payload(order, extra=extra),
    )


def _normalize_stock_key(sku_code: str, size: str, goods_type: str) -> tuple[str, str, str]:
    return (
        str(sku_code or "").strip().lower(),
        str(size or "").strip().lower(),
        StockAvailabilityService.normalize_goods_type(goods_type),
    )


def _compose_picker_key(
    *,
    sku_id: int,
    sku_code: str,
    name: str,
    size: str,
    goods_type: str,
    box_qty: int,
    barcode: str,
    mixed_group: str,
) -> str:
    return "|".join(
        [
            str(int(sku_id or 0)),
            str(sku_code or "").strip(),
            str(size or "").strip(),
            str(goods_type or "").strip(),
            str(int(box_qty or 0)),
            str(barcode or "").strip(),
            str(name or "").strip(),
            str(mixed_group or "").strip(),
        ]
    )


def _shipping_stock_picker_rows(
    agency: Agency | None,
    *,
    exclude_order: ShippingOrder | None = None,
) -> list[dict]:
    if not agency:
        return []
    rows = snapshot_stock_rows(agency=agency, require_box=True)
    processing_reserve_map, _ = StockAvailabilityService.build_processing_reserve_maps(agency)
    shipping_reserve_map, _ = StockAvailabilityService.build_shipping_reserve_maps(
        agency,
        exclude_shipping_order_id=str(exclude_order.number or "").strip() if exclude_order else None,
    )
    processing_left = {key: int(value or 0) for key, value in processing_reserve_map.items()}
    shipping_left = {key: int(value or 0) for key, value in shipping_reserve_map.items()}

    def _reserve_sort_key(row: dict):
        return (
            _normalize_stock_key(row.get("sku") or "", row.get("size") or "", row.get("goods_type") or ""),
            row.get("updated_at") or timezone.localtime(),
            str(row.get("order_id") or ""),
            str(row.get("box_code") or ""),
            str(row.get("pallet_code") or ""),
            int(row.get("id") or 0),
        )

    row_available_qty: dict[int, int] = {}
    for row in sorted(rows, key=_reserve_sort_key):
        row_id = int(row.get("id") or 0)
        row_qty = int(row.get("qty") or 0)
        if row_qty <= 0:
            row_available_qty[row_id] = 0
            continue
        reserve_key = _normalize_stock_key(row.get("sku") or "", row.get("size") or "", row.get("goods_type") or "")
        processing_used = min(row_qty, int(processing_left.get(reserve_key, 0)))
        qty_after_processing = max(row_qty - processing_used, 0)
        shipping_used = min(qty_after_processing, int(shipping_left.get(reserve_key, 0)))
        available_qty = max(qty_after_processing - shipping_used, 0)
        row_available_qty[row_id] = int(available_qty)
        if processing_used > 0:
            processing_left[reserve_key] = max(int(processing_left.get(reserve_key, 0)) - processing_used, 0)
        if shipping_used > 0:
            shipping_left[reserve_key] = max(int(shipping_left.get(reserve_key, 0)) - shipping_used, 0)

    box_lines: dict[str, dict[tuple[int, str, str, str, str, str], int]] = defaultdict(lambda: defaultdict(int))
    box_available_lines: dict[str, dict[tuple[int, str, str, str, str, str], int]] = defaultdict(lambda: defaultdict(int))
    box_barcodes: dict[str, set[str]] = defaultdict(set)
    box_item_signatures: dict[str, set[tuple[str, str, str, str, str]]] = defaultdict(set)
    for row in rows:
        sku_code = str(row.get("sku") or "").strip()
        if not sku_code:
            continue
        box_code = str(row.get("box_code") or "").strip()
        if not box_code:
            continue
        qty_in_box = int(row.get("qty") or 0)
        if qty_in_box <= 0:
            continue
        size = str(row.get("size") or "").strip()
        goods_type = str(row.get("goods_type") or "").strip()
        barcode = str(row.get("barcode") or "").strip()
        name = str(row.get("name") or "").strip()
        sku_id = int(row.get("sku_ref_id") or 0)
        line_key = (sku_id, sku_code, name, size, barcode, goods_type)
        box_id = f"{int(row.get('agency_id') or 0)}:{box_code}"
        box_lines[box_id][line_key] += qty_in_box
        box_available_lines[box_id][line_key] += int(row_available_qty.get(int(row.get("id") or 0), 0))
        item_signature = (
            sku_code.lower(),
            size.lower(),
            name.lower(),
            goods_type.lower(),
            barcode.lower(),
        )
        box_item_signatures[box_id].add(item_signature)
        normalized_barcode = barcode.lower()
        if normalized_barcode:
            box_barcodes[box_id].add(normalized_barcode)

    all_box_ids = set(box_item_signatures.keys()) | set(box_barcodes.keys())
    mixed_box_ids: set[str] = set()
    for box_id in all_box_ids:
        barcodes = box_barcodes.get(box_id, set())
        if len(barcodes) >= 2:
            mixed_box_ids.add(box_id)
            continue
        signatures = box_item_signatures.get(box_id, set())
        if len(signatures) >= 2:
            mixed_box_ids.add(box_id)

    fully_available_box_ids = {
        box_id
        for box_id, lines_map in box_lines.items()
        if lines_map
        and all(
            int(box_available_lines.get(box_id, {}).get(line_key, 0)) >= int(qty_in_box)
            for line_key, qty_in_box in lines_map.items()
        )
    }

    # Обычные (не mixed) короба агрегируем по строкам как раньше.
    regular_grouped: dict[tuple[int, str, str, str, str, str, int], set[str]] = defaultdict(set)
    for box_id, lines_map in box_lines.items():
        if box_id not in fully_available_box_ids:
            continue
        if box_id in mixed_box_ids:
            continue
        for line_key, qty_in_box in lines_map.items():
            sku_id, sku_code, name, size, barcode, goods_type = line_key
            key = (sku_id, sku_code, name, size, barcode, goods_type, int(qty_in_box))
            regular_grouped[key].add(box_id)

    # Mixed-короба группируем по составу, чтобы можно было выбирать количество коробов
    # и автоматически добавлять все позиции из того же короба/типа короба.
    mixed_compositions: dict[
        tuple[tuple[int, str, str, str, str, str, int], ...],
        list[str],
    ] = defaultdict(list)
    for box_id in sorted(mixed_box_ids & fully_available_box_ids):
        composition: list[tuple[int, str, str, str, str, str, int]] = []
        for line_key, qty_in_box in box_lines.get(box_id, {}).items():
            sku_id, sku_code, name, size, barcode, goods_type = line_key
            composition.append(
                (
                    int(sku_id),
                    str(sku_code),
                    str(name),
                    str(size),
                    str(barcode),
                    str(goods_type),
                    int(qty_in_box),
                )
            )
        if not composition:
            continue
        mixed_compositions[tuple(sorted(composition))].append(box_id)
    prepared = []
    for (sku_id, sku_code, name, size, barcode, goods_type, box_qty), boxes in regular_grouped.items():
        boxes_count = len(boxes)
        if boxes_count <= 0:
            continue
        total_qty = boxes_count * int(box_qty)
        prepared.append(
            {
                "sku_code": sku_code,
                "sku_id": sku_id,
                "name": name,
                "size": size,
                "barcode": barcode,
                "goods_type": goods_type,
                "box_qty": int(box_qty),
                "boxes_count": boxes_count,
                "total_qty": total_qty,
                "is_mixed_box": False,
                "mixed_group": "",
            }
        )

    for composition_index, (composition, box_ids) in enumerate(sorted(mixed_compositions.items(), key=lambda item: item[1][0])):
        boxes_count = len(box_ids)
        if boxes_count <= 0:
            continue
        mixed_group = f"mix:{composition_index}:{box_ids[0]}"
        for sku_id, sku_code, name, size, barcode, goods_type, box_qty in composition:
            total_qty = boxes_count * int(box_qty)
            prepared.append(
                {
                    "sku_code": sku_code,
                    "sku_id": int(sku_id),
                    "name": name,
                    "size": size,
                    "barcode": barcode,
                    "goods_type": goods_type,
                    "box_qty": int(box_qty),
                    "boxes_count": boxes_count,
                    "total_qty": total_qty,
                    "is_mixed_box": True,
                    "mixed_group": mixed_group,
                }
            )

    prepared.sort(
        key=lambda row: (
            0 if row["is_mixed_box"] else 1,
            row["mixed_group"],
            -int(row["sku_id"] or 0),
            row["sku_code"].lower(),
            row["size"].lower(),
            StockAvailabilityService.normalize_goods_type(row["goods_type"]),
            -int(row["box_qty"] or 0),
        )
    )

    interim_rows: list[dict] = []
    for row in prepared:
        available_boxes = int(row["boxes_count"])
        interim_rows.append(
            {
                **row,
                "available_boxes": int(max(available_boxes, 0)),
                "available_qty": int(max(available_boxes, 0)) * int(row["box_qty"]),
            }
        )

    mixed_rows_by_group: dict[str, list[dict]] = defaultdict(list)
    for row in interim_rows:
        if row.get("is_mixed_box") and row.get("mixed_group"):
            mixed_rows_by_group[str(row["mixed_group"])].append(row)

    for group_rows in mixed_rows_by_group.values():
        group_available = min(int(item.get("available_boxes") or 0) for item in group_rows)
        for item in group_rows:
            item["available_boxes"] = max(group_available, 0)
            item["available_qty"] = max(group_available, 0) * int(item.get("box_qty") or 0)

    mixed_color_map: dict[str, int] = {}
    for index, group_key in enumerate(sorted(mixed_rows_by_group.keys())):
        mixed_color_map[group_key] = index % 6

    result: list[dict] = []
    for row in interim_rows:
        available_boxes = int(row.get("available_boxes") or 0)
        if available_boxes <= 0:
            continue
        available_qty = available_boxes * int(row["box_qty"])
        picker_key = _compose_picker_key(
            sku_id=row["sku_id"],
            sku_code=row["sku_code"],
            name=row["name"],
            size=row["size"],
            goods_type=row["goods_type"],
            box_qty=row["box_qty"],
            barcode=row["barcode"],
            mixed_group=row["mixed_group"],
        )
        result.append(
            {
                "key": picker_key,
                "sku_id": int(row["sku_id"] or 0),
                "sku_code": row["sku_code"],
                "name": row["name"],
                "size": row["size"],
                "barcode": row["barcode"],
                "goods_type": row["goods_type"],
                "box_qty": int(row["box_qty"]),
                "available_boxes": int(available_boxes),
                "available_qty": int(available_qty),
                "is_mixed_box": bool(row["is_mixed_box"]),
                "mixed_group": row["mixed_group"],
                "mixed_color": int(mixed_color_map.get(str(row["mixed_group"] or ""), -1)),
            }
        )
    return result


def _selected_stock_rows_with_boxes(
    request,
    stock_rows: list[dict],
    *,
    multiple: bool,
) -> tuple[list[tuple[dict, int]], int, list[str]]:
    stock_map = {row["key"]: row for row in stock_rows}
    mixed_group_rows: dict[str, list[dict]] = defaultdict(list)
    for row in stock_rows:
        if row.get("is_mixed_box") and row.get("mixed_group"):
            mixed_group_rows[str(row["mixed_group"])].append(row)
    errors: list[str] = []

    if multiple:
        keys = []
    else:
        keys = [request.POST.get("stock_key", "")]
    all_keys = request.POST.getlist("stock_key_all[]") or request.POST.getlist("stock_key_all")
    all_boxes = request.POST.getlist("stock_boxes[]") or request.POST.getlist("stock_boxes")
    boxes_by_key: dict[str, str] = {}
    for index, raw_key in enumerate(all_keys):
        key = str(raw_key or "").strip()
        if not key or key in boxes_by_key:
            continue
        boxes_by_key[key] = all_boxes[index] if index < len(all_boxes) else ""

    if multiple:
        positive_box_keys: list[str] = []
        seen_positive: set[str] = set()
        for key, raw_boxes in boxes_by_key.items():
            try:
                boxes = int(str(raw_boxes or "").strip())
            except ValueError:
                continue
            if boxes <= 0:
                continue
            if key not in seen_positive:
                seen_positive.add(key)
                positive_box_keys.append(key)
        keys = positive_box_keys

    explicit_rows: list[tuple[dict, int]] = []
    for raw_key in keys:
        key = str(raw_key or "").strip()
        if not key:
            continue
        row = stock_map.get(key)
        if row is None:
            errors.append("Выбрана неактуальная складская строка. Обновите страницу и повторите.")
            continue
        raw_boxes = boxes_by_key.get(key, "")
        try:
            boxes = int(str(raw_boxes or "").strip())
        except ValueError:
            errors.append(f"{row['sku_code']}: количество коробов должно быть целым числом.")
            continue
        if boxes <= 0:
            continue
        explicit_rows.append((row, boxes))

    mixed_group_boxes: dict[str, int] = {}
    for row, boxes in explicit_rows:
        group = str(row.get("mixed_group") or "").strip()
        if not (row.get("is_mixed_box") and group):
            continue
        previous = mixed_group_boxes.get(group)
        if previous is None:
            mixed_group_boxes[group] = boxes
            continue
        if previous != boxes:
            errors.append(
                "Для одного микс-короба укажите одинаковое количество коробов по выбранным строкам."
            )

    expanded_rows: list[tuple[dict, int]] = []
    expanded_groups: set[str] = set()
    counted_groups: set[str] = set()
    total_box_count = 0
    for row, boxes in explicit_rows:
        group = str(row.get("mixed_group") or "").strip()
        if row.get("is_mixed_box") and group:
            if group not in counted_groups:
                counted_groups.add(group)
                total_box_count += boxes
            if group in expanded_groups:
                continue
            expanded_groups.add(group)
            group_boxes = mixed_group_boxes.get(group, boxes)
            for sibling in mixed_group_rows.get(group, []):
                expanded_rows.append((sibling, group_boxes))
        else:
            total_box_count += boxes
            expanded_rows.append((row, boxes))

    return expanded_rows, int(max(total_box_count, 0)), errors


def _parse_selected_stock_items(
    request,
    stock_rows: list[dict],
    *,
    multiple: bool,
) -> tuple[list[dict], list[str]]:
    expanded_rows, _total_box_count, errors = _selected_stock_rows_with_boxes(
        request,
        stock_rows,
        multiple=multiple,
    )
    selected_map: dict[tuple[str, str, str, str, str], dict] = {}

    for row, boxes in expanded_rows:
        if boxes > int(row["available_boxes"]):
            errors.append(
                f"{row['sku_code']}/{row['size'] or '-'}: доступно только {row['available_boxes']} короб."
            )
            continue
        qty_requested = boxes * int(row["box_qty"])
        item_key = (
            str(row.get("sku_code") or "").strip(),
            str(row.get("name") or "").strip(),
            str(row.get("size") or "").strip(),
            str(row.get("barcode") or "").strip(),
            str(row.get("goods_type") or "").strip(),
        )
        existing = selected_map.get(item_key)
        comment = f"Коробов: {boxes}; кратность: {row['box_qty']}"
        if row.get("is_mixed_box"):
            comment += "; микс-короб"
        if existing:
            existing["qty_requested"] += qty_requested
            continue
        selected_map[item_key] = {
            "sku_code": item_key[0],
            "name": item_key[1],
            "size": item_key[2],
            "barcode": item_key[3],
            "goods_type": item_key[4],
            "qty_requested": qty_requested,
            "comment": comment,
        }
    selected = list(selected_map.values())
    if not selected:
        errors.append("Выберите минимум одну позицию из складских остатков и укажите количество коробов.")
    return selected, errors


@login_required
def shipping_list(request):
    scope, role, client_agency = _request_scope(request)
    if scope is None:
        return HttpResponseForbidden("Доступ запрещен")
    context = build_shipping_list_page_context(
        request=request,
        scope=scope,
        role=role,
        client_agency=client_agency,
    )
    return render(request, "shipping/list.html", context)


@login_required
def shipping_create(request):
    scope, role, client_agency = _request_scope(request)
    if scope is None or not _can_write(scope, role):
        return HttpResponseForbidden("Доступ запрещен")
    return handle_shipping_create_request(
        request=request,
        scope=scope,
        role=role,
        client_agency=client_agency,
    )


@login_required
def shipping_detail(request, pk: int):
    scope, role, client_agency = _request_scope(request)
    if scope is None:
        return HttpResponseForbidden("Доступ запрещен")

    order = get_object_or_404(
        ShippingOrder.objects.select_related("agency", "created_by", "marketplace").prefetch_related("items", "reserves"),
        pk=pk,
    )
    if not _can_access_order(scope, client_agency, order):
        return HttpResponseForbidden("Доступ запрещен")

    can_write = _can_write(scope, role)
    can_edit_items = _can_edit_items(scope, role, order)
    can_submit_for_approval = _can_submit_for_approval(scope, role, order)
    can_manager_approve = _can_manager_approve(scope, role, order)
    can_manager_reopen = _can_manager_reopen(scope, role, order)
    can_storekeeper_accept = _can_storekeeper_accept(scope, role, order)
    can_storekeeper_pick = _can_storekeeper_pick(scope, role, order)
    pick_readiness = shipping_pick_readiness(order) if can_storekeeper_pick else {"can_pick": False, "reason": ""}
    can_storekeeper_pack = _can_storekeeper_pack(scope, role, order)
    can_storekeeper_manage_packing = _can_storekeeper_manage_packing(scope, role, order)
    can_cancel = _can_cancel(scope, role, order)
    can_edit_order_form = _can_edit_order_form(scope, role, order)
    stock_rows_for_order = _shipping_stock_picker_rows(order.agency, exclude_order=order)
    if request.method == "POST":
        if not can_write:
            return HttpResponseForbidden("Доступ запрещен")
        action_result = handle_shipping_detail_action(
            action=(request.POST.get("action") or "").strip(),
            order=order,
            request=request,
            role=role,
            stock_rows_for_order=stock_rows_for_order,
            permissions=ShippingDetailActionPermissions(
                can_edit_items=can_edit_items,
                can_submit_for_approval=can_submit_for_approval,
                can_manager_approve=can_manager_approve,
                can_manager_reopen=can_manager_reopen,
                can_storekeeper_accept=can_storekeeper_accept,
                can_storekeeper_pick=can_storekeeper_pick,
                can_cancel=can_cancel,
            ),
            parse_selected_stock_items=_parse_selected_stock_items,
            selected_stock_rows_with_boxes=_selected_stock_rows_with_boxes,
            parse_box_count_from_comment=_parse_box_count_from_comment,
            log_update=_log_update,
        )
        for level, text in action_result.messages:
            getattr(messages, level)(request, text)
        if action_result.redirect_name == "shipping:packing":
            return redirect("shipping:packing", pk=order.pk)
        return redirect("shipping:detail", pk=order.pk)
    context = build_shipping_detail_page_context(
        request=request,
        order=order,
        scope=scope,
        role=role,
    )
    return render(request, "shipping/detail.html", context)


@login_required
def shipping_dispatch_act(request, pk: int):
    return handle_shipping_dispatch_act_request(request=request, pk=pk)


@login_required
def shipping_sign_dispatch_act_logistician(request, pk: int):
    return handle_shipping_dispatch_sign_logistician_request(request=request, pk=pk)


@login_required
def shipping_sign_dispatch_act_manager(request, pk: int):
    return handle_shipping_dispatch_sign_manager_request(request=request, pk=pk)


@login_required
def shipping_attachment_download(request, pk: int, attachment_id: int):
    return download_shipping_attachment(request=request, pk=pk, attachment_id=attachment_id)


def _shipping_documents_response(request, pk: int):
    return handle_shipping_documents_request(request=request, pk=pk)


@login_required
def shipping_documents(request, pk: int):
    return _shipping_documents_response(request, pk)


@login_required
def shipping_transport_note(request, pk: int):
    return _shipping_documents_response(request, pk)


@login_required
@xframe_options_sameorigin
def shipping_transport_note_pdf(request, pk: int):
    return build_shipping_transport_note_pdf_response(request=request, pk=pk)


@login_required
def shipping_transport_note_docx(request, pk: int):
    return build_shipping_transport_note_docx_response(request=request, pk=pk)


@login_required
def shipping_return_act_doc(request, pk: int):
    return build_shipping_return_act_response(request=request, pk=pk)


@login_required
def shipping_packing(request, pk: int):
    return handle_shipping_packing_request(request=request, pk=pk)


@login_required
def shipping_packing_slips(request, pk: int):
    return handle_shipping_packing_slips_request(request=request, pk=pk)


@login_required
@require_http_methods(["GET", "POST"])
def shipping_packing_slips_status(request, pk: int):
    return handle_shipping_packing_slips_status_request(request=request, pk=pk)
