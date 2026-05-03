from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from itertools import combinations
import json

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from audit.models import OrderAuditEntry, log_order_action, log_stock_move
from reachtruck.models import MoveRequest, MoveRequestItem, MoveTask
from sklad.models import StockPalletState
from sklad.services import OperationalStockService
from sklad.services.stock_availability import StockAvailabilityService
from sklad.services.warehouse_stock_rows import legacy_stock_rows, snapshot_stock_rows
from sklad.stock_state import rebuild_stock_snapshot

from .pallet_ops import (
    MOVE_MODE_BOX_FULL,
    MOVE_MODE_BOX_PARTIAL,
    MOVE_MODE_PALLET_FULL,
    _normalize_barcode_qty_map,
    _normalize_box_code,
    _normalize_move_mode,
    _parse_json_list,
    _payload_box_codes,
    _requested_barcode_qty,
    _requested_partial_rows,
    _single_requested_box,
)


def _as_int(value) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _normalize_zone_code(raw: str) -> str:
    text = (raw or "").strip().upper()
    if not text:
        return "PR"
    if text in {"PR", "OTG", "MR", "OS", "OBR"}:
        return text
    return text


def _normalize_location(raw: dict | None) -> dict:
    source = raw if isinstance(raw, dict) else {}
    zone = _normalize_zone_code(source.get("zone") or "")
    row = _as_int(source.get("row"))
    section = _as_int(source.get("section"))
    tier = _as_int(source.get("tier"))
    cell = _as_int(source.get("cell"))
    if zone not in {"MR", "OS"}:
        row = 0
    if zone != "OS":
        section = 0
        tier = 0
        cell = 0
    return {
        "zone": zone,
        "row": row if row > 0 else "",
        "section": section if section > 0 else "",
        "tier": tier if tier > 0 else "",
        "cell": cell if cell > 0 else "",
    }


def _build_location(zone: str, row: int, section: int, tier: int, cell: int) -> dict:
    return _normalize_location(
        {
            "zone": zone,
            "row": row,
            "section": section,
            "tier": tier,
            "cell": cell,
        }
    )


def _normalize_goods_type(raw: str | None) -> str:
    return StockAvailabilityService.normalize_goods_type(raw)


def _shipping_task_kind_label(move_mode: str) -> str:
    if str(move_mode or "").strip() == MoveTask.MODE_PALLET_FULL:
        return "Паллета целиком"
    return "Частичный отбор с палеты для отгрузки"


def _shipping_full_pallet_instruction(*, pallet_code: str, destination_label: str) -> str:
    return f"Возьми палету {pallet_code} целиком и доставь в {destination_label}."


def _shipping_pick_instruction(
    *,
    pallet_code: str,
    requested_qty: int,
    barcode_qty: dict[str, int],
    destination_label: str,
    source_label: str,
) -> str:
    qty_label = f"{max(int(requested_qty or 0), 0)} шт."
    if barcode_qty:
        preview_rows = [f"{barcode} - {qty} шт." for barcode, qty in sorted(barcode_qty.items())[:6]]
        tail = "; ..." if len(barcode_qty) > 6 else ""
        details = "; ".join(preview_rows)
        return (
            f"Частичный отбор для отгрузки: возьми палету {pallet_code}, "
            f"отбери {qty_label} по списку ШК ({details}{tail}) и доставь отобранный товар в {destination_label}. "
            f"Остаток товара оставь на этой же палете и верни палету обратно на исходное место ({source_label})."
        )
    return (
        f"Частичный отбор для отгрузки: возьми палету {pallet_code}, "
        f"отбери {qty_label} по потребности и доставь отобранный товар в {destination_label}. "
        f"Остаток товара оставь на этой же палете и верни палету обратно на исходное место ({source_label})."
    )


def _shipping_entry_covers_full_pallet(
    pallet_code: str,
    *,
    remaining_by_row_id: dict[int, int],
    pallet_row_ids: dict[str, list[int]],
) -> bool:
    row_ids = list(pallet_row_ids.get(str(pallet_code or "").strip()) or [])
    if not row_ids:
        return False
    for row_id in row_ids:
        if int(remaining_by_row_id.get(row_id, 0) or 0) > 0:
            return False
    return True


def _location_label(location: dict | None) -> str:
    data = _normalize_location(location if isinstance(location, dict) else {})
    zone = data.get("zone") or "PR"
    row = _as_int(data.get("row"))
    section = _as_int(data.get("section"))
    tier = _as_int(data.get("tier"))
    cell = _as_int(data.get("cell"))
    if zone == "PR":
        return "PR · Зона приемки"
    if zone == "OBR":
        return "OBR · Зона обработки"
    if zone == "OTG":
        return "OTG · Зона отгрузки"
    if zone == "MR":
        return f"MR · Между рядами · Ряд {row}" if row else "MR · Между рядами"
    if zone == "OS":
        if row and section and tier and cell:
            return f"OS · Ряд {row} · Секция {section} · Ярус {tier} · Ячейка {cell}"
        if row:
            return f"OS · Ряд {row}"
        return "OS · Основной склад"
    return zone


def _location_parts(location_value, pallet=None) -> dict:
    pallet = pallet or {}
    zone = ""
    row = 0
    section = 0
    tier = 0
    cell = 0
    if isinstance(location_value, dict):
        zone = _normalize_zone_code(location_value.get("zone") or "")
        row = _as_int(location_value.get("row") or pallet.get("row"))
        section = _as_int(location_value.get("section"))
        tier = _as_int(location_value.get("tier"))
        cell = _as_int(location_value.get("cell"))
    elif isinstance(location_value, str):
        zone = _normalize_zone_code(location_value)
    if not zone:
        zone = _normalize_zone_code(pallet.get("zone") or "")
    if zone == "OS":
        row = row or _as_int(pallet.get("row"))
        section = section or _as_int(pallet.get("section"))
        tier = tier or _as_int(pallet.get("tier"))
        cell = cell or _as_int(pallet.get("cell"))
    if zone == "MR" and not row:
        row = _as_int(pallet.get("row"))
    return {
        "zone": zone or "PR",
        "row": row,
        "section": section,
        "tier": tier,
        "cell": cell,
    }


