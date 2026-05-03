from __future__ import annotations

import re
from urllib.parse import urlencode

from django.db import transaction
from django.db.models import Q
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect

from audit.models import OrderAuditEntry, log_order_action
from employees.access import get_request_employee, get_request_role, resolve_cabinet_url
from sklad.models import WarehouseOperation, WarehouseOperationTask
from sku.models import Agency, SKUBarcode
from sklad.models import StockPalletState
from sklad.services.warehouse_stock_rows import legacy_stock_rows, snapshot_stock_rows
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sklad.stock_state import rebuild_stock_snapshot

from reachtruck.models import MoveRequest, MoveRequestItem, MoveTask
from .move_requests import (
    _agency_id_for_processing_order as agency_id_for_processing_order_service,
    _destination_from_request_data as destination_from_request_data_service,
    _find_pallet_by_code as find_pallet_by_code_service,
    _latest_moves_by_pallet as latest_moves_by_pallet_service,
    _location_label,
    _move_payload_matches_selectors as move_payload_matches_selectors_service,
    _normalize_zone_code,
    _parse_explicit_requested_rows as parse_explicit_requested_rows_service,
    _parse_move_request_items as parse_move_request_items_service,
    _plan_move_request_to_tasks as plan_move_request_to_tasks_service,
    sync_task_status_by_legacy_order_id,
)
from .pallet_ops import (
    MOVE_MODE_PALLET_FULL,
    _box_execution_plan as box_execution_plan_service,
    _matching_stock_boxes_for_pallet as matching_stock_boxes_for_pallet_service,
    _normalize_goods_type,
    _normalize_move_mode,
    _pallet_box_plan as pallet_box_plan_service,
    _parse_int_value,
    _parse_json_list,
    _payload_box_codes,
    _requested_barcode_qty,
    _requested_partial_rows,
    _single_requested_box,
)
from .task_commands import build_mobile_execution_snapshot, complete_move_task, scan_move_task_step, take_move_task


def _views():
    from .. import views as reachtruck_views

    return reachtruck_views


def _short_agency_name(name: str) -> str:
    text = str(name or "").strip()
    if not text:
        return "Без клиента"
    text = re.sub(r"^Индивидуальный предприниматель\s+", "ИП ", text, flags=re.IGNORECASE)
    return text


def _task_count_label(count: int) -> str:
    count = int(count or 0)
    mod10 = count % 10
    mod100 = count % 100
    if mod10 == 1 and mod100 != 11:
        suffix = "задание"
    elif mod10 in {2, 3, 4} and mod100 not in {12, 13, 14}:
        suffix = "задания"
    else:
        suffix = "заданий"
    return f"{count} {suffix}"


def _zone_summary_label(location: dict | None) -> str:
    location = location or {}
    zone = _normalize_zone_code(location.get("zone") or "") or "PR"
    if zone == "PR":
        return "Зона PR"
    if zone == "OBR":
        return "Зона OBR"
    if zone == "OTG":
        return "Зона OTG"
    if zone == "MR":
        return "Между рядами"
    if zone == "OS":
        return "Основной склад"
    return f"Зона {zone}"


def _mobile_request_route_summary(move: dict) -> str:
    from_label = _zone_summary_label(move.get("from_location") or {})
    to_label = _zone_summary_label(move.get("to_location") or {})
    return f"{from_label} -> {to_label}"


def _mobile_route_detail(move: dict) -> str:
    from_label = str(move.get("from_label") or "").strip()
    to_label = str(move.get("mobile_destination") or move.get("to_label") or "").strip()
    if from_label and to_label:
        return f"{from_label} -> {to_label}"
    return from_label or to_label or "-"