def _find_pallet_by_code(
    code: str,
    agency_id: int | None = None,
    pallet_lookup: tuple[dict[tuple[int, str], tuple], dict[str, tuple]] | None = None,
):
    del pallet_lookup
    target = (code or "").strip()
    if not target:
        return None
    stock_tree = OperationalStockService.get_pallet_tree(target, agency_id=agency_id)
    if stock_tree is None:
        return None
    pallet = ((stock_tree.payload.get("act_pallets") or [{}])[0]) or {}
    location = _location_parts(pallet.get("location"), pallet)
    return stock_tree.synthetic_entry, 0, pallet, location


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
    if not isinstance(payload, dict):
        return ""
    explicit_instruction = str(payload.get("instruction") or "").strip()
    if explicit_instruction:
        return explicit_instruction
    pallet_code = str(payload.get("pallet_code") or "").strip() or "палету"
    mode = _normalize_move_mode(payload.get("move_mode"), payload.get("pick_mode"))
    requested_qty = _as_int(payload.get("requested_qty"))
    requested_sku = str(payload.get("requested_sku") or "").strip()
    requested_goods_type = _normalize_goods_type(payload.get("requested_goods_type"))
    requested_barcodes_raw = payload.get("requested_barcodes")
    if isinstance(requested_barcodes_raw, list):
        requested_barcodes = [
            str(value).strip() for value in requested_barcodes_raw if str(value or "").strip()
        ]
    else:
        requested_barcodes = _parse_json_list(requested_barcodes_raw)
    requested_barcode_qty = _requested_barcode_qty(payload)
    requested_rows = _requested_partial_rows(payload)
    product_parts = []
    if requested_sku:
        product_parts.append(f"артикул {requested_sku}")
    if requested_barcodes and not requested_barcode_qty:
        preview = requested_barcodes[:2]
        tail = "…" if len(requested_barcodes) > 2 else ""
        product_parts.append(f"ШК {', '.join(preview)}{tail}")
    if requested_goods_type:
        product_parts.append(f"тип {requested_goods_type}")
    product_label = "; ".join(product_parts)
    box_codes = _payload_box_codes(payload)
    if mode == MOVE_MODE_PALLET_FULL:
        return f"Возьми палету {pallet_code} целиком и доставь в назначенную зону."
    if mode == MOVE_MODE_BOX_FULL:
        if box_codes:
            return (
                f"Возьми палету {pallet_code}, отдай короба: {', '.join(box_codes)}. "
                "Разбирать короба не нужно."
            )
        return f"Возьми палету {pallet_code} и отдай указанные короба без разбора."
    if len(requested_rows) > 1:
        steps = []
        for row in requested_rows[:5]:
            row_box = _normalize_box_code(row.get("box_code"))
            row_qty = _as_int(row.get("qty"))
            if not row_box or row_qty <= 0:
                continue
            steps.append(f"{row_box} - {row_qty} шт.")
        if steps:
            tail = "; …" if len(requested_rows) > 5 else ""
            total_qty = requested_qty if requested_qty > 0 else sum(_as_int(row.get("qty")) for row in requested_rows)
            total_label = f"{total_qty} шт." if total_qty > 0 else "указанное количество"
            product_suffix = f" Товар: {product_label}." if product_label else ""
            return (
                f"Возьми палету {pallet_code}, выполни отбор из коробов: {'; '.join(steps)}{tail}. "
                f"Общий отбор: {total_label}.{product_suffix} "
                "Остаток оставь в коробах и верни палету на место."
            )
    if not _single_requested_box(payload) and not box_codes:
        qty_label = f"{requested_qty} шт." if requested_qty > 0 else "указанное количество"
        product_suffix = f" Товар: {product_label}." if product_label else ""
        return (
            f"Возьми палету {pallet_code} и отберите {qty_label}."
            f"{product_suffix} "
            "Короб выбери по месту, остаток оставь на палете."
        )
    box_code = _single_requested_box(payload) or "указанный короб"
    qty_label = f"{requested_qty} шт." if requested_qty > 0 else "указанное количество"
    product_suffix = f" Товар: {product_label}." if product_label else ""
    return (
        f"Возьми палету {pallet_code}, вытащи короб {box_code} и отбери {qty_label}."
        f"{product_suffix} "
        "Остаток оставь в коробе."
    )


def _latest_moves_by_pallet(
    processing_order_id: str | None = None,
    barcode_values: set[str] | None = None,
    sku_values: set[str] | None = None,
    goods_type_values: set[str] | None = None,
) -> dict[str, dict]:
    processing_order_id = str(processing_order_id or "").strip()
    entries = OrderAuditEntry.objects.filter(order_type="stock_move").order_by("-created_at")
    latest = {}
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
        move_mode = _normalize_move_mode(payload.get("move_mode"), payload.get("pick_mode"))
        requested_boxes = _payload_box_codes(payload)
        latest[pallet_code] = {
            "status": status,
            "status_label": status_label,
            "to_label": _location_label(to_location),
            "to_zone": _normalize_zone_code(to_location.get("zone") or ""),
            "order_id": entry.order_id,
            "pick_mode": (payload.get("pick_mode") or "full"),
            "move_mode": move_mode,
            "requested_qty": _as_int(payload.get("requested_qty")),
            "picked_qty": _as_int(payload.get("picked_qty")),
            "requested_sku": str(payload.get("requested_sku") or "").strip(),
            "requested_barcodes": requested_barcodes,
            "requested_barcode_qty": _requested_barcode_qty(payload),
            "requested_boxes": requested_boxes,
            "requested_box": _single_requested_box(payload),
            "requested_rows": _requested_partial_rows(payload),
            "requested_goods_type": _normalize_goods_type(
                payload.get("requested_goods_type") or payload.get("requested_goods_type_label")
            ),
            "processing_order_id": move_processing_order_id,
            "instruction": _move_instruction(payload),
        }
    return latest


def _parse_move_request_items(raw_items, fallback_form: dict | None = None) -> list[dict]:
    items = []
    data = raw_items
    if isinstance(raw_items, str):
        text = raw_items.strip()
        if not text:
            data = []
        else:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = []
    if not isinstance(data, list):
        data = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        sku = str(raw.get("requested_article") or raw.get("sku_code") or "").strip()
        goods_type = _normalize_goods_type(raw.get("requested_goods_type") or raw.get("goods_type"))
        qty = _as_int(raw.get("requested_qty") or raw.get("qty"))
        barcodes_raw = raw.get("requested_barcodes")
        if isinstance(barcodes_raw, list):
            barcodes = [str(value).strip() for value in barcodes_raw if str(value or "").strip()]
        else:
            barcodes = _parse_json_list(barcodes_raw)
        if qty <= 0:
            continue
        if not sku and not barcodes:
            continue
        items.append(
            {
                "requested_article": sku,
                "requested_goods_type": goods_type,
                "requested_qty": qty,
                "requested_barcodes": barcodes,
            }
        )
    if items:
        return items
    form = fallback_form or {}
    sku = str(form.get("requested_article") or "").strip()
    goods_type = _normalize_goods_type(form.get("requested_goods_type"))
    qty = _as_int(form.get("requested_qty") or form.get("pick_qty"))
    barcodes = _parse_json_list(form.get("requested_barcodes_json"))
    if qty > 0 and (sku or barcodes):
        return [
            {
                "requested_article": sku,
                "requested_goods_type": goods_type,
                "requested_qty": qty,
                "requested_barcodes": barcodes,
            }
        ]
    return []


def _parse_explicit_requested_rows(raw_rows) -> list[dict]:
    parsed = raw_rows
    if isinstance(raw_rows, str):
        text = raw_rows.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return []
    if not isinstance(parsed, list):
        return []
    result: list[dict] = []
    for row in parsed:
        if not isinstance(row, dict):
            continue
        pallet_code = str(row.get("pallet_code") or "").strip()
        box_code = _normalize_box_code(row.get("box_code"))
        qty = _as_int(row.get("qty"))
        if not pallet_code or not box_code or qty <= 0:
            continue
        requested_article = str(row.get("requested_article") or row.get("sku_code") or "").strip()
        requested_goods_type = _normalize_goods_type(row.get("requested_goods_type") or row.get("goods_type"))
        barcodes_raw = row.get("requested_barcodes")
        if isinstance(barcodes_raw, list):
            requested_barcodes = [str(value).strip() for value in barcodes_raw if str(value or "").strip()]
        else:
            requested_barcodes = _parse_json_list(barcodes_raw)
        result.append(
            {
                "pallet_code": pallet_code,
                "box_code": box_code,
                "qty": qty,
                "barcode_qty": _normalize_barcode_qty_map(row.get("barcode_qty")),
                "requested_article": requested_article,
                "requested_goods_type": requested_goods_type,
                "requested_barcodes": requested_barcodes,
            }
        )
    return result


def _destination_from_request_data(data: dict) -> dict:
    zone = _normalize_zone_code(data.get("to_zone") or "")
    row = _as_int(data.get("to_row"))
    section = _as_int(data.get("to_section"))
    tier = _as_int(data.get("to_tier"))
    cell = _as_int(data.get("to_cell"))
    return _build_location(zone, row, section, tier, cell)


def _request_instruction(pallet_code: str, qty: int, destination: dict, items: list[dict]) -> str:
    parts = []
    if items:
        labels = []
        for item in items[:3]:
            sku = str(item.get("requested_article") or "").strip()
            barcodes = item.get("requested_barcodes") or []
            if sku:
                labels.append(f"арт. {sku}")
            elif barcodes:
                labels.append(f"ШК {barcodes[0]}")
        if labels:
            tail = ", …" if len(items) > 3 else ""
            parts.append(f"товар: {', '.join(labels)}{tail}")
    qty_label = f"{qty} шт." if qty > 0 else "нужное количество"
    return (
        f"Возьми палету {pallet_code}, отберите {qty_label} по потребности "
        f"и доставь в {_location_label(destination)}"
        + (f" ({'; '.join(parts)})." if parts else ".")
    )


def _next_stock_move_number() -> str:
    order_ids = (
        OrderAuditEntry.objects.filter(order_type="stock_move")
        .values_list("order_id", flat=True)
        .distinct()
    )
    max_number = 0
    for order_id in order_ids:
        value = str(order_id or "").strip()
        if not value.isdigit():
            continue
        number = int(value)
        if number > max_number:
            max_number = number
    next_number = max_number + 1
    while OrderAuditEntry.objects.filter(order_type="stock_move", order_id=str(next_number)).exists():
        next_number += 1
    return str(next_number)


def _build_request_items(move_request: MoveRequest, payload: dict) -> None:
    requested_sku = str(payload.get("requested_sku") or "").strip()
    requested_goods_type = str(payload.get("requested_goods_type") or "").strip()
    requested_qty = max(_as_int(payload.get("requested_qty")), 0)

    barcode_qty = payload.get("requested_barcode_qty") or {}
    if isinstance(barcode_qty, dict) and barcode_qty:
        for barcode, qty in barcode_qty.items():
            value = str(barcode or "").strip()
            amount = max(_as_int(qty), 0)
            if not value or amount <= 0:
                continue
            MoveRequestItem.objects.create(
                request=move_request,
                sku_code=requested_sku,
                barcode=value,
                goods_type=requested_goods_type,
                qty_requested=amount,
                qty_planned=amount,
            )
        return

    barcodes = payload.get("requested_barcodes") or []
    if isinstance(barcodes, list):
        normalized = [str(value or "").strip() for value in barcodes if str(value or "").strip()]
    else:
        normalized = []
    if normalized:
        if len(normalized) == 1:
            qty = requested_qty
            MoveRequestItem.objects.create(
                request=move_request,
                sku_code=requested_sku,
                barcode=normalized[0],
                goods_type=requested_goods_type,
                qty_requested=qty,
                qty_planned=qty,
            )
        else:
            for barcode in normalized:
                MoveRequestItem.objects.create(
                    request=move_request,
                    sku_code=requested_sku,
                    barcode=barcode,
                    goods_type=requested_goods_type,
                    qty_requested=0,
                    qty_planned=0,
                )
        return

    if requested_sku or requested_goods_type or requested_qty:
        MoveRequestItem.objects.create(
            request=move_request,
            sku_code=requested_sku,
            barcode="",
            goods_type=requested_goods_type,
            qty_requested=requested_qty,
            qty_planned=requested_qty,
        )


def _recompute_request_status(move_request: MoveRequest) -> None:
    tasks = list(move_request.tasks.values_list("status", flat=True))
    if not tasks:
        new_status = MoveRequest.STATUS_CREATED
    else:
        total = len(tasks)
        done = sum(1 for status in tasks if status == MoveTask.STATUS_DONE)
        in_progress = sum(1 for status in tasks if status == MoveTask.STATUS_IN_PROGRESS)
        created = sum(1 for status in tasks if status == MoveTask.STATUS_CREATED)
        canceled = sum(1 for status in tasks if status == MoveTask.STATUS_CANCELED)
        failed = sum(1 for status in tasks if status == MoveTask.STATUS_FAILED)
        if done == total:
            new_status = MoveRequest.STATUS_DONE
        elif in_progress > 0:
            new_status = MoveRequest.STATUS_IN_PROGRESS
        elif done > 0:
            new_status = MoveRequest.STATUS_PARTIAL
        elif created > 0:
            new_status = MoveRequest.STATUS_PLANNED
        elif canceled == total:
            new_status = MoveRequest.STATUS_CANCELED
        elif failed > 0:
            new_status = MoveRequest.STATUS_BLOCKED
        else:
            new_status = move_request.status
    if move_request.status != new_status:
        move_request.status = new_status
        move_request.save(update_fields=["status", "updated_at"])


def _active_pallet_codes_for_agency(agency_id: int | None) -> set[str]:
    qs = MoveTask.objects.select_related("request").filter(
        status__in=[MoveTask.STATUS_CREATED, MoveTask.STATUS_IN_PROGRESS]
    ).exclude(pallet_code="")
    if agency_id:
        qs = qs.filter(request__agency_id=agency_id)
    return {str(task.pallet_code or "").strip() for task in qs if str(task.pallet_code or "").strip()}


def _row_value(row, field: str, default=None):
    if isinstance(row, dict):
        return row.get(field, default)
    return getattr(row, field, default)


def _warehouse_base_rows_for_planning(
    *,
    agency_id: int | None = None,
    sku_values: set[str] | None = None,
    barcode_values: set[str] | None = None,
) -> list[dict]:
    rows = snapshot_stock_rows(
        agency_id=agency_id,
        sku_values=sku_values,
        barcode_values=barcode_values,
        require_pallet=True,
    )
    if rows:
        return rows

    qs = (
        StockPalletState.objects.filter(state=StockPalletState.STATE_WAREHOUSE)
        .exclude(pallet_code="")
        .order_by("created_at", "id")
    )
    if agency_id:
        qs = qs.filter(agency_id=agency_id)
    if sku_values:
        if barcode_values:
            qs = qs.filter(Q(sku__in=sku_values) | Q(barcode__in=barcode_values))
        else:
            qs = qs.filter(sku__in=sku_values)
    elif barcode_values:
        qs = qs.filter(barcode__in=barcode_values)
    rows = legacy_stock_rows(qs)
    if rows:
        return rows

    try:
        rebuild_stock_snapshot()
    except Exception:
        pass

    qs = (
        StockPalletState.objects.filter(state=StockPalletState.STATE_WAREHOUSE)
        .exclude(pallet_code="")
        .order_by("created_at", "id")
    )
    if agency_id:
        qs = qs.filter(agency_id=agency_id)
    if sku_values:
        if barcode_values:
            qs = qs.filter(Q(sku__in=sku_values) | Q(barcode__in=barcode_values))
        else:
            qs = qs.filter(sku__in=sku_values)
    elif barcode_values:
        qs = qs.filter(barcode__in=barcode_values)
    return legacy_stock_rows(qs)