def _task_kind_label(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    explicit = str(payload.get("task_kind_label") or "").strip()
    if explicit:
        return explicit
    mode = _normalize_move_mode(payload.get("move_mode"), payload.get("pick_mode"))
    to_location = payload.get("to_location") or {}
    to_zone = _normalize_zone_code(to_location.get("zone") or "")
    if mode == "box_partial" and to_zone == "OTG":
        return "Частичный отбор с палеты для отгрузки"
    if mode == "box_partial":
        return "Частичный отбор с палеты"
    if mode == "box_full":
        return "Выдача коробов без разбора"
    return "Перемещение палеты"


def _agency_id_for_processing_order(order_id: str | None) -> int | None:
    order_key = str(order_id or "").strip()
    if not order_key:
        return None
    entry = (
        OrderAuditEntry.objects.filter(order_type="processing", order_id=order_key)
        .exclude(agency=None)
        .order_by("-created_at")
        .first()
    )
    if not entry:
        return None
    return int(entry.agency_id)


def _move_payload_matches_selectors(
    payload: dict,
    barcode_values: set[str] | None = None,
    sku_values: set[str] | None = None,
    goods_type_values: set[str] | None = None,
) -> bool:
    if not isinstance(payload, dict):
        return False
    barcode_values = {str(value).strip() for value in (barcode_values or set()) if str(value or "").strip()}
    sku_values = {str(value).strip() for value in (sku_values or set()) if str(value or "").strip()}
    goods_type_values = {
        _normalize_goods_type(value)
        for value in (goods_type_values or set())
        if _normalize_goods_type(value)
    }

    move_goods_type = _normalize_goods_type(payload.get("requested_goods_type"))
    if goods_type_values and move_goods_type and move_goods_type not in goods_type_values:
        return False

    move_sku = str(payload.get("requested_sku") or "").strip()
    requested_barcodes_raw = payload.get("requested_barcodes")
    if isinstance(requested_barcodes_raw, list):
        move_barcodes = {
            str(value).strip() for value in requested_barcodes_raw if str(value or "").strip()
        }
    else:
        move_barcodes = {
            str(value).strip() for value in _parse_json_list(requested_barcodes_raw) if str(value or "").strip()
        }

    if not barcode_values and not sku_values:
        return True
    sku_hit = bool(sku_values and move_sku and move_sku in sku_values)
    barcode_hit = bool(barcode_values and move_barcodes and (move_barcodes & barcode_values))
    if barcode_values and sku_values:
        return sku_hit or barcode_hit
    if barcode_values:
        return barcode_hit
    return sku_hit


def _move_instruction(payload: dict) -> str:
    explicit_instruction = str((payload or {}).get("instruction") or "").strip()
    if explicit_instruction:
        return explicit_instruction
    return "Переместить товар по заданию."


def _latest_moves_by_pallet(
    processing_order_id: str | None = None,
    barcode_values: set[str] | None = None,
    sku_values: set[str] | None = None,
    goods_type_values: set[str] | None = None,
) -> dict[str, dict]:
    processing_order_id = str(processing_order_id or "").strip()
    entries = OrderAuditEntry.objects.filter(order_type="stock_move").order_by("-created_at")
    latest: dict[str, dict] = {}
    for entry in entries:
        payload = entry.payload or {}
        pallet_code = str(payload.get("pallet_code") or "").strip()
        move_processing_order_id = str(payload.get("processing_order_id") or "").strip()
        if processing_order_id and move_processing_order_id != processing_order_id:
            continue
        if not _move_payload_matches_selectors(
            payload,
            barcode_values=barcode_values,
            sku_values=sku_values,
            goods_type_values=goods_type_values,
        ):
            continue
        if not pallet_code or pallet_code in latest:
            continue
        status = (payload.get("status") or payload.get("submit_action") or "").strip().lower()
        status_label = (payload.get("status_label") or "").strip()
        to_location = payload.get("to_location") or {}
        requested_barcodes_raw = payload.get("requested_barcodes")
        if isinstance(requested_barcodes_raw, list):
            requested_barcodes = [
                str(value).strip()
                for value in requested_barcodes_raw
                if str(value or "").strip()
            ]
        else:
            requested_barcodes = _parse_json_list(requested_barcodes_raw)
        latest[pallet_code] = {
            "status": status,
            "status_label": status_label,
            "to_label": _location_label(to_location),
            "to_zone": _normalize_zone_code(to_location.get("zone") or ""),
            "order_id": entry.order_id,
            "pick_mode": (payload.get("pick_mode") or "full"),
            "move_mode": _normalize_move_mode(payload.get("move_mode"), payload.get("pick_mode")),
            "requested_qty": _parse_int_value(payload.get("requested_qty")),
            "picked_qty": _parse_int_value(payload.get("picked_qty")),
            "requested_sku": str(payload.get("requested_sku") or "").strip(),
            "requested_barcodes": requested_barcodes,
            "requested_barcode_qty": _requested_barcode_qty(payload),
            "requested_boxes": _payload_box_codes(payload),
            "requested_box": _single_requested_box(payload),
            "requested_rows": _requested_partial_rows(payload),
            "requested_goods_type": _normalize_goods_type(
                payload.get("requested_goods_type") or payload.get("requested_goods_type_label")
            ),
            "processing_order_id": move_processing_order_id,
            "instruction": _move_instruction(payload),
        }
    return latest


def lookup_pallet_location_response(request):
    reachtruck_views = _views()
    role = get_request_role(request)
    if role not in reachtruck_views.ALLOWED_ROLES:
        return HttpResponseForbidden("Доступ запрещен")
    code = str(request.GET.get("code") or "").strip()
    if not code:
        return JsonResponse({"ok": False, "error": "Укажите код паллеты."}, status=400)
    processing_order_id = str(request.GET.get("processing_order_id") or "").strip()
    processing_agency_id = _agency_id_for_processing_order(processing_order_id)
    found = find_pallet_by_code_service(code, agency_id=processing_agency_id)
    if not found:
        return JsonResponse({"ok": False, "error": "Паллета не найдена."}, status=404)
    entry, _, _, location = found
    return JsonResponse(
        {
            "ok": True,
            "label": _location_label(location),
            "location": location,
            "receiving_order_id": entry.order_id,
        }
    )


def lookup_item_pallets_response(request):
    reachtruck_views = _views()
    role = get_request_role(request)
    if role not in reachtruck_views.ALLOWED_ROLES:
        return HttpResponseForbidden("Доступ запрещен")
    barcodes = request.GET.getlist("barcode")
    barcode = str(request.GET.get("barcode") or "").strip()
    if barcode and barcode not in barcodes:
        barcodes.append(barcode)
    expanded = []
    for value in barcodes:
        for part in str(value or "").split(","):
            part = part.strip()
            if part:
                expanded.append(part)
    barcodes = expanded
    sku = str(request.GET.get("sku") or "").strip()
    requested_goods_type = _normalize_goods_type(request.GET.get("goods_type"))
    if not barcodes and not sku:
        return JsonResponse({"ok": False, "error": "Укажите ШК или артикул."}, status=400)
    barcode_values = {value for value in barcodes if value}
    sku_values = {sku} if sku else set()
    goods_type_values = {requested_goods_type} if requested_goods_type else set()
    if barcode_values:
        sku_values.update(
            SKUBarcode.objects.filter(value__in=barcode_values)
            .values_list("sku__sku_code", flat=True)
        )
        sku_values.discard(None)

    include_moves = request.GET.get("include_moves") == "1"
    processing_order_id = str(request.GET.get("processing_order_id") or "").strip()
    processing_agency_id = _agency_id_for_processing_order(processing_order_id)
    moves_by_pallet = {}
    if include_moves:
        moves_by_pallet = _latest_moves_by_pallet(
            processing_order_id=processing_order_id,
            barcode_values=barcode_values,
            sku_values=sku_values,
            goods_type_values=goods_type_values,
        )
    matches = {}

    def _move_matches_lookup(move_payload: dict) -> bool:
        return _move_payload_matches_selectors(
            move_payload,
            barcode_values=barcode_values,
            sku_values=sku_values,
            goods_type_values=goods_type_values,
        )

    state_rows = snapshot_stock_rows(
        agency_id=processing_agency_id,
        sku_values=sku_values,
        barcode_values=barcode_values,
        require_pallet=True,
    )
    if not state_rows:
        legacy_qs = StockPalletState.objects.filter(state=StockPalletState.STATE_WAREHOUSE)
        if processing_agency_id:
            legacy_qs = legacy_qs.filter(agency_id=processing_agency_id)
        if sku_values:
            if barcode_values:
                legacy_qs = legacy_qs.filter(
                    Q(sku__in=sku_values) | Q(barcode__in=barcode_values)
                )
            else:
                legacy_qs = legacy_qs.filter(sku__in=sku_values)
        elif barcode_values:
            legacy_qs = legacy_qs.filter(barcode__in=barcode_values)
        else:
            legacy_qs = legacy_qs.none()
        state_rows = legacy_stock_rows(legacy_qs)
    if not state_rows:
        try:
            rebuild_stock_snapshot()
        except Exception:
            pass
        state_rows = legacy_stock_rows(
            StockPalletState.objects.filter(state=StockPalletState.STATE_WAREHOUSE)
            .filter(agency_id=processing_agency_id) if processing_agency_id else
            StockPalletState.objects.filter(state=StockPalletState.STATE_WAREHOUSE)
        )
        if sku_values or barcode_values:
            filtered_rows = []
            for row in state_rows:
                row_sku = str(row.get("sku") or "").strip()
                row_barcode = str(row.get("barcode") or "").strip()
                if sku_values and barcode_values:
                    if row_sku not in sku_values and row_barcode not in barcode_values:
                        continue
                elif sku_values and row_sku not in sku_values:
                    continue
                elif barcode_values and row_barcode not in barcode_values:
                    continue
                filtered_rows.append(row)
            state_rows = filtered_rows
        else:
            state_rows = []

    for state_row in state_rows:
        pallet_code = str(state_row.get("pallet_code") or "").strip()
        if not pallet_code:
            continue
        payload_goods_type = _normalize_goods_type(state_row.get("goods_type"))
        if goods_type_values and payload_goods_type and payload_goods_type not in goods_type_values:
            continue
        prev = matches.get(pallet_code)
        payload = {
            "pallet": pallet_code,
            "location": str(state_row.get("location") or "").strip() or "—",
            "receiving_order_id": state_row.get("order_id") or "",
            "qty": int(state_row.get("qty") or 0),
            "box_qty": 0,
            "box_matches": [],
            "goods_type": state_row.get("goods_type") or "",
        }
        if include_moves and pallet_code in moves_by_pallet:
            move_payload = moves_by_pallet[pallet_code]
            payload.update(
                {
                    "move_status": move_payload.get("status") or "",
                    "move_status_label": move_payload.get("status_label") or "",
                    "move_to_label": move_payload.get("to_label") or "",
                    "move_to_zone": move_payload.get("to_zone") or "",
                    "move_order_id": move_payload.get("order_id") or "",
                    "move_pick_mode": move_payload.get("pick_mode") or "full",
                    "move_mode": move_payload.get("move_mode") or MOVE_MODE_PALLET_FULL,
                    "move_requested_qty": move_payload.get("requested_qty") or 0,
                    "move_picked_qty": move_payload.get("picked_qty") or 0,
                    "move_requested_sku": move_payload.get("requested_sku") or "",
                    "move_requested_barcodes": move_payload.get("requested_barcodes") or [],
                    "move_requested_barcode_qty": move_payload.get("requested_barcode_qty") or {},
                    "move_requested_boxes": move_payload.get("requested_boxes") or [],
                    "move_requested_box": move_payload.get("requested_box") or "",
                    "move_requested_rows": move_payload.get("requested_rows") or [],
                    "move_requested_goods_type": move_payload.get("requested_goods_type") or "",
                    "move_processing_order_id": move_payload.get("processing_order_id") or "",
                    "move_instruction": move_payload.get("instruction") or "",
                }
            )
        if not prev:
            matches[pallet_code] = payload
            continue
        prev_qty = int(prev.get("qty") or 0)
        payload["qty"] = prev_qty + int(payload.get("qty") or 0)
        matches[pallet_code] = payload

    if include_moves:
        for pallet_code, move_payload in moves_by_pallet.items():
            pallet_code = str(pallet_code or "").strip()
            if not pallet_code or pallet_code in matches:
                continue
            if not _move_matches_lookup(move_payload):
                continue
            move_requested_goods_type = _normalize_goods_type(move_payload.get("requested_goods_type"))
            payload = {
                "pallet": pallet_code,
                "location": str(move_payload.get("to_label") or "").strip() or "—",
                "receiving_order_id": "",
                "qty": 0,
                "box_qty": 0,
                "box_matches": [],
                "goods_type": move_requested_goods_type or "",
                "move_status": move_payload.get("status") or "",
                "move_status_label": move_payload.get("status_label") or "",
                "move_to_label": move_payload.get("to_label") or "",
                "move_to_zone": move_payload.get("to_zone") or "",
                "move_order_id": move_payload.get("order_id") or "",
                "move_pick_mode": move_payload.get("pick_mode") or "full",
                "move_mode": move_payload.get("move_mode") or MOVE_MODE_PALLET_FULL,
                "move_requested_qty": move_payload.get("requested_qty") or 0,
                "move_picked_qty": move_payload.get("picked_qty") or 0,
                "move_requested_sku": move_payload.get("requested_sku") or "",
                "move_requested_barcodes": move_payload.get("requested_barcodes") or [],
                "move_requested_barcode_qty": move_payload.get("requested_barcode_qty") or {},
                "move_requested_boxes": move_payload.get("requested_boxes") or [],
                "move_requested_box": move_payload.get("requested_box") or "",
                "move_requested_rows": move_payload.get("requested_rows") or [],
                "move_requested_goods_type": move_requested_goods_type or "",
                "move_processing_order_id": move_payload.get("processing_order_id") or "",
                "move_instruction": move_payload.get("instruction") or "",
            }
            matches[pallet_code] = payload
    for pallet_code, payload in matches.items():
        code = str(pallet_code or "").strip()
        if not code:
            continue
        box_matches = matching_stock_boxes_for_pallet_service(
            code,
            barcode_values,
            sku_values,
            goods_type_values,
            agency_id=processing_agency_id,
        )
        box_qty = sum(_parse_int_value(item.get("qty")) for item in box_matches)
        payload["box_matches"] = box_matches
        payload["box_qty"] = box_qty
        if box_qty > 0:
            payload["qty"] = box_qty
    return JsonResponse({"ok": True, "pallets": list(matches.values())})


def create_move_request_response(request):
    reachtruck_views = _views()
    role = get_request_role(request)
    if role not in reachtruck_views.CREATE_ROLES:
        return HttpResponseForbidden("Доступ запрещен")
    processing_order_id = str(request.POST.get("processing_order_id") or "").strip()
    destination = destination_from_request_data_service(
        {
            "to_zone": request.POST.get("to_zone") or "OBR",
            "to_row": request.POST.get("to_row"),
            "to_section": request.POST.get("to_section"),
            "to_tier": request.POST.get("to_tier"),
            "to_cell": request.POST.get("to_cell"),
        }
    )
    zone = _normalize_zone_code(destination.get("zone") or "")
    row = _parse_int_value(destination.get("row"))
    section = _parse_int_value(destination.get("section"))
    tier = _parse_int_value(destination.get("tier"))
    cell = _parse_int_value(destination.get("cell"))
    if zone not in reachtruck_views.ALLOWED_ZONES:
        return JsonResponse({"ok": False, "error": "Некорректная зона назначения."}, status=400)
    if zone == "MR" and not row:
        return JsonResponse({"ok": False, "error": "Для зоны MR укажите ряд."}, status=400)
    if zone == "OS" and not (row and section and tier and cell):
        return JsonResponse(
            {"ok": False, "error": "Для зоны OS укажите ряд, секцию, ярус и ячейку."},
            status=400,
        )
    request_items = parse_move_request_items_service(
        request.POST.get("request_items_json"),
        fallback_form={
            "requested_article": request.POST.get("requested_article"),
            "requested_goods_type": request.POST.get("requested_goods_type"),
            "requested_qty": request.POST.get("requested_qty"),
            "pick_qty": request.POST.get("pick_qty"),
            "requested_barcodes_json": request.POST.get("requested_barcodes_json"),
        },
    )
    explicit_requested_rows = parse_explicit_requested_rows_service(request.POST.get("requested_rows_json"))
    if not request_items and not explicit_requested_rows:
        return JsonResponse(
            {"ok": False, "error": "Укажите товар (SKU/ШК) и количество для перемещения."},
            status=400,
        )
    agency_id = _agency_id_for_processing_order(processing_order_id)
    if not agency_id:
        agency_id = _parse_int_value(request.POST.get("agency_id"))
    if not agency_id:
        return JsonResponse(
            {"ok": False, "error": "Не удалось определить клиента для перемещения."},
            status=400,
        )
    agency = Agency.objects.filter(pk=agency_id).first()
    if not agency:
        return JsonResponse(
            {"ok": False, "error": "Клиент не найден."},
            status=400,
        )
    employee = get_employee_for_user(request.user)
    if employee and employee.full_name:
        requested_by_name = employee.full_name
    else:
        requested_by_name = request.user.get_full_name().strip() or request.user.username
    context_type = MoveRequest.CONTEXT_PROCESSING if processing_order_id else MoveRequest.CONTEXT_MANUAL
    move_request = MoveRequest.objects.create(
        context_type=context_type,
        context_id=processing_order_id,
        agency=agency,
        requested_by=request.user if request.user.is_authenticated else None,
        requested_by_role=role or "",
        requested_by_name=requested_by_name,
        destination_zone=zone or "PR",
        destination_row=row or None,
        destination_section=section or None,
        destination_tier=tier or None,
        destination_cell=cell or None,
        status=MoveRequest.STATUS_CREATED,
        comment=str(request.POST.get("comment") or "").strip(),
    )
    move_request_items: list[MoveRequestItem] = []
    for item in request_items:
        requested_barcodes = item.get("requested_barcodes") or []
        barcode_value = requested_barcodes[0] if len(requested_barcodes) == 1 else ""
        move_request_items.append(
            MoveRequestItem.objects.create(
                request=move_request,
                sku_code=str(item.get("requested_article") or "").strip(),
                barcode=barcode_value,
                goods_type=str(item.get("requested_goods_type") or "").strip(),
                qty_requested=_parse_int_value(item.get("requested_qty")),
                qty_planned=0,
            )
        )
    result = plan_move_request_to_tasks_service(
        move_request=move_request,
        move_request_items=move_request_items,
        request_items=request_items,
        explicit_requested_rows=explicit_requested_rows,
        destination=destination,
        processing_order_id=processing_order_id,
        requested_by_name=requested_by_name,
        requested_by_role=role or "",
        user=request.user,
    )
    created = _parse_int_value(result.get("created"))
    shortage_qty = _parse_int_value(result.get("shortage_qty"))
    return JsonResponse(
        {
            "ok": created > 0,
            "request_id": move_request.id,
            "tasks_created": created,
            "shortage_qty": shortage_qty,
            "status": move_request.status,
            "error": move_request.planning_error if created <= 0 else "",
        },
        status=200 if created > 0 else 400,
    )


def mobile_category_key(move: dict, payload: dict) -> str:
    reachtruck_views = _views()
    explicit = str(payload.get("task_category") or payload.get("mobile_category") or "").strip().lower()
    if explicit in {"shipping", "movement", "inventory", "optimization"}:
        return explicit
    to_zone = _normalize_zone_code((move.get("to_location") or {}).get("zone") or "")
    task_kind = str(payload.get("task_kind_label") or "").strip().lower()
    if payload.get("shipping_order_pk") or payload.get("shipping_order_id") or to_zone == "OTG":
        return "shipping"
    if "инвентар" in task_kind:
        return "inventory"
    if "комплект" in task_kind or "оптим" in task_kind:
        return "optimization"
    return "movement"


def mobile_category_label(key: str) -> str:
    for category_key, label in _views().MOBILE_MOVE_CATEGORIES:
        if category_key == key:
            return label
    return "Перемещения"


def mobile_task_url(category: str, order_id: str) -> str:
    return f"/reachtruck/?{urlencode({'mobile_category': category, 'mobile_task': order_id})}"


def _source_order_identity(
    payload: dict,
    *,
    request_context_type: str = "",
    request_context_id: str = "",
) -> tuple[str, str]:
    processing_number = str(payload.get("processing_order_id") or "").strip()
    if processing_number:
        return "processing", processing_number
    receiving_number = str(payload.get("receiving_order_id") or "").strip()
    if receiving_number:
        return "receiving", receiving_number
    shipping_number = str(payload.get("shipping_order_id") or "").strip()
    if shipping_number:
        return "shipping", shipping_number
    context_type = str(request_context_type or "").strip().lower()
    context_id = str(request_context_id or "").strip()
    if context_type in {"processing", "receiving", "shipping"} and context_id:
        return context_type, context_id
    stockmap_source_type = str(payload.get("stockmap_source_order_type") or "").strip().lower()
    stockmap_source_id = str(payload.get("stockmap_source_order_id") or "").strip()
    if stockmap_source_type in {"processing", "receiving", "shipping"} and stockmap_source_id:
        return stockmap_source_type, stockmap_source_id
    return "task", ""


def mobile_request_identity(
    payload: dict,
    order_id: str,
    *,
    request_context_type: str = "",
    request_context_id: str = "",
) -> tuple[str, str, str, str]:
    source_type, source_id = _source_order_identity(
        payload,
        request_context_type=request_context_type,
        request_context_id=request_context_id,
    )
    if source_type == "processing" and source_id:
        return (
            "processing",
            source_id,
            _views().format_order_number("processing", source_id),
            "Заявка на обработку",
        )
    if source_type == "receiving" and source_id:
        return (
            "receiving",
            source_id,
            _views().format_order_number("receiving", source_id),
            "Заявка на приемку",
        )
    if source_type == "shipping" and source_id:
        return (
            "shipping",
            source_id,
            _views().format_order_number("shipping", source_id),
            "Заявка на отгрузку",
        )
    fallback_order_id = str(order_id or "").strip() or "-"
    return ("task", fallback_order_id, fallback_order_id, "Задание ричтрака")


def mobile_request_key(
    payload: dict,
    order_id: str,
    *,
    request_context_type: str = "",
    request_context_id: str = "",
) -> str:
    source_type, source_id, _label, _type_label = mobile_request_identity(
        payload,
        order_id,
        request_context_type=request_context_type,
        request_context_id=request_context_id,
    )
    return f"{source_type}:{source_id}"


def mobile_request_url(category: str, request_key: str) -> str:
    return f"/reachtruck/?{urlencode({'mobile_category': category, 'mobile_request': request_key})}"


def mobile_number_label(
    category: str,
    payload: dict,
    order_id: str,
    shipping_order_pk: int,
    *,
    request_context_type: str = "",
    request_context_id: str = "",
) -> str:
    suffix_map = {
        "shipping": "OTG",
        "movement": "MOV",
        "inventory": "INV",
        "optimization": "OPT",
    }
    suffix = suffix_map.get(category, "MOV")
    if category == "shipping":
        shipping_number = str(payload.get("shipping_order_id") or "").strip()
        if shipping_number:
            return _views().format_order_number("shipping", shipping_number)
        base = shipping_order_pk or order_id
    else:
        source_type, _source_id, source_label, _type_label = mobile_request_identity(
            payload,
            order_id,
            request_context_type=request_context_type,
            request_context_id=request_context_id,
        )
        if source_type != "task":
            return source_label
        base = (
            payload.get("processing_order_id")
            or payload.get("receiving_order_id")
            or payload.get("stockmap_source_order_id")
            or payload.get("shipping_order_id")
            or order_id
        )
    base_str = str(base or order_id).strip() or str(order_id)
    if base_str.endswith(f"_{suffix}"):
        return base_str
    return f"{base_str}_{suffix}"


def move_source_link(
    payload: dict,
    order_id: str,
    *,
    request_context_type: str = "",
    request_context_id: str = "",
) -> tuple[str, str, str]:
    reachtruck_views = _views()
    source_type, source_id = _source_order_identity(
        payload,
        request_context_type=request_context_type,
        request_context_id=request_context_id,
    )
    processing_number = source_id if source_type == "processing" else ""
    if processing_number:
        return (
            f"/orders/processing/{processing_number}/work/",
            reachtruck_views.format_order_number("processing", processing_number),
            "заявку на обработку",
        )
    receiving_number = source_id if source_type == "receiving" else ""
    if receiving_number:
        return (
            f"/orders/receiving/{receiving_number}/flow/",
            reachtruck_views.format_order_number("receiving", receiving_number),
            "приемку",
        )
    shipping_order_pk = _parse_int_value(payload.get("shipping_order_pk"))
    shipping_number = str(payload.get("shipping_order_id") or "").strip()
    if shipping_order_pk > 0:
        shipping_label = (
            reachtruck_views.format_order_number("shipping", shipping_number)
            if shipping_number
            else f"Отгрузка #{shipping_order_pk}"
        )
        return (
            f"/shipping/{shipping_order_pk}/",
            shipping_label,
            "отгрузку",
        )
    return "", "", ""


def dashboard_identity(role: str | None) -> dict[str, str]:
    role_key = str(role or "").strip().lower()
    if role_key == "reachtruck_driver":
        return {
            "browser_title": "Кабинет водителя ричтрака",
            "profile_title": "Кабинет водителя ричтрака",
            "panel_title": "Ричтрак",
            "panel_subtitle": "Перемещение паллет",
        }
    if role_key == "storekeeper":
        return {
            "browser_title": "Перемещение паллет кладовщика",
            "profile_title": "Кабинет кладовщика",
            "panel_title": "Склад",
            "panel_subtitle": "Задания на перемещение паллет",
        }
    if role_key == "processing_head":
        return {
            "browser_title": "Перемещение паллет обработки",
            "profile_title": "Кабинет руководителя обработки",
            "panel_title": "Перемещения",
            "panel_subtitle": "Контроль заданий ричтрака",
        }
    if role_key == "manager":
        return {
            "browser_title": "Перемещение паллет менеджера",
            "profile_title": "Кабинет менеджера",
            "panel_title": "Перемещения",
            "panel_subtitle": "Контроль заданий ричтрака",
        }
    return {
        "browser_title": "Перемещение паллет",
        "profile_title": "Перемещение паллет",
        "panel_title": "Перемещения",
        "panel_subtitle": "Контроль заданий ричтрака",
    }


@transaction.atomic
def cancel_move_before_take(*, legacy_order_id: str, actor_role: str) -> tuple[bool, str]:
    target_id = str(legacy_order_id or "").strip()
    if not target_id:
        return False, "Задание не найдено."
    if actor_role not in _views().CREATE_ROLES:
        return False, "Доступ запрещен."
    task = (
        MoveTask.objects.select_related("request")
        .filter(legacy_order_id=target_id)
        .order_by("-updated_at")
        .first()
    )
    if not task:
        return False, "Задание не найдено."
    payload = dict(task.payload or {})
    assigned_to_id = task.assigned_to_id
    if assigned_to_id is None:
        try:
            assigned_to_id = int(payload.get("assigned_to_id") or 0) or None
        except (TypeError, ValueError):
            assigned_to_id = None
    if task.status == MoveTask.STATUS_CANCELED:
        return False, "Задание уже отменено."
    if task.status == MoveTask.STATUS_DONE:
        return False, "Задание уже выполнено и не может быть удалено."
    if task.status != MoveTask.STATUS_CREATED or assigned_to_id:
        return False, "Задание уже взято в работу и не может быть удалено."

    updated = sync_task_status_by_legacy_order_id(
        target_id,
        status=MoveTask.STATUS_CANCELED,
    )
    if not updated:
        return False, "Не удалось обновить статус задания."
    updated_payload = dict(updated.payload or {})
    updated_payload["status"] = MoveTask.STATUS_CANCELED
    updated_payload["status_label"] = "Отменено до начала выполнения"
    updated.payload = updated_payload
    updated.save(update_fields=["payload", "updated_at"])

    warehouse_task_id = updated_payload.get("warehouse_operation_task_id")
    warehouse_operation_id = updated_payload.get("warehouse_operation_id")
    warehouse_task = None
    if warehouse_task_id:
        try:
            warehouse_task = WarehouseOperationTask.objects.select_related("operation").get(id=int(warehouse_task_id))
        except (WarehouseOperationTask.DoesNotExist, TypeError, ValueError):
            warehouse_task = None
    if warehouse_task:
        warehouse_task.status = WarehouseOperationTask.STATUS_CANCELED
        warehouse_task.payload = {
            **dict(warehouse_task.payload or {}),
            "legacy_move_id": target_id,
            "status_label": "Отменено до начала выполнения",
        }
        warehouse_task.save(update_fields=["status", "payload", "updated_at"])
        operation = warehouse_task.operation
    else:
        operation = None
        if warehouse_operation_id:
            try:
                operation = WarehouseOperation.objects.get(id=int(warehouse_operation_id))
            except (WarehouseOperation.DoesNotExist, TypeError, ValueError):
                operation = None
    if operation:
        statuses = list(operation.tasks.values_list("status", flat=True))
        if statuses and all(status == WarehouseOperationTask.STATUS_CANCELED for status in statuses):
            operation.status = WarehouseOperation.STATUS_CANCELED
            operation.save(update_fields=["status", "updated_at"])
    return True, "Задание удалено."


@transaction.atomic
def edit_move_destination_before_take(
    *,
    legacy_order_id: str,
    actor_role: str,
    destination_data: dict,
    user=None,
) -> tuple[bool, str]:
    target_id = str(legacy_order_id or "").strip()
    if not target_id:
        return False, "Задание не найдено."
    if actor_role not in _views().CREATE_ROLES:
        return False, "Доступ запрещен."
    task = (
        MoveTask.objects.select_related("request")
        .filter(legacy_order_id=target_id)
        .order_by("-updated_at")
        .first()
    )
    if not task:
        return False, "Задание не найдено."
    payload = dict(task.payload or {})
    assigned_to_id = task.assigned_to_id
    if assigned_to_id is None:
        try:
            assigned_to_id = int(payload.get("assigned_to_id") or 0) or None
        except (TypeError, ValueError):
            assigned_to_id = None
    if task.status == MoveTask.STATUS_CANCELED:
        return False, "Задание уже отменено."
    if task.status == MoveTask.STATUS_DONE:
        return False, "Задание уже выполнено."
    if task.status != MoveTask.STATUS_CREATED or assigned_to_id:
        return False, "Задание уже взято в работу и не может быть изменено."

    destination = destination_from_request_data_service(destination_data or {})
    zone = _normalize_zone_code(destination.get("zone") or "")
    row = _parse_int_value(destination.get("row"))
    section = _parse_int_value(destination.get("section"))
    tier = _parse_int_value(destination.get("tier"))
    cell = _parse_int_value(destination.get("cell"))
    if zone not in _views().ALLOWED_ZONES:
        return False, "Некорректная зона назначения."
    if zone == "MR" and not row:
        return False, "Для зоны MR укажите ряд."
    if zone == "OS" and not (row and section and tier and cell):
        return False, "Для зоны OS укажите ряд, секцию, ярус и ячейку."

    location = WarehouseWritePathService.ensure_location(
        warehouse_code="MSK",
        zone_code=zone or "PR",
        row_no=row,
        section_no=section,
        tier_no=tier,
        cell_no=cell,
    )
    location_label = _location_label(destination)

    task.to_zone = zone or ""
    task.to_row = row or None
    task.to_section = section or None
    task.to_tier = tier or None
    task.to_cell = cell or None
    payload["to_location"] = destination
    payload["to_label"] = location_label
    payload["destination_label"] = location_label
    task.payload = payload
    task.save(
        update_fields=[
            "to_zone",
            "to_row",
            "to_section",
            "to_tier",
            "to_cell",
            "payload",
            "updated_at",
        ]
    )

    move_request = task.request
    if move_request:
        move_request.destination_zone = zone or ""
        move_request.destination_row = row or None
        move_request.destination_section = section or None
        move_request.destination_tier = tier or None
        move_request.destination_cell = cell or None
        move_request.save(
            update_fields=[
                "destination_zone",
                "destination_row",
                "destination_section",
                "destination_tier",
                "destination_cell",
                "updated_at",
            ]
        )

    warehouse_task_id = payload.get("warehouse_operation_task_id")
    warehouse_operation_id = payload.get("warehouse_operation_id")
    warehouse_task = None
    if warehouse_task_id:
        try:
            warehouse_task = WarehouseOperationTask.objects.select_related("operation").get(id=int(warehouse_task_id))
        except (WarehouseOperationTask.DoesNotExist, TypeError, ValueError):
            warehouse_task = None
    if warehouse_task:
        warehouse_task.to_location = location
        warehouse_task.to_zone_code = zone or ""
        warehouse_payload = dict(warehouse_task.payload or {})
        warehouse_payload["destination_label"] = location_label
        warehouse_task.payload = warehouse_payload
        warehouse_task.save(update_fields=["to_location", "to_zone_code", "payload", "updated_at"])
        operation = warehouse_task.operation
    else:
        operation = None
        if warehouse_operation_id:
            try:
                operation = WarehouseOperation.objects.get(id=int(warehouse_operation_id))
            except (WarehouseOperation.DoesNotExist, TypeError, ValueError):
                operation = None
    if operation:
        operation.destination_location = location
        operation.destination_zone_code = zone or ""
        operation.save(update_fields=["destination_location", "destination_zone_code", "updated_at"])

    log_order_action(
        "update",
        order_id=target_id,
        order_type="stock_move",
        user=user if getattr(user, "is_authenticated", False) else None,
        agency=getattr(move_request, "agency", None),
        description=f"Изменено место назначения: {location_label}",
        payload=payload,
    )
    return True, "Заявка ричтраку обновлена."


def collect_moves(employee_id: int | None, driver_view: bool) -> tuple[list[dict], list[dict]]:
    reachtruck_views = _views()
    closed_statuses = {"done", "canceled", "cancelled"}
    entries = (
        OrderAuditEntry.objects.filter(order_type="stock_move")
        .select_related("user", "agency")
        .order_by("order_id", "created_at")
    )
    latest_by_order = {}
    created_at_by_order = {}
    for entry in entries:
        created_at_by_order.setdefault(entry.order_id, entry.created_at)
        latest_by_order[entry.order_id] = entry
    move_tasks_by_order = {
        str(task.legacy_order_id or "").strip(): task
        for task in MoveTask.objects.select_related("request").filter(
            legacy_order_id__in=list(latest_by_order.keys())
        )
    }

    shipping_destinations: dict[int, str] = {}
    shipping_order_pks = {
        _parse_int_value((entry.payload or {}).get("shipping_order_pk"))
        for entry in latest_by_order.values()
        if _parse_int_value((entry.payload or {}).get("shipping_order_pk")) > 0
    }
    if shipping_order_pks:
        try:
            from shipping.models import ShippingOrder

            shipping_destinations = {
                int(order.pk): str(order.destination_warehouse or order.destination_address or "").strip()
                for order in ShippingOrder.objects.filter(pk__in=shipping_order_pks)
            }
        except Exception:
            shipping_destinations = {}

    moves = []
    for order_id, entry in latest_by_order.items():
        payload = entry.payload or {}
        move_task = move_tasks_by_order.get(str(order_id or "").strip())
        move_request = move_task.request if move_task else None
        request_context_type = str(getattr(move_request, "context_type", "") or "").strip().lower()
        request_context_id = str(getattr(move_request, "context_id", "") or "").strip()
        task_payload = dict(move_task.payload or {}) if move_task and isinstance(move_task.payload, dict) else {}
        effective_payload = {**payload, **task_payload}
        status = str(
            (move_task.status if move_task else "")
            or effective_payload.get("status")
            or effective_payload.get("submit_action")
            or ""
        ).strip().lower()
        status_label = (
            str(effective_payload.get("status_label") or "").strip()
            or {
                MoveTask.STATUS_CREATED: "Ожидает перевозки",
                MoveTask.STATUS_IN_PROGRESS: "В работе",
                MoveTask.STATUS_DONE: "Выполнено",
                MoveTask.STATUS_CANCELED: "Отменено",
                MoveTask.STATUS_FAILED: "Ошибка",
            }.get(status, status or "-")
        )
        pallet_code = str(effective_payload.get("pallet_code") or "").strip()
        from_location = effective_payload.get("from_location") or {}
        to_location = effective_payload.get("to_location") or {}
        assigned_to_id = move_task.assigned_to_id if move_task and move_task.assigned_to_id else effective_payload.get("assigned_to_id")
        assigned_to_name = (
            move_task.assigned_to_name
            if move_task and move_task.assigned_to_name
            else effective_payload.get("assigned_to_name") or "-"
        )
        if assigned_to_id:
            try:
                assigned_to_id = int(assigned_to_id)
            except (TypeError, ValueError):
                assigned_to_id = None
        source_type, source_id = _source_order_identity(
            effective_payload,
            request_context_type=request_context_type,
            request_context_id=request_context_id,
        )
        move = {
            "order_id": order_id,
            "agency_id": entry.agency_id,
            "created_at": created_at_by_order.get(order_id) or (move_task.created_at if move_task else entry.created_at) or entry.created_at,
            "updated_at": (move_task.updated_at if move_task else entry.created_at) or entry.created_at,
            "status": status or "-",
            "status_label": status_label,
            "task_kind_label": _task_kind_label(effective_payload),
            "pallet_code": pallet_code or "-",
            "from_location": from_location,
            "to_location": to_location,
            "from_label": _location_label(from_location),
            "to_label": _location_label(to_location),
            "assigned_to_id": assigned_to_id,
            "assigned_to_name": assigned_to_name,
            "requested_by_name": effective_payload.get("requested_by_name") or "-",
            "receiving_order_id": (
                source_id if source_type == "receiving" else ""
                or "-"
            ),
            "pick_mode": (effective_payload.get("pick_mode") or "full"),
            "move_mode": _normalize_move_mode(effective_payload.get("move_mode"), effective_payload.get("pick_mode")),
            "requested_qty": _parse_int_value(effective_payload.get("requested_qty")),
            "picked_qty": _parse_int_value(effective_payload.get("picked_qty")),
            "requested_sku": (effective_payload.get("requested_sku") or "").strip(),
            "requested_barcode_qty": _requested_barcode_qty(effective_payload),
            "requested_rows": _requested_partial_rows(effective_payload),
            "requested_boxes": _payload_box_codes(effective_payload),
            "requested_box": _single_requested_box(effective_payload),
            "instruction": _move_instruction(effective_payload),
        }
        pallet_plan = pallet_box_plan_service(
            effective_payload,
            move["pallet_code"],
            agency_id=entry.agency_id,
        )
        move["pallet_box_plan"] = pallet_plan
        move["box_execution_plan"] = box_execution_plan_service(
            effective_payload,
            move["pallet_code"],
            agency_id=entry.agency_id,
            pallet_plan=pallet_plan,
        )
        shipping_order_pk = _parse_int_value(effective_payload.get("shipping_order_pk"))
        move["agency_name"] = str(getattr(entry.agency, "agn_name", "") or "").strip() or "Без клиента"
        move["agency_name_short"] = _short_agency_name(move["agency_name"])
        move["mobile_category"] = mobile_category_key(move, effective_payload)
        move["mobile_number"] = mobile_number_label(
            move["mobile_category"],
            effective_payload,
            order_id,
            shipping_order_pk,
            request_context_type=request_context_type,
            request_context_id=request_context_id,
        )
        move["mobile_request_key"] = mobile_request_key(
            effective_payload,
            order_id,
            request_context_type=request_context_type,
            request_context_id=request_context_id,
        )
        (
            _mobile_request_source,
            _mobile_request_source_id,
            move["mobile_request_label"],
            move["mobile_request_type_label"],
        ) = mobile_request_identity(
            effective_payload,
            order_id,
            request_context_type=request_context_type,
            request_context_id=request_context_id,
        )
        move["mobile_destination"] = (
            shipping_destinations.get(shipping_order_pk)
            or str(effective_payload.get("destination_label") or "").strip()
            or move["to_label"]
        )
        move["mobile_route_summary"] = _mobile_request_route_summary(move)
        move["mobile_route_detail"] = _mobile_route_detail(move)
        move["source_url"], move["source_label"], move["source_type_label"] = move_source_link(
            effective_payload,
            order_id,
            request_context_type=request_context_type,
            request_context_id=request_context_id,
        )
        if driver_view:
            if status in closed_statuses:
                moves.append(move)
                continue
            if assigned_to_id and employee_id and assigned_to_id != employee_id:
                continue
        moves.append(move)

    moves.sort(key=lambda item: item["updated_at"], reverse=True)
    active = [move for move in moves if move["status"] not in closed_statuses]
    done = [move for move in moves if move["status"] in closed_statuses][:10]
    for move in active:
        move["can_take"] = driver_view and move["status"] == "created" and not move["assigned_to_id"]
        move["can_complete"] = (
            driver_view and move["status"] == "in_progress" and move["assigned_to_id"] and move["assigned_to_id"] == employee_id
        )
        move["can_manage"] = False
    for move in done:
        move["can_take"] = False
        move["can_complete"] = False
        move["can_manage"] = False
    return active, done


def build_dashboard_context(request, **kwargs) -> dict:
    role = get_request_role(request)
    employee = get_request_employee(request)
    employee_id = employee.id if employee else None
    ctx: dict = {}
    ctx["role"] = role
    ctx["is_driver"] = role == "reachtruck_driver"
    ctx["can_create"] = False
    ctx["cabinet_url"] = resolve_cabinet_url(role)
    ctx.update(dashboard_identity(role))
    ctx["error"] = kwargs.get("error")
    mobile_flash_state = kwargs.get("mobile_flash_state") or ""
    mobile_category = str(kwargs.get("mobile_category") or request.GET.get("mobile_category") or request.POST.get("mobile_category") or "").strip().lower()
    mobile_request_key = str(kwargs.get("mobile_request_key") or request.GET.get("mobile_request") or request.POST.get("mobile_request") or "").strip()
    mobile_task_id = str(kwargs.get("mobile_task_id") or request.GET.get("mobile_task") or request.POST.get("mobile_task") or "").strip()
    if request.GET.get("ok") == "1":
        order_id = str(request.GET.get("order") or "").strip()
        ctx["ok_message"] = f"Задание №{order_id} создано." if order_id else "Задание создано."
    if request.GET.get("mobile_done") == "1":
        ctx["ok_message"] = "Задание ричтрака выполнено."
        mobile_flash_state = mobile_flash_state or "success"
    if kwargs.get("ok_message"):
        ctx["ok_message"] = kwargs.get("ok_message")
        mobile_flash_state = mobile_flash_state or "success"
    active_moves, done_moves = collect_moves(employee_id, ctx["is_driver"])
    ctx["moves_active"] = active_moves
    ctx["moves_done"] = done_moves
    category_counts = {key: 0 for key, _label in _views().MOBILE_MOVE_CATEGORIES}
    for move in active_moves:
        category_counts[move["mobile_category"]] = category_counts.get(move["mobile_category"], 0) + 1
    ctx["mobile_categories"] = [
        {
            "key": key,
            "label": label,
            "count": category_counts.get(key, 0),
            "url": f"/reachtruck/?mobile_category={key}",
        }
        for key, label in _views().MOBILE_MOVE_CATEGORIES
    ]
    ctx["mobile_category"] = mobile_category
    ctx["mobile_category_label"] = mobile_category_label(mobile_category) if mobile_category else ""
    category_moves = [move for move in active_moves if not mobile_category or move["mobile_category"] == mobile_category]
    inferred_task = next((move for move in category_moves if str(move["order_id"]) == mobile_task_id), None)
    if inferred_task and not mobile_request_key:
        mobile_request_key = inferred_task["mobile_request_key"]

    request_groups: list[dict] = []
    request_lookup: dict[str, dict] = {}
    for move in category_moves:
        request_key = move["mobile_request_key"]
        group = request_lookup.get(request_key)
        if not group:
            group = {
                "key": request_key,
                "label": move["mobile_request_label"],
                "type_label": move["mobile_request_type_label"],
                "agency_name": move["agency_name"],
                "agency_name_short": move["agency_name_short"],
                "source_url": move.get("source_url") or "",
                "source_label": move.get("source_label") or "",
                "source_type_label": move.get("source_type_label") or "",
                "count": 0,
                "count_label": "",
                "destinations": [],
                "summary": _mobile_request_route_summary(move),
                "url": mobile_request_url(mobile_category, request_key) if mobile_category else "/reachtruck/",
                "tasks": [],
            }
            request_lookup[request_key] = group
            request_groups.append(group)
        group["count"] += 1
        group["count_label"] = _task_count_label(group["count"])
        group["tasks"].append(move)
        destination = str(move["mobile_destination"] or move["to_label"] or "").strip()
        if destination and destination not in group["destinations"]:
            group["destinations"].append(destination)

    selected_request = request_lookup.get(mobile_request_key) if mobile_request_key else None
    if mobile_request_key and not selected_request:
        mobile_request_key = ""
    request_tasks = list(selected_request["tasks"]) if selected_request else []
    ctx["mobile_requests"] = request_groups
    ctx["mobile_request_key"] = mobile_request_key
    ctx["mobile_request_label"] = selected_request["label"] if selected_request else ""
    ctx["mobile_request_tasks"] = request_tasks
    ctx["mobile_selected_request"] = selected_request
    ctx["mobile_tasks"] = category_moves
    ctx["mobile_selected_task"] = next(
        (move for move in request_tasks if str(move["order_id"]) == mobile_task_id),
        None,
    )
    ctx["mobile_selected_execution"] = (
        build_mobile_execution_snapshot(ctx["mobile_selected_task"]["order_id"])
        if ctx["mobile_selected_task"]
        else {}
    )
    can_manage_moves = role in _views().CREATE_ROLES and role != "reachtruck_driver"
    for move in active_moves:
        move["can_manage"] = bool(
            can_manage_moves
            and move["status"] == MoveTask.STATUS_CREATED
            and not move["assigned_to_id"]
        )
    ctx["mobile_flash_state"] = mobile_flash_state
    return ctx


def handle_dashboard_post(view, request, *args, **kwargs):
    role = get_request_role(request)
    employee = get_request_employee(request)
    employee_id = employee.id if employee else None
    employee_name = employee.full_name if employee else request.user.get_full_name() or request.user.username
    mobile_category = str(request.POST.get("mobile_category") or "").strip().lower()
    mobile_request_key = str(request.POST.get("mobile_request") or "").strip()
    mobile_task = str(request.POST.get("mobile_task") or request.POST.get("order_id") or "").strip()
    action = str(request.POST.get("action") or "").strip()
    if action == "create_move":
        return view._render_error(
            "Ручное создание заданий отключено. Используйте автоматическое планирование по потребности или сценарий перемещения на хранение.",
            status=403,
        )
    if action == "cancel_move":
        ok, message = cancel_move_before_take(
            legacy_order_id=str(request.POST.get("order_id") or "").strip(),
            actor_role=role or "",
        )
        if not ok:
            return view._render_error(message, status=400)
        if mobile_category:
            target_params = {"mobile_category": mobile_category}
            if mobile_request_key:
                target_params["mobile_request"] = mobile_request_key
            return redirect(f"/reachtruck/?{urlencode(target_params)}")
        return redirect("/reachtruck/")
    if action == "edit_move_destination":
        ok, message = edit_move_destination_before_take(
            legacy_order_id=str(request.POST.get("order_id") or "").strip(),
            actor_role=role or "",
            destination_data={
                "to_zone": request.POST.get("to_zone") or "PR",
                "to_row": request.POST.get("to_row"),
                "to_section": request.POST.get("to_section"),
                "to_tier": request.POST.get("to_tier"),
                "to_cell": request.POST.get("to_cell"),
            },
            user=request.user if request.user.is_authenticated else None,
        )
        if not ok:
            return view._render_error(message, status=400)
        if mobile_category:
            target_params = {"mobile_category": mobile_category}
            if mobile_request_key:
                target_params["mobile_request"] = mobile_request_key
            if mobile_task:
                target_params["mobile_task"] = mobile_task
            return redirect(f"/reachtruck/?{urlencode(target_params)}")
        return redirect("/reachtruck/")
    if action == "take_move":
        if role != "reachtruck_driver":
            return HttpResponseForbidden("Доступ запрещен")
        result = take_move_task(
            legacy_order_id=str(request.POST.get("order_id") or "").strip(),
            user=request.user,
            employee_id=employee_id,
            employee_name=employee_name,
        )
        if not result.ok:
            return view._render_error(result.error)
        if mobile_category and mobile_task:
            ctx = view.get_context_data(
                mobile_category=mobile_category,
                mobile_request_key=mobile_request_key,
                mobile_task_id=mobile_task,
                ok_message="Задание взято в работу.",
                mobile_flash_state="success",
            )
            return view.render_to_response(ctx)
        return redirect("/reachtruck/")
    if action == "scan_move":
        if role != "reachtruck_driver":
            return HttpResponseForbidden("Доступ запрещен")
        result = scan_move_task_step(
            legacy_order_id=str(request.POST.get("order_id") or "").strip(),
            scan_value=str(request.POST.get("scan_value") or "").strip(),
            user=request.user,
            employee_id=employee_id,
            employee_name=employee_name,
        )
        if not result.ok:
            ctx = view.get_context_data(
                mobile_category=mobile_category,
                mobile_request_key=mobile_request_key,
                mobile_task_id=mobile_task,
                error=result.error,
                mobile_flash_state="error",
            )
            return view.render_to_response(ctx, status=400)
        if result.completed:
            target_params = {"mobile_done": 1}
            if mobile_category:
                target_params["mobile_category"] = mobile_category
            if mobile_request_key:
                target_params["mobile_request"] = mobile_request_key
            target = f"/reachtruck/?{urlencode(target_params)}"
            return redirect(target)
        ctx = view.get_context_data(
            mobile_category=mobile_category,
            mobile_request_key=mobile_request_key,
            mobile_task_id=mobile_task,
            ok_message=result.message,
            mobile_flash_state="success",
        )
        return view.render_to_response(ctx)
    if action == "complete_move":
        if role != "reachtruck_driver":
            return HttpResponseForbidden("Доступ запрещен")
        result = complete_move_task(
            legacy_order_id=str(request.POST.get("order_id") or "").strip(),
            user=request.user,
            employee_id=employee_id,
            employee_name=employee_name,
        )
        if not result.ok:
            return view._render_error(result.error)
        if mobile_category:
            target_params = {"mobile_category": mobile_category, "mobile_done": 1}
            if mobile_request_key:
                target_params["mobile_request"] = mobile_request_key
            return redirect(f"/reachtruck/?{urlencode(target_params)}")
        return redirect("/reachtruck/")
    return view.get(request, *args, **kwargs)