def _candidate_pallets_for_rows(
    rows,
    *,
    remaining_by_row_id: dict[int, int] | None = None,
    blocked_pallets: set[str] | None = None,
    requested_goods_type: str = "",
) -> list[dict]:
    grouped: dict[str, dict] = {}
    blocked_pallets = blocked_pallets or set()
    for row in rows:
        pallet_code = str(_row_value(row, "pallet_code", "") or "").strip()
        if not pallet_code or pallet_code in blocked_pallets:
            continue
        row_goods_type = _normalize_goods_type(_row_value(row, "goods_type", ""))
        if requested_goods_type and row_goods_type and row_goods_type != requested_goods_type:
            continue
        if remaining_by_row_id is None:
            available_qty = int(_row_value(row, "qty", 0) or 0)
        else:
            available_qty = int(remaining_by_row_id.get(_as_int(_row_value(row, "id", 0)), 0) or 0)
        if available_qty <= 0:
            continue
        entry = grouped.setdefault(
            pallet_code,
            {
                "pallet_code": pallet_code,
                "available_qty": 0,
                "rows": [],
                "from_location": _build_location(
                    _row_value(row, "zone", ""),
                    _as_int(_row_value(row, "row", 0)),
                    _as_int(_row_value(row, "section", 0)),
                    _as_int(_row_value(row, "tier", 0)),
                    _as_int(_row_value(row, "cell", 0)),
                ),
                "receiving_order_id": str(_row_value(row, "order_id", "") or "").strip(),
            },
        )
        entry["available_qty"] += available_qty
        entry["rows"].append((row, available_qty))
    return list(grouped.values())


def _choose_minimal_subset_by_qty(candidates: list[dict], required_qty: int) -> list[dict]:
    if required_qty <= 0 or not candidates:
        return []
    sorted_candidates = sorted(
        candidates,
        key=lambda item: (-int(item.get("available_qty") or 0), str(item.get("pallet_code") or "")),
    )
    if len(sorted_candidates) <= 18:
        best_indexes: tuple[int, ...] | None = None
        best_key: tuple[int, int, tuple[str, ...]] | None = None
        for size in range(1, len(sorted_candidates) + 1):
            for indexes in combinations(range(len(sorted_candidates)), size):
                total_qty = sum(int(sorted_candidates[idx].get("available_qty") or 0) for idx in indexes)
                if total_qty < required_qty:
                    continue
                pallet_codes = tuple(
                    str(sorted_candidates[idx].get("pallet_code") or "").strip()
                    for idx in indexes
                )
                overshoot = total_qty - required_qty
                key = (len(indexes), overshoot, pallet_codes)
                if best_key is None or key < best_key:
                    best_key = key
                    best_indexes = indexes
            if best_indexes is not None:
                break
        if best_indexes is not None:
            return [sorted_candidates[idx] for idx in best_indexes]

    chosen: list[dict] = []
    covered = 0
    for candidate in sorted_candidates:
        chosen.append(candidate)
        covered += int(candidate.get("available_qty") or 0)
        if covered >= required_qty:
            break
    return chosen


def _allocate_qty_from_candidates(
    candidates: list[dict],
    required_qty: int,
    *,
    remaining_by_row_id: dict[int, int],
) -> tuple[list[dict], int]:
    remaining = int(required_qty or 0)
    allocations: list[dict] = []
    ordered_candidates = sorted(
        candidates,
        key=lambda item: (-int(item.get("available_qty") or 0), str(item.get("pallet_code") or "")),
    )
    for candidate in ordered_candidates:
        if remaining <= 0:
            break
        allocated_qty = 0
        for row, _row_available in candidate.get("rows") or []:
            row_id = _as_int(_row_value(row, "id", 0))
            row_available = int(remaining_by_row_id.get(row_id, 0) or 0)
            if row_available <= 0:
                continue
            take_qty = min(remaining, row_available)
            if take_qty <= 0:
                continue
            remaining_by_row_id[row_id] = max(row_available - take_qty, 0)
            allocated_qty += take_qty
            remaining -= take_qty
            if remaining <= 0:
                break
        if allocated_qty > 0:
            allocations.append(
                {
                    **candidate,
                    "allocated_qty": allocated_qty,
                }
            )
    return allocations, remaining


def _plan_item_across_minimal_pallets(
    rows,
    *,
    qty_required: int,
    remaining_by_row_id: dict[int, int],
    blocked_pallets: set[str] | None = None,
    requested_goods_type: str = "",
    preferred_pallets: set[str] | None = None,
) -> tuple[list[dict], int]:
    if qty_required <= 0:
        return [], 0
    preferred_pallets = preferred_pallets or set()
    candidates = _candidate_pallets_for_rows(
        rows,
        remaining_by_row_id=remaining_by_row_id,
        blocked_pallets=blocked_pallets,
        requested_goods_type=requested_goods_type,
    )
    if not candidates:
        return [], qty_required

    preferred_candidates = [candidate for candidate in candidates if candidate["pallet_code"] in preferred_pallets]
    regular_candidates = [candidate for candidate in candidates if candidate["pallet_code"] not in preferred_pallets]

    selected_candidates: list[dict] = []
    remaining = qty_required
    if preferred_candidates:
        preferred_allocations, remaining = _allocate_qty_from_candidates(
            preferred_candidates,
            remaining,
            remaining_by_row_id=remaining_by_row_id,
        )
        selected_candidates.extend(preferred_allocations)
    if remaining > 0 and regular_candidates:
        chosen_subset = _choose_minimal_subset_by_qty(regular_candidates, remaining)
        subset_allocations, remaining = _allocate_qty_from_candidates(
            chosen_subset,
            remaining,
            remaining_by_row_id=remaining_by_row_id,
        )
        selected_candidates.extend(subset_allocations)
    return selected_candidates, remaining


@transaction.atomic
def create_stock_move_task(
    *,
    user,
    agency,
    description: str,
    payload: dict,
    requested_by_name: str = "",
    requested_by_role: str = "",
    move_request: MoveRequest | None = None,
) -> str:
    move_payload = deepcopy(payload or {})
    from_location = _normalize_location(move_payload.get("from_location"))
    to_location = _normalize_location(move_payload.get("to_location"))
    move_payload["from_location"] = from_location
    move_payload["to_location"] = to_location
    move_payload.setdefault("status", MoveTask.STATUS_CREATED)
    move_payload.setdefault("status_label", "Ожидает перевозки")
    move_payload.setdefault("pick_mode", "full")
    move_payload.setdefault("move_mode", MoveTask.MODE_PALLET_FULL)
    move_payload.setdefault("from_label", _location_label(from_location))
    move_payload.setdefault("to_label", _location_label(to_location))
    if requested_by_name:
        move_payload["requested_by_name"] = requested_by_name
    if requested_by_role:
        move_payload["requested_by_role"] = requested_by_role

    processing_order_id = str(move_payload.get("processing_order_id") or "").strip()
    receiving_order_id = str(move_payload.get("receiving_order_id") or "").strip()
    if processing_order_id:
        context_type = MoveRequest.CONTEXT_PROCESSING
        context_id = processing_order_id
    elif receiving_order_id:
        context_type = MoveRequest.CONTEXT_RECEIVING
        context_id = receiving_order_id
    else:
        context_type = MoveRequest.CONTEXT_MANUAL
        context_id = ""

    authenticated_user = user if getattr(user, "is_authenticated", False) else None
    request_obj = move_request
    if request_obj is None:
        request_obj = MoveRequest.objects.create(
            context_type=context_type,
            context_id=context_id,
            agency=agency,
            requested_by=authenticated_user,
            requested_by_role=str(move_payload.get("requested_by_role") or requested_by_role or ""),
            requested_by_name=str(move_payload.get("requested_by_name") or requested_by_name or ""),
            destination_zone=to_location.get("zone") or "PR",
            destination_row=_as_int(to_location.get("row")) or None,
            destination_section=_as_int(to_location.get("section")) or None,
            destination_tier=_as_int(to_location.get("tier")) or None,
            destination_cell=_as_int(to_location.get("cell")) or None,
            status=MoveRequest.STATUS_PLANNED,
            comment=str(move_payload.get("instruction") or ""),
        )
        _build_request_items(request_obj, move_payload)

    move_id = _next_stock_move_number()
    qty_planned = max(_as_int(move_payload.get("requested_qty")), 0)
    move_task = MoveTask.objects.create(
        request=request_obj,
        pallet_code=str(move_payload.get("pallet_code") or "").strip() or "-",
        from_zone=str(from_location.get("zone") or ""),
        from_row=_as_int(from_location.get("row")) or None,
        from_section=_as_int(from_location.get("section")) or None,
        from_tier=_as_int(from_location.get("tier")) or None,
        from_cell=_as_int(from_location.get("cell")) or None,
        to_zone=str(to_location.get("zone") or ""),
        to_row=_as_int(to_location.get("row")) or None,
        to_section=_as_int(to_location.get("section")) or None,
        to_tier=_as_int(to_location.get("tier")) or None,
        to_cell=_as_int(to_location.get("cell")) or None,
        move_mode=str(move_payload.get("move_mode") or MoveTask.MODE_PALLET_FULL),
        qty_planned=qty_planned,
        payload=move_payload,
        status=MoveTask.STATUS_CREATED,
        legacy_order_id=move_id,
    )
    move_payload["move_request_id"] = request_obj.id
    move_payload["move_task_id"] = move_task.id
    move_task.payload = move_payload
    move_task.save(update_fields=["payload", "updated_at"])

    log_order_action(
        "create",
        order_id=move_id,
        order_type="stock_move",
        user=authenticated_user,
        agency=agency,
        description=description,
        payload=move_payload,
    )
    log_stock_move(
        "create",
        user=authenticated_user,
        agency=agency,
        description=description,
        snapshot={
            "move_id": move_id,
            "move_request_id": request_obj.id,
            "move_task_id": move_task.id,
            "pallet_code": move_payload.get("pallet_code"),
            "from_location": from_location,
            "to_location": to_location,
            "from_label": move_payload.get("from_label"),
            "to_label": move_payload.get("to_label"),
            "receiving_order_id": move_payload.get("receiving_order_id") or "",
            "processing_order_id": move_payload.get("processing_order_id") or "",
            "status": "created",
            "pick_mode": move_payload.get("pick_mode") or "full",
            "move_mode": move_payload.get("move_mode") or MoveTask.MODE_PALLET_FULL,
            "requested_qty": qty_planned if qty_planned > 0 else "",
            "requested_barcode_qty": move_payload.get("requested_barcode_qty") or {},
            "requested_boxes": move_payload.get("requested_boxes") or [],
            "requested_box": move_payload.get("requested_box") or "",
            "requested_rows": move_payload.get("requested_rows") or [],
            "instruction": move_payload.get("instruction") or "",
        },
    )
    _recompute_request_status(request_obj)
    return move_id


def _plan_move_request_to_tasks(
    *,
    move_request: MoveRequest,
    move_request_items: list[MoveRequestItem],
    request_items: list[dict],
    explicit_requested_rows: list[dict] | None,
    destination: dict,
    processing_order_id: str,
    requested_by_name: str,
    requested_by_role: str,
    user,
) -> dict:
    agency_id = move_request.agency_id
    base_rows = _warehouse_base_rows_for_planning(agency_id=agency_id)

    latest_moves = _latest_moves_by_pallet(processing_order_id=processing_order_id)
    blocked_pallets = {
        code
        for code, move in latest_moves.items()
        if (move.get("status") or "").strip().lower() in {"created", "in_progress"}
    }

    remaining_by_row_id: dict[int, int] = {
        _as_int(_row_value(row, "id", 0)): _as_int(_row_value(row, "qty", 0))
        for row in base_rows
    }
    plan_by_pallet: dict[str, dict] = {}
    planned_by_item_idx: dict[int, int] = {}
    shortage_qty = 0
    explicit_requested_rows = explicit_requested_rows or []
    if explicit_requested_rows:
        pallet_info_by_code: dict[str, dict] = {}
        for row in base_rows:
            pallet_code = str(_row_value(row, "pallet_code", "") or "").strip()
            if not pallet_code or pallet_code in pallet_info_by_code:
                continue
            pallet_info_by_code[pallet_code] = {
                "from_location": _build_location(
                    _row_value(row, "zone", ""),
                    _as_int(_row_value(row, "row", 0)),
                    _as_int(_row_value(row, "section", 0)),
                    _as_int(_row_value(row, "tier", 0)),
                    _as_int(_row_value(row, "cell", 0)),
                ),
                "receiving_order_id": str(_row_value(row, "order_id", "") or "").strip(),
            }
        blocked_requested_pallets = {
            code for code in {str(row.get("pallet_code") or "").strip() for row in explicit_requested_rows}
            if code and code in blocked_pallets
        }
        if blocked_requested_pallets:
            move_request.status = MoveRequest.STATUS_BLOCKED
            move_request.planning_error = (
                "Паллета уже занята активным заданием: "
                + ", ".join(sorted(blocked_requested_pallets))
            )
            move_request.save(update_fields=["status", "planning_error", "updated_at"])
            return {"created": 0, "move_ids": [], "shortage_qty": 0}
        for row in explicit_requested_rows:
            pallet_code = str(row.get("pallet_code") or "").strip()
            box_code = _normalize_box_code(row.get("box_code"))
            requested_qty = _as_int(row.get("qty"))
            if not pallet_code or not box_code or requested_qty <= 0:
                continue
            pallet_info = pallet_info_by_code.get(pallet_code) or {}
            entry = plan_by_pallet.setdefault(
                pallet_code,
                {
                    "qty": 0,
                    "barcodes": set(),
                    "barcode_qty": {},
                    "articles": set(),
                    "goods_types": set(),
                    "from_location": pallet_info.get("from_location") or _build_location("PR", 0, 0, 0, 0),
                    "receiving_order_id": pallet_info.get("receiving_order_id") or "",
                    "item_chunks": [],
                    "requested_rows": [],
                    "requested_boxes": [],
                },
            )
            entry["qty"] += requested_qty
            requested_article = str(row.get("requested_article") or "").strip()
            requested_goods_type = _normalize_goods_type(row.get("requested_goods_type"))
            requested_barcodes = [
                str(value).strip()
                for value in (row.get("requested_barcodes") or [])
                if str(value or "").strip()
            ]
            if requested_article:
                entry["articles"].add(requested_article)
            if requested_goods_type:
                entry["goods_types"].add(requested_goods_type)
            if requested_barcodes:
                entry["barcodes"].update(requested_barcodes)
            barcode_qty = _normalize_barcode_qty_map(row.get("barcode_qty"))
            for barcode, qty in barcode_qty.items():
                entry["barcode_qty"][barcode] = int(entry["barcode_qty"].get(barcode, 0)) + int(qty)
            if box_code not in entry["requested_boxes"]:
                entry["requested_boxes"].append(box_code)
            entry["requested_rows"].append(
                {
                    "box_code": box_code,
                    "qty": requested_qty,
                    "barcode_qty": barcode_qty,
                }
            )
            item_chunk = {
                "requested_article": requested_article,
                "requested_goods_type": requested_goods_type,
                "requested_qty": requested_qty,
                "requested_barcodes": requested_barcodes,
            }
            entry["item_chunks"].append(item_chunk)
            for idx, request_item in enumerate(request_items):
                item_article = str(request_item.get("requested_article") or "").strip()
                item_goods_type = _normalize_goods_type(request_item.get("requested_goods_type"))
                item_barcodes = {
                    str(value).strip()
                    for value in (request_item.get("requested_barcodes") or [])
                    if str(value or "").strip()
                }
                goods_type_match = not item_goods_type or not requested_goods_type or item_goods_type == requested_goods_type
                article_match = bool(item_article and requested_article and item_article == requested_article)
                barcode_match = bool(item_barcodes and requested_barcodes and item_barcodes.intersection(requested_barcodes))
                if goods_type_match and (article_match or barcode_match):
                    planned_by_item_idx[idx] = planned_by_item_idx.get(idx, 0) + requested_qty
                    break

    for idx, item in enumerate(request_items):
        if explicit_requested_rows:
            requested_total = _as_int(item.get("requested_qty"))
            planned_total = planned_by_item_idx.get(idx, 0)
            if requested_total > planned_total:
                shortage_qty += requested_total - planned_total
            continue
        qty_required = _as_int(item.get("requested_qty"))
        if qty_required <= 0:
            continue
        requested_article = str(item.get("requested_article") or "").strip()
        requested_goods_type = _normalize_goods_type(item.get("requested_goods_type"))
        requested_barcodes = [
            str(value).strip() for value in (item.get("requested_barcodes") or []) if str(value or "").strip()
        ]
        if not requested_article and not requested_barcodes:
            continue
        rows = []
        for row in base_rows:
            row_sku = str(_row_value(row, "sku", "") or "").strip()
            row_barcode = str(_row_value(row, "barcode", "") or "").strip()
            if requested_article and requested_barcodes:
                if row_sku != requested_article and row_barcode not in requested_barcodes:
                    continue
            elif requested_article and row_sku != requested_article:
                continue
            elif requested_barcodes and row_barcode not in requested_barcodes:
                continue
            rows.append(row)
        allocations, remaining = _plan_item_across_minimal_pallets(
            rows,
            qty_required=qty_required,
            remaining_by_row_id=remaining_by_row_id,
            blocked_pallets=blocked_pallets,
            requested_goods_type=requested_goods_type,
            preferred_pallets=set(plan_by_pallet.keys()),
        )
        for allocation in allocations:
            pallet_code = str(allocation.get("pallet_code") or "").strip()
            alloc_qty = _as_int(allocation.get("allocated_qty"))
            if not pallet_code or alloc_qty <= 0:
                continue
            entry = plan_by_pallet.setdefault(
                pallet_code,
                {
                    "qty": 0,
                    "barcodes": set(),
                    "articles": set(),
                    "goods_types": set(),
                    "from_location": allocation.get("from_location") or _build_location("PR", 0, 0, 0, 0),
                    "receiving_order_id": allocation.get("receiving_order_id") or "",
                    "item_chunks": [],
                },
            )
            entry["qty"] += alloc_qty
            if requested_article:
                entry["articles"].add(requested_article)
            if requested_goods_type:
                entry["goods_types"].add(requested_goods_type)
            if requested_barcodes:
                entry["barcodes"].update(requested_barcodes)
            entry["item_chunks"].append(
                {
                    "requested_article": requested_article,
                    "requested_goods_type": requested_goods_type,
                    "requested_qty": alloc_qty,
                    "requested_barcodes": requested_barcodes,
                }
            )
            planned_by_item_idx[idx] = planned_by_item_idx.get(idx, 0) + alloc_qty
        if remaining > 0:
            shortage_qty += remaining

    created = 0
    move_ids: list[str] = []
    for pallet_code, entry in plan_by_pallet.items():
        from_location = entry.get("from_location") or {"zone": "PR", "row": "", "section": "", "tier": "", "cell": ""}
        requested_qty = _as_int(entry.get("qty"))
        if requested_qty <= 0:
            continue
        articles = sorted(list(entry.get("articles") or []))
        goods_types = sorted(list(entry.get("goods_types") or []))
        barcodes = sorted(list(entry.get("barcodes") or []))
        requested_boxes = list(entry.get("requested_boxes") or [])
        payload = {
            "status": "created",
            "status_label": "Ожидает отбора по потребности",
            "pallet_code": pallet_code,
            "from_location": from_location,
            "to_location": destination,
            "from_label": _location_label(from_location),
            "to_label": _location_label(destination),
            "receiving_order_id": entry.get("receiving_order_id") or processing_order_id,
            "requested_by_name": requested_by_name,
            "requested_by_role": requested_by_role,
            "pick_mode": "partial",
            "move_mode": MOVE_MODE_BOX_PARTIAL,
            "requested_qty": requested_qty,
            "requested_sku": articles[0] if len(articles) == 1 else "",
            "requested_barcodes": barcodes,
            "requested_barcode_qty": dict(entry.get("barcode_qty") or {}),
            "requested_boxes": requested_boxes,
            "requested_box": requested_boxes[0] if len(requested_boxes) == 1 else "",
            "requested_rows": list(entry.get("requested_rows") or []),
            "requested_goods_type": goods_types[0] if len(goods_types) == 1 else "",
            "available_qty": requested_qty,
            "processing_order_id": processing_order_id,
            "request_items": entry.get("item_chunks") or [],
        }
        payload["instruction"] = _request_instruction(
            pallet_code,
            requested_qty,
            destination,
            entry.get("item_chunks") or [],
        )
        move_id = create_stock_move_task(
            user=user if getattr(user, "is_authenticated", False) else None,
            agency=move_request.agency,
            description=f"Задание по потребности #{move_request.id}: палета {pallet_code}",
            payload=payload,
            requested_by_name=requested_by_name,
            requested_by_role=requested_by_role,
            move_request=move_request,
        )
        if processing_order_id:
            from sklad.models import WarehouseStockSnapshot
            from sklad.services.warehouse_write_path import WarehouseWritePathService

            has_warehouse_snapshot = WarehouseStockSnapshot.objects.filter(
                agency=move_request.agency,
                container_code=pallet_code,
                is_archived=False,
            ).exists()
            if not has_warehouse_snapshot:
                WarehouseWritePathService.sync_legacy_storage_pallet(
                    agency=move_request.agency,
                    pallet_code=pallet_code,
                    source_document_type="legacy_stock",
                    source_document_id=processing_order_id,
                )
            try:
                warehouse_operation = WarehouseWritePathService.request_move_to_processing(
                    agency=move_request.agency,
                    order_id=processing_order_id,
                    container_codes=[pallet_code],
                    requested_by=user if getattr(user, "is_authenticated", False) else None,
                    requested_by_role=requested_by_role or "processing_head",
                    source_document_type="stock_move",
                    source_document_id=move_id,
                )
            except ValueError:
                warehouse_operation = None
            if warehouse_operation is not None:
                warehouse_task = warehouse_operation.tasks.order_by("id").first()
                if warehouse_task:
                    warehouse_task.payload = {
                        **dict(warehouse_task.payload or {}),
                        "legacy_move_id": move_id,
                        "pallet_code": pallet_code,
                    }
                    warehouse_task.save(update_fields=["payload", "updated_at"])
                move_task = MoveTask.objects.filter(legacy_order_id=move_id).first()
                if move_task:
                    move_task.payload = {
                        **dict(move_task.payload or {}),
                        "warehouse_operation_id": warehouse_operation.id,
                        "warehouse_operation_task_id": warehouse_task.id if warehouse_task else "",
                    }
                    move_task.save(update_fields=["payload", "updated_at"])
        move_ids.append(move_id)
        created += 1

    for idx, move_item in enumerate(move_request_items):
        planned_qty = planned_by_item_idx.get(idx, 0)
        if move_item.qty_planned != planned_qty:
            move_item.qty_planned = planned_qty
            move_item.save(update_fields=["qty_planned", "updated_at"])

    if created <= 0:
        move_request.status = MoveRequest.STATUS_BLOCKED
        move_request.planning_error = "Не найден доступный товар для формирования заданий."
        move_request.save(update_fields=["status", "planning_error", "updated_at"])
    elif shortage_qty > 0:
        move_request.status = MoveRequest.STATUS_PARTIAL
        move_request.planning_error = f"Сформировано частично: не удалось покрыть {shortage_qty} шт."
        move_request.save(update_fields=["status", "planning_error", "updated_at"])
    elif move_request.planning_error:
        move_request.planning_error = ""
        move_request.save(update_fields=["planning_error", "updated_at"])

    return {
        "created": created,
        "shortage_qty": shortage_qty,
        "move_ids": move_ids,
    }


@transaction.atomic
def create_batch_move_tasks(
    *,
    context_type: str,
    context_id: str,
    agency,
    user=None,
    requested_by_name: str = "",
    requested_by_role: str = "",
    destination: dict | None = None,
    comment: str = "",
    task_specs: list[dict] | None = None,
) -> tuple[MoveRequest | None, list[str]]:
    specs = [spec for spec in (task_specs or []) if isinstance(spec, dict) and isinstance(spec.get("payload"), dict)]
    if not specs:
        return None, []

    normalized_destination = _normalize_location(destination or specs[0]["payload"].get("to_location"))
    authenticated_user = user if getattr(user, "is_authenticated", False) else None
    move_request = MoveRequest.objects.create(
        context_type=context_type,
        context_id=str(context_id or "").strip(),
        agency=agency,
        requested_by=authenticated_user,
        requested_by_role=str(requested_by_role or ""),
        requested_by_name=str(requested_by_name or ""),
        destination_zone=normalized_destination.get("zone") or "PR",
        destination_row=_as_int(normalized_destination.get("row")) or None,
        destination_section=_as_int(normalized_destination.get("section")) or None,
        destination_tier=_as_int(normalized_destination.get("tier")) or None,
        destination_cell=_as_int(normalized_destination.get("cell")) or None,
        status=MoveRequest.STATUS_CREATED,
        comment=str(comment or "").strip(),
    )

    move_ids: list[str] = []
    for spec in specs:
        move_id = create_stock_move_task(
            user=authenticated_user,
            agency=agency,
            description=str(spec.get("description") or "").strip() or "Задание ричтрака",
            payload=dict(spec.get("payload") or {}),
            requested_by_name=requested_by_name,
            requested_by_role=requested_by_role,
            move_request=move_request,
        )
        move_ids.append(move_id)
    return move_request, move_ids


@transaction.atomic
def create_shipping_pick_request(
    *,
    order,
    user=None,
    requested_by_name: str = "",
    requested_by_role: str = "",
) -> tuple[MoveRequest, list[str], int]:
    destination = _build_location("OTG", 0, 0, 0, 0)
    authenticated_user = user if getattr(user, "is_authenticated", False) else None
    move_request = MoveRequest.objects.create(
        context_type=MoveRequest.CONTEXT_MANUAL,
        context_id=str(getattr(order, "pk", "") or ""),
        agency=order.agency,
        requested_by=authenticated_user,
        requested_by_role=str(requested_by_role or ""),
        requested_by_name=str(requested_by_name or ""),
        destination_zone="OTG",
        status=MoveRequest.STATUS_CREATED,
        comment=f"Отгрузка {order.number}",
    )

    items = list(order.items.order_by("id"))
    request_item_map: dict[int, MoveRequestItem] = {}
    for item in items:
        qty_requested = max(int(item.qty_reserved or item.qty_requested or 0), 0)
        request_item_map[item.id] = MoveRequestItem.objects.create(
            request=move_request,
            sku_code=item.sku_code,
            barcode=item.barcode,
            goods_type=item.goods_type,
            qty_requested=qty_requested,
            qty_planned=0,
        )

    base_rows = _warehouse_base_rows_for_planning(agency_id=order.agency_id)
    blocked_pallets = _active_pallet_codes_for_agency(order.agency_id)
    remaining_by_row_id: dict[int, int] = {
        _as_int(_row_value(row, "id", 0)): _as_int(_row_value(row, "qty", 0))
        for row in base_rows
    }
    pallet_row_ids: dict[str, list[int]] = defaultdict(list)
    for row in base_rows:
        pallet_code = str(_row_value(row, "pallet_code", "") or "").strip()
        if pallet_code:
            pallet_row_ids[pallet_code].append(_as_int(_row_value(row, "id", 0)))
    plan_by_pallet: dict[str, dict] = {}
    planned_by_item_id: dict[int, int] = {}
    shortage_qty = 0

    for item in items:
        qty_required = max(int(item.qty_reserved or item.qty_requested or 0), 0)
        if qty_required <= 0:
            continue
        size_value = str(item.size or "").strip()
        barcode_value = str(item.barcode or "").strip()
        rows = []
        for row in base_rows:
            row_sku = str(_row_value(row, "sku", "") or "").strip()
            if row_sku.lower() != str(item.sku_code or "").strip().lower():
                continue
            row_size = str(_row_value(row, "size", "") or "").strip()
            if size_value:
                if row_size.lower() != size_value.lower():
                    continue
            elif row_size:
                continue
            if barcode_value and str(_row_value(row, "barcode", "") or "").strip().lower() != barcode_value.lower():
                continue
            rows.append(row)
        requested_goods_type = _normalize_goods_type(item.goods_type)
        preferred_pallets = set(plan_by_pallet.keys())
        allocations, remaining = _plan_item_across_minimal_pallets(
            rows,
            qty_required=qty_required,
            remaining_by_row_id=remaining_by_row_id,
            blocked_pallets=blocked_pallets,
            requested_goods_type=requested_goods_type,
            preferred_pallets=preferred_pallets,
        )
        for allocation in allocations:
            pallet_code = str(allocation.get("pallet_code") or "").strip()
            alloc_qty = int(allocation.get("allocated_qty") or 0)
            if not pallet_code or alloc_qty <= 0:
                continue
            entry = plan_by_pallet.setdefault(
                pallet_code,
                {
                    "qty": 0,
                    "barcodes": set(),
                    "barcode_qty": defaultdict(int),
                    "articles": set(),
                    "goods_types": set(),
                    "from_location": allocation.get("from_location") or _build_location("PR", 0, 0, 0, 0),
                    "receiving_order_id": allocation.get("receiving_order_id") or "",
                    "request_items": [],
                },
            )
            entry["qty"] += alloc_qty
            if str(item.sku_code or "").strip():
                entry["articles"].add(str(item.sku_code or "").strip())
            if requested_goods_type:
                entry["goods_types"].add(requested_goods_type)
            if barcode_value:
                entry["barcodes"].add(barcode_value)
                entry["barcode_qty"][barcode_value] += alloc_qty
            entry["request_items"].append(
                {
                    "requested_article": str(item.sku_code or "").strip(),
                    "requested_goods_type": requested_goods_type,
                    "requested_qty": alloc_qty,
                    "requested_barcodes": [barcode_value] if barcode_value else [],
                    "shipping_item_id": item.id,
                }
            )
            planned_by_item_id[item.id] = planned_by_item_id.get(item.id, 0) + alloc_qty
            remaining -= alloc_qty
        if remaining > 0:
            shortage_qty += remaining

    move_ids: list[str] = []
    for pallet_code, entry in plan_by_pallet.items():
        requested_qty = int(entry.get("qty") or 0)
        if requested_qty <= 0:
            continue
        articles = sorted(entry.get("articles") or [])
        goods_types = sorted(entry.get("goods_types") or [])
        barcodes = sorted(entry.get("barcodes") or [])
        barcode_qty = {
            str(barcode): int(qty or 0)
            for barcode, qty in dict(entry.get("barcode_qty") or {}).items()
            if str(barcode or "").strip() and int(qty or 0) > 0
        }
        source_label = _location_label(entry.get("from_location"))
        destination_label = _location_label(destination)
        move_mode = (
            MoveTask.MODE_PALLET_FULL
            if _shipping_entry_covers_full_pallet(
                pallet_code,
                remaining_by_row_id=remaining_by_row_id,
                pallet_row_ids=pallet_row_ids,
            )
            else MoveTask.MODE_BOX_PARTIAL
        )
        payload = {
            "status": MoveTask.STATUS_CREATED,
            "status_label": "Ожидает перевозки" if move_mode == MoveTask.MODE_PALLET_FULL else "Ожидает отбора по потребности",
            "task_kind_label": _shipping_task_kind_label(move_mode),
            "shipping_order_id": order.number,
            "shipping_order_pk": order.pk,
            "pallet_code": pallet_code,
            "from_location": entry.get("from_location") or _build_location("PR", 0, 0, 0, 0),
            "to_location": destination,
            "from_label": _location_label(entry.get("from_location")),
            "to_label": destination_label,
            "receiving_order_id": entry.get("receiving_order_id") or "",
            "requested_by_name": requested_by_name,
            "requested_by_role": requested_by_role,
            "pick_mode": "full" if move_mode == MoveTask.MODE_PALLET_FULL else "partial",
            "move_mode": move_mode,
            "requested_qty": "" if move_mode == MoveTask.MODE_PALLET_FULL else requested_qty,
            "requested_sku": articles[0] if len(articles) == 1 else "",
            "requested_barcodes": barcodes,
            "requested_barcode_qty": {} if move_mode == MoveTask.MODE_PALLET_FULL else barcode_qty,
            "requested_boxes": [],
            "requested_box": "",
            "requested_rows": [],
            "requested_goods_type": goods_types[0] if len(goods_types) == 1 else "",
            "available_qty": "" if move_mode == MoveTask.MODE_PALLET_FULL else requested_qty,
            "request_items": entry.get("request_items") or [],
            "instruction": (
                _shipping_full_pallet_instruction(
                    pallet_code=pallet_code,
                    destination_label=destination_label,
                )
                if move_mode == MoveTask.MODE_PALLET_FULL
                else _shipping_pick_instruction(
                    pallet_code=pallet_code,
                    requested_qty=requested_qty,
                    barcode_qty=barcode_qty,
                    destination_label=destination_label,
                    source_label=source_label,
                )
            ),
        }
        move_id = create_stock_move_task(
            user=authenticated_user,
            agency=order.agency,
            description=f"Задание по отгрузке {order.number}: палета {pallet_code}",
            payload=payload,
            requested_by_name=requested_by_name,
            requested_by_role=requested_by_role,
            move_request=move_request,
        )
        move_ids.append(move_id)

    for item_id, request_item in request_item_map.items():
        planned_qty = int(planned_by_item_id.get(item_id, 0) or 0)
        if request_item.qty_planned != planned_qty:
            request_item.qty_planned = planned_qty
            request_item.save(update_fields=["qty_planned", "updated_at"])

    if not move_ids:
        move_request.status = MoveRequest.STATUS_BLOCKED
        move_request.planning_error = "Не найден доступный товар для формирования заданий ричтраку."
        move_request.save(update_fields=["status", "planning_error", "updated_at"])
    elif shortage_qty > 0:
        move_request.status = MoveRequest.STATUS_PARTIAL
        move_request.planning_error = f"Сформировано частично: не удалось покрыть {shortage_qty} шт."
        move_request.save(update_fields=["status", "planning_error", "updated_at"])
    elif move_request.planning_error:
        move_request.planning_error = ""
        move_request.save(update_fields=["planning_error", "updated_at"])

    return move_request, move_ids, shortage_qty


@transaction.atomic
def sync_task_status_by_legacy_order_id(
    legacy_order_id: str,
    *,
    status: str,
    assigned_to=None,
    assigned_to_name: str = "",
    qty_done: int | None = None,
    error: str = "",
) -> MoveTask | None:
    target_id = str(legacy_order_id or "").strip()
    if not target_id:
        return None
    task = (
        MoveTask.objects.select_related("request")
        .filter(legacy_order_id=target_id)
        .order_by("-updated_at")
        .first()
    )
    if not task:
        return None
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in {
        MoveTask.STATUS_CREATED,
        MoveTask.STATUS_IN_PROGRESS,
        MoveTask.STATUS_DONE,
        MoveTask.STATUS_CANCELED,
        MoveTask.STATUS_FAILED,
    }:
        normalized_status = task.status
    update_fields = ["status", "updated_at"]
    task.status = normalized_status
    if normalized_status == MoveTask.STATUS_IN_PROGRESS:
        task.started_at = timezone.localtime()
        if "started_at" not in update_fields:
            update_fields.append("started_at")
        if assigned_to is not None and getattr(assigned_to, "is_authenticated", False):
            task.assigned_to = assigned_to
            update_fields.append("assigned_to")
        if assigned_to_name:
            task.assigned_to_name = assigned_to_name
            update_fields.append("assigned_to_name")
    if normalized_status == MoveTask.STATUS_DONE:
        task.completed_at = timezone.localtime()
        if "completed_at" not in update_fields:
            update_fields.append("completed_at")
        done_value = qty_done if qty_done is not None else task.qty_planned
        task.qty_done = max(_as_int(done_value), 0)
        update_fields.append("qty_done")
    if normalized_status == MoveTask.STATUS_CANCELED:
        task.canceled_at = timezone.localtime()
        if "canceled_at" not in update_fields:
            update_fields.append("canceled_at")
    if error:
        task.error = str(error)
        update_fields.append("error")

    payload = dict(task.payload or {})
    payload["status"] = normalized_status
    if normalized_status == MoveTask.STATUS_IN_PROGRESS:
        payload["status_label"] = "В работе"
        if assigned_to is not None and getattr(assigned_to, "id", None):
            payload["assigned_to_id"] = int(assigned_to.id)
        if assigned_to_name:
            payload["assigned_to_name"] = assigned_to_name
    elif normalized_status == MoveTask.STATUS_DONE:
        if qty_done is not None:
            payload["picked_qty"] = max(_as_int(qty_done), 0)
    elif normalized_status == MoveTask.STATUS_CANCELED:
        payload["status_label"] = payload.get("status_label") or "Отменено"
    elif normalized_status == MoveTask.STATUS_FAILED and error:
        payload["status_label"] = payload.get("status_label") or "Ошибка"
        payload["error"] = str(error)
    task.payload = payload
    update_fields.append("payload")

    task.save(update_fields=sorted(set(update_fields)))
    _recompute_request_status(task.request)
    return task
