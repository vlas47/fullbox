from __future__ import annotations

import json
import re
from itertools import combinations

from sku.models import SKUBarcode
from sklad.services import OperationalStockService
from sklad.services.stock_availability import StockAvailabilityService


MOVE_MODE_PALLET_FULL = "pallet_full"
MOVE_MODE_BOX_FULL = "box_full"
MOVE_MODE_BOX_PARTIAL = "box_partial"

_SKU_BARCODE_BY_SIZE_CACHE: dict[tuple[str, str], str] = {}


def _parse_int_value(raw) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return 0


def _normalize_goods_type(raw: str | None) -> str:
    return StockAvailabilityService.normalize_goods_type(raw)


def _item_matches(
    item,
    barcode_values: set[str],
    sku_values: set[str],
    goods_type_values: set[str] | None = None,
) -> bool:
    if not isinstance(item, dict):
        return False
    goods_type_values = goods_type_values or set()
    if goods_type_values:
        item_goods_type = _normalize_goods_type(
            item.get("goods_type") or item.get("goods_type_label")
        )
        if item_goods_type and item_goods_type not in goods_type_values:
            return False
    barcode = _resolved_item_barcode(item)
    if barcode and barcode in barcode_values:
        return True
    sku = str(item.get("sku") or item.get("sku_code") or "").strip()
    return bool(sku and sku in sku_values)


def _item_qty(item) -> int:
    if not isinstance(item, dict):
        return 0
    for key in ("qty", "actual_qty", "count"):
        try:
            value = int(item.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value:
            return value
    return 0


def _set_item_qty(item: dict, value: int) -> None:
    qty_value = max(0, int(value or 0))
    if "qty" in item:
        item["qty"] = qty_value
        return
    if "actual_qty" in item:
        item["actual_qty"] = qty_value
        return
    if "count" in item:
        item["count"] = qty_value
        return
    item["qty"] = qty_value


def _parse_json_list(raw: str | None) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    values = []
    for value in data:
        text_value = str(value or "").strip()
        if text_value:
            values.append(text_value)
    return values


def _normalize_barcode_qty_map(raw) -> dict[str, int]:
    source = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            source = json.loads(text)
        except json.JSONDecodeError:
            return {}
    if not isinstance(source, dict):
        return {}
    result: dict[str, int] = {}
    for raw_barcode, raw_qty in source.items():
        barcode = str(raw_barcode or "").strip()
        qty = _parse_int_value(raw_qty)
        if not barcode or barcode == "-" or qty <= 0:
            continue
        result[barcode] = result.get(barcode, 0) + qty
    return result


def _requested_barcode_qty(payload: dict) -> dict[str, int]:
    if not isinstance(payload, dict):
        return {}
    if isinstance(payload.get("requested_barcode_qty"), dict):
        return _normalize_barcode_qty_map(payload.get("requested_barcode_qty"))
    if payload.get("requested_barcode_qty_json") is not None:
        return _normalize_barcode_qty_map(payload.get("requested_barcode_qty_json"))
    return {}


def _normalize_box_code(value: str | None) -> str:
    return str(value or "").strip()


def _barcode_qty_total(source: dict[str, int]) -> int:
    return sum(_parse_int_value(value) for value in (source or {}).values())


def _normalize_requested_partial_rows(raw) -> list[dict]:
    source = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            source = json.loads(text)
        except json.JSONDecodeError:
            return []
    if not isinstance(source, list):
        return []
    merged: dict[str, dict] = {}
    order: list[str] = []
    for item in source:
        if not isinstance(item, dict):
            continue
        box_code = _normalize_box_code(
            item.get("box_code") or item.get("box") or item.get("requested_box")
        )
        row_barcode_qty = _normalize_barcode_qty_map(
            item.get("barcode_qty")
            or item.get("requested_barcode_qty")
            or item.get("requested_barcode_qty_json")
        )
        qty = _parse_int_value(item.get("qty") or item.get("pick_qty") or item.get("requested_qty"))
        if qty <= 0 and row_barcode_qty:
            qty = _barcode_qty_total(row_barcode_qty)
        if not box_code or qty <= 0:
            continue
        key = box_code.lower()
        if key not in merged:
            merged[key] = {"box_code": box_code, "qty": 0, "barcode_qty": {}}
            order.append(key)
        merged_row = merged[key]
        merged_row["qty"] = _parse_int_value(merged_row.get("qty")) + qty
        merged_barcode_qty = _normalize_barcode_qty_map(merged_row.get("barcode_qty"))
        for barcode, barcode_qty in row_barcode_qty.items():
            merged_barcode_qty[barcode] = merged_barcode_qty.get(barcode, 0) + _parse_int_value(barcode_qty)
        merged_row["barcode_qty"] = merged_barcode_qty
    result = []
    for key in order:
        row = merged.get(key) or {}
        box_code = _normalize_box_code(row.get("box_code"))
        qty = _parse_int_value(row.get("qty"))
        barcode_qty = _normalize_barcode_qty_map(row.get("barcode_qty"))
        if barcode_qty:
            barcode_total = _barcode_qty_total(barcode_qty)
            if barcode_total > qty:
                qty = barcode_total
        if not box_code or qty <= 0:
            continue
        result.append(
            {
                "box_code": box_code,
                "qty": qty,
                "barcode_qty": barcode_qty,
            }
        )
    return result


def _requested_partial_rows(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    if payload.get("requested_rows") is not None:
        return _normalize_requested_partial_rows(payload.get("requested_rows"))
    if payload.get("requested_rows_json") is not None:
        return _normalize_requested_partial_rows(payload.get("requested_rows_json"))
    return []


def _normalize_move_mode(raw_mode: str | None, raw_pick_mode: str | None = None) -> str:
    token = str(raw_mode or "").strip().lower()
    if token in {MOVE_MODE_PALLET_FULL, "full", "pallet", "pallet_full"}:
        return MOVE_MODE_PALLET_FULL
    if token in {MOVE_MODE_BOX_FULL, "boxes", "box", "box_full"}:
        return MOVE_MODE_BOX_FULL
    if token in {MOVE_MODE_BOX_PARTIAL, "partial", "box_partial"}:
        return MOVE_MODE_BOX_PARTIAL
    pick_mode = str(raw_pick_mode or "").strip().lower()
    if pick_mode == "partial":
        return MOVE_MODE_BOX_PARTIAL
    if pick_mode in {"box_full", "boxes"}:
        return MOVE_MODE_BOX_FULL
    return MOVE_MODE_PALLET_FULL


def _parse_box_codes(raw: str | None) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    parsed_list = _parse_json_list(text)
    if parsed_list:
        source = parsed_list
    else:
        source = re.split(r"[,\n;\t]+", text)
    seen = set()
    result = []
    for item in source:
        code = _normalize_box_code(item)
        if not code:
            continue
        key = code.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(code)
    return result


def _payload_box_codes(payload: dict) -> list[str]:
    if not isinstance(payload, dict):
        return []
    raw = payload.get("requested_boxes")
    if isinstance(raw, list):
        source = raw
    else:
        source = _parse_box_codes(raw)
    seen = set()
    result = []
    for item in source:
        code = _normalize_box_code(item)
        if not code:
            continue
        key = code.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(code)
    return result


def _single_requested_box(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    explicit = _normalize_box_code(payload.get("requested_box"))
    if explicit:
        return explicit
    codes = _payload_box_codes(payload)
    return codes[0] if len(codes) == 1 else ""


def _sku_barcode_for_code_and_size(sku_code: str, size: str) -> str:
    sku_key = str(sku_code or "").strip()
    size_key = str(size or "").strip()
    if not sku_key:
        return ""
    cache_key = (sku_key, size_key)
    cached = _SKU_BARCODE_BY_SIZE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    qs = SKUBarcode.objects.select_related("sku").filter(sku__sku_code=sku_key)
    value = ""
    if size_key:
        value = (
            qs.filter(size=size_key)
            .order_by("-is_primary", "value")
            .values_list("value", flat=True)
            .first()
            or ""
        )
    if not value:
        value = (
            qs.filter(is_primary=True).values_list("value", flat=True).first()
            or qs.values_list("value", flat=True).first()
            or ""
        )
    _SKU_BARCODE_BY_SIZE_CACHE[cache_key] = value
    return value


def _resolved_item_barcode(item) -> str:
    if not isinstance(item, dict):
        return ""
    barcode = str(item.get("barcode") or "").strip()
    if barcode:
        return barcode
    sku = str(item.get("sku") or item.get("sku_code") or "").strip()
    size = str(item.get("size") or "").strip()
    return _sku_barcode_for_code_and_size(sku, size)


def _pallet_match_qty(
    payload: dict,
    pallet_code: str,
    barcode_values: set[str],
    sku_values: set[str],
    goods_type_values: set[str] | None = None,
) -> int:
    goods_type_values = goods_type_values or set()
    payload_goods_type = _normalize_goods_type(
        payload.get("goods_type") or payload.get("goods_type_label")
    )
    if goods_type_values and payload_goods_type and payload_goods_type not in goods_type_values:
        return 0
    pallets = payload.get("act_pallets") or []
    boxes = payload.get("act_boxes") or []
    box_items: dict[str, list] = {}
    for box in boxes:
        if not isinstance(box, dict):
            continue
        code = str(box.get("code") or "").strip()
        if code:
            box_items[code] = box.get("items") or []
    for pallet in pallets:
        if not isinstance(pallet, dict):
            continue
        code = str(pallet.get("code") or "").strip()
        if code != pallet_code:
            continue
        total = 0
        for item in pallet.get("items") or []:
            if _item_matches(item, barcode_values, sku_values, goods_type_values):
                total += _item_qty(item)
        for box_code in pallet.get("boxes") or []:
            for item in box_items.get(str(box_code).strip()) or []:
                if _item_matches(item, barcode_values, sku_values, goods_type_values):
                    total += _item_qty(item)
        return total
    return 0


def _find_pallet_in_placement(placement_payload: dict, pallet_code: str):
    pallets = placement_payload.get("act_pallets") or []
    for idx, value in enumerate(pallets):
        if not isinstance(value, dict):
            continue
        if str(value.get("code") or "").strip() == pallet_code:
            return pallets, idx, value
    return pallets, -1, None


def _find_box_in_placement(placement_payload: dict, box_code: str):
    target = _normalize_box_code(box_code).lower()
    if not target:
        return placement_payload.get("act_boxes") or [], -1, None
    boxes = placement_payload.get("act_boxes") or []
    for idx, value in enumerate(boxes):
        if not isinstance(value, dict):
            continue
        candidate = _normalize_box_code(value.get("code"))
        if candidate and candidate.lower() == target:
            return boxes, idx, value
    return boxes, -1, None


def _box_match_breakdown(
    placement_payload: dict,
    pallet_code: str,
    box_code: str,
    barcode_values: set[str],
    sku_values: set[str],
    goods_type_values: set[str] | None = None,
) -> tuple[int, dict[str, int]]:
    goods_type_values = goods_type_values or set()
    payload_goods_type = _normalize_goods_type(
        placement_payload.get("goods_type") or placement_payload.get("goods_type_label")
    )
    if goods_type_values and payload_goods_type and payload_goods_type not in goods_type_values:
        return 0, {}
    pallets, _, pallet = _find_pallet_in_placement(placement_payload, pallet_code)
    if not pallet:
        return 0, {}
    requested_code = _normalize_box_code(box_code)
    requested_code_key = requested_code.lower()
    pallet_boxes = {
        _normalize_box_code(code).lower()
        for code in (pallet.get("boxes") or [])
        if _normalize_box_code(code)
    }
    if requested_code_key not in pallet_boxes:
        return 0, {}
    boxes, _, box = _find_box_in_placement(placement_payload, requested_code)
    if not boxes or not box:
        return 0, {}
    total = 0
    barcode_qty: dict[str, int] = {}
    for item in box.get("items") or []:
        if _item_matches(item, barcode_values, sku_values, goods_type_values):
            qty = _item_qty(item)
            if qty <= 0:
                continue
            total += qty
            barcode = _resolved_item_barcode(item)
            if barcode and (not barcode_values or barcode in barcode_values):
                barcode_qty[barcode] = barcode_qty.get(barcode, 0) + qty
    return total, barcode_qty


def _box_match_qty(
    placement_payload: dict,
    pallet_code: str,
    box_code: str,
    barcode_values: set[str],
    sku_values: set[str],
    goods_type_values: set[str] | None = None,
) -> int:
    total, _ = _box_match_breakdown(
        placement_payload,
        pallet_code,
        box_code,
        barcode_values,
        sku_values,
        goods_type_values,
    )
    return total


def _matching_boxes_for_pallet(
    placement_payload: dict,
    pallet_code: str,
    barcode_values: set[str],
    sku_values: set[str],
    goods_type_values: set[str] | None = None,
) -> list[dict]:
    pallets, _, pallet = _find_pallet_in_placement(placement_payload, pallet_code)
    if not pallets or not pallet:
        return []
    result = []
    for raw_code in pallet.get("boxes") or []:
        box_code = _normalize_box_code(raw_code)
        if not box_code:
            continue
        qty, barcode_qty = _box_match_breakdown(
            placement_payload,
            pallet_code,
            box_code,
            barcode_values,
            sku_values,
            goods_type_values,
        )
        if qty <= 0:
            continue
        result.append({"code": box_code, "qty": int(qty), "barcode_qty": barcode_qty})
    return result


def _select_otg_box_codes_by_barcode_qty(
    matching: list[dict],
    requested_barcode_qty: dict[str, int],
) -> list[str]:
    requested = _normalize_barcode_qty_map(requested_barcode_qty)
    if not requested:
        return []

    candidates: list[dict] = []
    for row in matching:
        code = str(row.get("code") or "").strip()
        if not code:
            continue
        barcode_qty = {
            barcode: qty
            for barcode, qty in _normalize_barcode_qty_map(row.get("barcode_qty")).items()
            if barcode in requested and qty > 0
        }
        if not barcode_qty:
            continue
        candidates.append(
            {
                "code": code,
                "qty": _parse_int_value(row.get("qty")),
                "barcode_qty": barcode_qty,
            }
        )
    if not candidates:
        return []

    def _covers_request(selected_rows: list[dict]) -> tuple[bool, int, int]:
        covered = {barcode: 0 for barcode in requested}
        total_qty = 0
        for row in selected_rows:
            total_qty += _parse_int_value(row.get("qty"))
            for barcode, qty in dict(row.get("barcode_qty") or {}).items():
                if barcode in covered:
                    covered[barcode] += _parse_int_value(qty)
        if any(covered.get(barcode, 0) < req_qty for barcode, req_qty in requested.items()):
            return False, 0, total_qty
        overshoot = sum(max(covered.get(barcode, 0) - req_qty, 0) for barcode, req_qty in requested.items())
        return True, overshoot, total_qty

    if len(candidates) <= 18:
        best_codes: list[str] = []
        best_key: tuple[int, int, int, tuple[str, ...]] | None = None
        for size in range(1, len(candidates) + 1):
            for indexes in combinations(range(len(candidates)), size):
                selected_rows = [candidates[idx] for idx in indexes]
                ok, overshoot, total_qty = _covers_request(selected_rows)
                if not ok:
                    continue
                codes = [str(row.get("code") or "").strip() for row in selected_rows if str(row.get("code") or "").strip()]
                key = (overshoot, len(codes), total_qty, tuple(codes))
                if best_key is None or key < best_key:
                    best_key = key
                    best_codes = codes
        if best_codes:
            return best_codes

    remaining = dict(requested)
    selected_codes: list[str] = []
    unused = list(candidates)
    while any(qty > 0 for qty in remaining.values()):
        best_row = None
        best_key = None
        for row in unused:
            barcode_qty = dict(row.get("barcode_qty") or {})
            covered = sum(min(remaining.get(barcode, 0), qty) for barcode, qty in barcode_qty.items())
            if covered <= 0:
                continue
            overshoot = sum(max(qty - remaining.get(barcode, 0), 0) for barcode, qty in barcode_qty.items())
            total_qty = _parse_int_value(row.get("qty"))
            key = (-covered, overshoot, total_qty, str(row.get("code") or ""))
            if best_key is None or key < best_key:
                best_key = key
                best_row = row
        if best_row is None:
            return []
        selected_codes.append(str(best_row.get("code") or "").strip())
        for barcode, qty in dict(best_row.get("barcode_qty") or {}).items():
            if barcode not in remaining:
                continue
            remaining[barcode] = max(remaining.get(barcode, 0) - _parse_int_value(qty), 0)
        unused = [row for row in unused if row is not best_row]
    return selected_codes


def _all_boxes_for_pallet(placement_payload: dict, pallet_code: str) -> list[dict]:
    pallets, _, pallet = _find_pallet_in_placement(placement_payload, pallet_code)
    if not pallets or not pallet:
        return []
    result: list[dict] = []
    for raw_code in pallet.get("boxes") or []:
        box_code = _normalize_box_code(raw_code)
        if not box_code:
            continue
        _boxes, _idx, box = _find_box_in_placement(placement_payload, box_code)
        if not box:
            continue
        qty = 0
        barcode_qty: dict[str, int] = {}
        for item in box.get("items") or []:
            item_qty = _item_qty(item)
            if item_qty <= 0:
                continue
            qty += item_qty
            barcode = _resolved_item_barcode(item)
            if barcode:
                barcode_qty[barcode] = barcode_qty.get(barcode, 0) + item_qty
        result.append({"code": box_code, "qty": int(qty), "barcode_qty": barcode_qty})
    return result


def _pallet_total_qty(placement_payload: dict, pallet_code: str) -> int:
    pallets, _, pallet = _find_pallet_in_placement(placement_payload, pallet_code)
    if not pallets or not pallet:
        return 0
    total = 0
    for item in pallet.get("items") or []:
        total += _item_qty(item)
    for box in _all_boxes_for_pallet(placement_payload, pallet_code):
        total += _parse_int_value(box.get("qty"))
    return int(max(total, 0))


def _partial_request_covers_full_pallet(
    placement_payload: dict,
    pallet_code: str,
    *,
    pick_qty: int,
    barcode_values: set[str],
    sku_values: set[str],
    goods_type_values: set[str] | None = None,
    requested_rows: list[dict] | None = None,
    requested_boxes: list[str] | None = None,
    requested_box: str | None = None,
    requested_barcode_qty: dict[str, int] | None = None,
) -> bool:
    goods_type_values = goods_type_values or set()
    requested_rows = requested_rows or []
    requested_boxes = requested_boxes or []
    requested_barcode_qty = _normalize_barcode_qty_map(requested_barcode_qty)

    pallet_total_qty = _pallet_total_qty(placement_payload, pallet_code)
    if pallet_total_qty <= 0:
        return False
    matching_total_qty = _pallet_match_qty(
        placement_payload,
        pallet_code,
        barcode_values,
        sku_values,
        goods_type_values,
    )
    if matching_total_qty != pallet_total_qty:
        return False

    planned_total_qty = _parse_int_value(pick_qty)
    if requested_rows:
        planned_total_qty = sum(_parse_int_value(row.get("qty")) for row in requested_rows)
    elif requested_barcode_qty:
        planned_total_qty = _barcode_qty_total(requested_barcode_qty)
    if planned_total_qty != pallet_total_qty:
        return False

    all_box_codes = {
        str(box.get("code") or "").strip().lower()
        for box in _all_boxes_for_pallet(placement_payload, pallet_code)
        if str(box.get("code") or "").strip()
    }
    if requested_rows:
        selected_box_codes = {
            _normalize_box_code(row.get("box_code")).lower()
            for row in requested_rows
            if _normalize_box_code(row.get("box_code"))
        }
        return selected_box_codes == all_box_codes
    if requested_boxes:
        selected_box_codes = {
            _normalize_box_code(code).lower()
            for code in requested_boxes
            if _normalize_box_code(code)
        }
        return selected_box_codes == all_box_codes
    if requested_box:
        requested_box_code = _normalize_box_code(requested_box).lower()
        return bool(requested_box_code) and {requested_box_code} == all_box_codes
    return True


def _planned_box_codes_for_move(payload: dict, placement_payload: dict, pallet_code: str) -> list[str]:
    move_mode = _normalize_move_mode(payload.get("move_mode"), payload.get("pick_mode"))
    if move_mode == MOVE_MODE_BOX_PARTIAL:
        codes, _error = _resolve_box_partial_codes(placement_payload, pallet_code, payload)
        return codes
    requested_rows = _requested_partial_rows(payload)
    if requested_rows:
        seen = set()
        rows: list[str] = []
        for row in requested_rows:
            code = _normalize_box_code(row.get("box_code"))
            key = code.lower()
            if not code or key in seen:
                continue
            seen.add(key)
            rows.append(code)
        if rows:
            return rows
    requested_box = _single_requested_box(payload)
    if requested_box:
        return [requested_box]
    return _payload_box_codes(payload)


def _deduct_from_items(
    items: list,
    needed_qty: int,
    barcode_values: set[str],
    sku_values: set[str],
    goods_type_values: set[str] | None = None,
) -> int:
    if needed_qty <= 0:
        return 0
    if not isinstance(items, list):
        return needed_qty
    idx = 0
    while idx < len(items) and needed_qty > 0:
        item = items[idx]
        if not isinstance(item, dict) or not _item_matches(
            item,
            barcode_values,
            sku_values,
            goods_type_values,
        ):
            idx += 1
            continue
        qty = _item_qty(item)
        if qty <= 0:
            items.pop(idx)
            continue
        take = min(qty, needed_qty)
        left = qty - take
        needed_qty -= take
        if left <= 0:
            items.pop(idx)
            continue
        _set_item_qty(item, left)
        idx += 1
    return needed_qty


def _consume_pallet_qty(
    placement_payload: dict,
    pallet_code: str,
    qty_to_pick: int,
    barcode_values: set[str],
    sku_values: set[str],
    goods_type_values: set[str] | None = None,
) -> tuple[bool, str, int]:
    pallets = placement_payload.get("act_pallets") or []
    boxes = placement_payload.get("act_boxes") or []
    box_index = {}
    for idx, box in enumerate(boxes):
        if not isinstance(box, dict):
            continue
        code = str(box.get("code") or "").strip()
        if code:
            box_index[code] = idx
    pallet_idx = -1
    pallet = None
    for idx, value in enumerate(pallets):
        if not isinstance(value, dict):
            continue
        if str(value.get("code") or "").strip() == pallet_code:
            pallet_idx = idx
            pallet = value
            break
    if pallet_idx < 0 or pallet is None:
        return False, "Паллета не найдена в размещении.", 0
    available_qty = _pallet_match_qty(
        placement_payload,
        pallet_code,
        barcode_values,
        sku_values,
        goods_type_values,
    )
    if available_qty <= 0:
        return False, "На паллете нет товара для отбора.", 0
    if qty_to_pick > available_qty:
        return False, f"Недостаточно товара на паллете: доступно {available_qty}.", 0

    remaining = qty_to_pick
    removed_boxes = set()
    for code in list(pallet.get("boxes") or []):
        box_pos = box_index.get(str(code).strip())
        if box_pos is None:
            continue
        box = boxes[box_pos]
        remaining = _deduct_from_items(
            box.get("items") or [],
            remaining,
            barcode_values,
            sku_values,
            goods_type_values,
        )
        box["items"] = box.get("items") or []
        if not box["items"]:
            removed_boxes.add(str(code).strip())
        if remaining <= 0:
            break

    if remaining > 0:
        remaining = _deduct_from_items(
            pallet.get("items") or [],
            remaining,
            barcode_values,
            sku_values,
            goods_type_values,
        )
        pallet["items"] = pallet.get("items") or []

    if remaining > 0:
        return False, "Не удалось списать нужное количество с паллеты.", 0

    if removed_boxes:
        pallet["boxes"] = [
            str(code).strip()
            for code in (pallet.get("boxes") or [])
            if str(code).strip() and str(code).strip() not in removed_boxes
        ]
        boxes = [
            box
            for box in boxes
            if str((box or {}).get("code") or "").strip() not in removed_boxes
        ]
        placement_payload["act_boxes"] = boxes

    if not (pallet.get("boxes") or []) and not (pallet.get("items") or []):
        pallets.pop(pallet_idx)
    else:
        pallets[pallet_idx] = pallet
    placement_payload["act_pallets"] = pallets
    return True, "", qty_to_pick


def _consume_box_qty(
    placement_payload: dict,
    pallet_code: str,
    box_code: str,
    qty_to_pick: int,
    barcode_values: set[str],
    sku_values: set[str],
    goods_type_values: set[str] | None = None,
) -> tuple[bool, str, int]:
    requested_box = _normalize_box_code(box_code)
    if not requested_box:
        return False, "Не указан короб для отбора.", 0
    pallets, pallet_idx, pallet = _find_pallet_in_placement(placement_payload, pallet_code)
    if pallet_idx < 0 or not pallet:
        return False, "Паллета не найдена в размещении.", 0
    pallet_boxes = [_normalize_box_code(code) for code in (pallet.get("boxes") or []) if _normalize_box_code(code)]
    requested_box_key = requested_box.lower()
    if requested_box_key not in {code.lower() for code in pallet_boxes}:
        return False, f"Короб {requested_box} не найден на паллете {pallet_code}.", 0
    available_qty = _box_match_qty(
        placement_payload,
        pallet_code,
        requested_box,
        barcode_values,
        sku_values,
        goods_type_values,
    )
    if available_qty <= 0:
        return False, f"В коробе {requested_box} нет нужного товара.", 0
    if qty_to_pick <= 0:
        return False, "Укажите количество к отбору.", 0
    if qty_to_pick > available_qty:
        return False, f"В коробе {requested_box} доступно {available_qty} шт.", 0

    boxes, box_idx, box = _find_box_in_placement(placement_payload, requested_box)
    if box_idx < 0 or not box:
        return False, f"Короб {requested_box} не найден в размещении.", 0

    remaining = _deduct_from_items(
        box.get("items") or [],
        qty_to_pick,
        barcode_values,
        sku_values,
        goods_type_values,
    )
    box["items"] = box.get("items") or []
    if remaining > 0:
        return False, "Не удалось списать нужное количество из короба.", 0

    if not box["items"]:
        boxes.pop(box_idx)
        pallet["boxes"] = [
            code for code in pallet_boxes if code.lower() != requested_box_key
        ]
    else:
        boxes[box_idx] = box
    placement_payload["act_boxes"] = boxes

    if not (pallet.get("boxes") or []) and not (pallet.get("items") or []):
        pallets.pop(pallet_idx)
    else:
        pallets[pallet_idx] = pallet
    placement_payload["act_pallets"] = pallets
    return True, "", qty_to_pick


def _otg_requested_box_codes(payload: dict) -> list[str]:
    requested_rows = _requested_partial_rows(payload)
    row_codes = []
    seen = set()
    for row in requested_rows:
        code = _normalize_box_code(row.get("box_code"))
        if not code:
            continue
        key = code.lower()
        if key in seen:
            continue
        seen.add(key)
        row_codes.append(code)
    if row_codes:
        return row_codes
    requested_box = _single_requested_box(payload)
    if requested_box:
        return [requested_box]
    return _payload_box_codes(payload)


def _otg_selector_sets(payload: dict) -> tuple[set[str], set[str], set[str], int]:
    requested_qty = _parse_int_value(payload.get("requested_qty"))
    requested_barcode_qty = _requested_barcode_qty(payload)
    if requested_qty <= 0 and requested_barcode_qty:
        requested_qty = _barcode_qty_total(requested_barcode_qty)

    requested_rows = _requested_partial_rows(payload)
    if requested_rows:
        requested_qty = sum(_parse_int_value(row.get("qty")) for row in requested_rows)

    requested_sku = str(payload.get("requested_sku") or "").strip()
    requested_barcodes_raw = payload.get("requested_barcodes")
    if isinstance(requested_barcodes_raw, list):
        requested_barcodes = requested_barcodes_raw
    else:
        requested_barcodes = _parse_json_list(requested_barcodes_raw)
    barcode_values = {
        str(value).strip()
        for value in requested_barcodes
        if str(value or "").strip()
    }
    if requested_barcode_qty:
        barcode_values.update(requested_barcode_qty.keys())

    sku_values = {requested_sku} if requested_sku else set()
    if barcode_values:
        sku_values.update(
            SKUBarcode.objects.filter(value__in=barcode_values)
            .values_list("sku__sku_code", flat=True)
        )
        sku_values.discard(None)
        sku_values = {str(value).strip() for value in sku_values if str(value or "").strip()}

    requested_goods_type = _normalize_goods_type(payload.get("requested_goods_type"))
    goods_type_values = {requested_goods_type} if requested_goods_type else set()
    return barcode_values, sku_values, goods_type_values, requested_qty


def _resolve_otg_box_codes(
    placement_payload: dict,
    pallet_code: str,
    payload: dict,
) -> tuple[list[str], str]:
    explicit_codes = _otg_requested_box_codes(payload)
    if explicit_codes:
        return explicit_codes, ""

    requested_barcode_qty = _requested_barcode_qty(payload)
    barcode_values, sku_values, goods_type_values, requested_qty = _otg_selector_sets(payload)
    if barcode_values or sku_values or goods_type_values:
        matching = _matching_boxes_for_pallet(
            placement_payload,
            pallet_code,
            barcode_values,
            sku_values,
            goods_type_values,
        )
        if not matching:
            return [], "На паллете нет подходящих коробов для доставки в OTG."
        if requested_barcode_qty:
            selected_codes = _select_otg_box_codes_by_barcode_qty(matching, requested_barcode_qty)
            if selected_codes:
                return selected_codes, ""
            return [], "Не удалось подобрать набор коробов под требуемое количество по ШК."
        if requested_qty <= 0:
            return [str(row.get("code") or "").strip() for row in matching if str(row.get("code") or "").strip()], ""
        selected: list[str] = []
        total = 0
        for row in matching:
            code = str(row.get("code") or "").strip()
            if not code:
                continue
            selected.append(code)
            total += _parse_int_value(row.get("qty"))
            if total >= requested_qty:
                break
        if total < requested_qty:
            return [], f"Недостаточно коробов для доставки в OTG: требуется {requested_qty} шт., найдено {total} шт."
        return selected, ""

    pallets, _, pallet = _find_pallet_in_placement(placement_payload, pallet_code)
    if not pallets or not pallet:
        return [], "Паллета не найдена в размещении."
    all_codes = [
        _normalize_box_code(code)
        for code in (pallet.get("boxes") or [])
        if _normalize_box_code(code)
    ]
    if not all_codes:
        return [], "На паллете нет коробов для доставки в OTG."
    return all_codes, ""


def _resolve_box_partial_codes(
    placement_payload: dict,
    pallet_code: str,
    payload: dict,
) -> tuple[list[str], str]:
    to_zone = str((payload.get("to_location") or {}).get("zone") or "").strip().upper()
    if to_zone == "OTG":
        return _resolve_otg_box_codes(placement_payload, pallet_code, payload)

    requested_rows = _requested_partial_rows(payload)
    if requested_rows:
        seen = set()
        selected_rows: list[str] = []
        for row in requested_rows:
            code = _normalize_box_code(row.get("box_code"))
            key = code.lower()
            if not code or key in seen:
                continue
            seen.add(key)
            selected_rows.append(code)
        if selected_rows:
            return selected_rows, ""

    requested_box = _single_requested_box(payload)
    if requested_box:
        return [requested_box], ""

    requested_barcode_qty = _requested_barcode_qty(payload)
    barcode_values, sku_values, goods_type_values, requested_qty = _otg_selector_sets(payload)
    if barcode_values or sku_values or goods_type_values:
        matching = _matching_boxes_for_pallet(
            placement_payload,
            pallet_code,
            barcode_values,
            sku_values,
            goods_type_values,
        )
        if not matching:
            return [], "На паллете нет подходящих коробов для отбора."
        if requested_barcode_qty:
            selected_codes = _select_otg_box_codes_by_barcode_qty(matching, requested_barcode_qty)
            if selected_codes:
                return selected_codes, ""
            return [], "Не удалось подобрать набор коробов под требуемое количество по ШК."
        if requested_qty <= 0:
            return [
                str(row.get("code") or "").strip()
                for row in matching
                if str(row.get("code") or "").strip()
            ], ""
        selected: list[str] = []
        total = 0
        for row in matching:
            code = str(row.get("code") or "").strip()
            if not code:
                continue
            selected.append(code)
            total += _parse_int_value(row.get("qty"))
            if total >= requested_qty:
                break
        if total < requested_qty:
            return [], f"Недостаточно коробов для отбора: требуется {requested_qty} шт., найдено {total} шт."
        return selected, ""

    return _payload_box_codes(payload), ""


def _remove_boxes_from_pallet(
    placement_payload: dict,
    pallet_code: str,
    box_codes: list[str],
) -> tuple[bool, str, int]:
    requested_codes = [_normalize_box_code(code) for code in box_codes if _normalize_box_code(code)]
    if not requested_codes:
        return False, "Не указаны короба для отбора.", 0
    pallets, pallet_idx, pallet = _find_pallet_in_placement(placement_payload, pallet_code)
    if pallet_idx < 0 or not pallet:
        return False, "Паллета не найдена в размещении.", 0
    pallet_boxes = [_normalize_box_code(code) for code in (pallet.get("boxes") or []) if _normalize_box_code(code)]
    pallet_box_keys = {code.lower() for code in pallet_boxes}
    missing = [code for code in requested_codes if code.lower() not in pallet_box_keys]
    if missing:
        return False, f"На паллете нет коробов: {', '.join(missing)}.", 0

    boxes = placement_payload.get("act_boxes") or []
    requested_set = {code.lower() for code in requested_codes}
    placement_payload["act_boxes"] = [
        box
        for box in boxes
        if _normalize_box_code((box or {}).get("code") or "").lower() not in requested_set
    ]
    pallet["boxes"] = [
        code for code in pallet_boxes if code.lower() not in requested_set
    ]
    if not (pallet.get("boxes") or []) and not (pallet.get("items") or []):
        pallets.pop(pallet_idx)
    else:
        pallets[pallet_idx] = pallet
    placement_payload["act_pallets"] = pallets
    return True, "", len(requested_codes)


def _move_boxes_to_otg(
    placement_payload: dict,
    pallet_code: str,
    box_codes: list[str],
    *,
    otg_location: dict,
    return_location: dict,
) -> tuple[bool, str, dict]:
    requested_codes = []
    seen = set()
    for raw in box_codes:
        code = _normalize_box_code(raw)
        if not code:
            continue
        key = code.lower()
        if key in seen:
            continue
        seen.add(key)
        requested_codes.append(code)
    if not requested_codes:
        return False, "Не указаны короба для доставки в OTG.", {}

    pallets, pallet_idx, pallet = _find_pallet_in_placement(placement_payload, pallet_code)
    if pallet_idx < 0 or not pallet:
        return False, "Паллета не найдена в размещении.", {}

    pallet_boxes = [_normalize_box_code(code) for code in (pallet.get("boxes") or []) if _normalize_box_code(code)]
    pallet_box_keys = {code.lower() for code in pallet_boxes}
    missing = [code for code in requested_codes if code.lower() not in pallet_box_keys]
    if missing:
        return False, f"На паллете нет коробов: {', '.join(missing)}.", {}

    boxes = placement_payload.get("act_boxes") or []
    for code in requested_codes:
        _, box_idx, box = _find_box_in_placement(placement_payload, code)
        if box_idx < 0 or not box:
            return False, f"Короб {code} не найден в размещении.", {}
        box["location"] = {
            "zone": otg_location.get("zone") or "OTG",
            "row": _parse_int_value(otg_location.get("row")) or "",
            "section": _parse_int_value(otg_location.get("section")) or "",
            "tier": _parse_int_value(otg_location.get("tier")) or "",
            "cell": _parse_int_value(otg_location.get("cell")) or "",
        }
        boxes[box_idx] = box
    placement_payload["act_boxes"] = boxes

    requested_set = {code.lower() for code in requested_codes}
    pallet["boxes"] = [code for code in pallet_boxes if code.lower() not in requested_set]
    has_remainder = bool((pallet.get("boxes") or []) or (pallet.get("items") or []))
    pallet_deleted = False
    if has_remainder:
        pallet["location"] = {
            "zone": return_location.get("zone") or "PR",
            "row": _parse_int_value(return_location.get("row")) or "",
            "section": _parse_int_value(return_location.get("section")) or "",
            "tier": _parse_int_value(return_location.get("tier")) or "",
            "cell": _parse_int_value(return_location.get("cell")) or "",
        }
        pallets[pallet_idx] = pallet
    else:
        pallets.pop(pallet_idx)
        pallet_deleted = True
    placement_payload["act_pallets"] = pallets
    return True, "", {
        "moved_boxes": requested_codes,
        "pallet_deleted": pallet_deleted,
        "pallet_returned": not pallet_deleted,
    }


def _all_stock_boxes_for_pallet(
    pallet_code: str,
    *,
    agency_id: int | None = None,
) -> list[dict]:
    boxes = OperationalStockService.get_pallet_boxes(pallet_code, agency_id=agency_id)
    if not boxes:
        return []
    return [
        {
            "code": str(box.get("code") or "").strip(),
            "qty": _parse_int_value(box.get("qty")),
            "barcode_qty": _normalize_barcode_qty_map(box.get("barcode_qty")),
            "items": list(box.get("items") or []),
            "marked_units": list(box.get("marked_units") or []),
        }
        for box in boxes
        if str(box.get("code") or "").strip()
    ]


def _matching_stock_boxes_for_pallet(
    pallet_code: str,
    barcode_values: set[str],
    sku_values: set[str],
    goods_type_values: set[str] | None = None,
    *,
    agency_id: int | None = None,
) -> list[dict]:
    result: list[dict] = []
    for box in _all_stock_boxes_for_pallet(pallet_code, agency_id=agency_id):
        total = 0
        barcode_qty: dict[str, int] = {}
        for item in box.get("items") or []:
            if not _item_matches(item, barcode_values, sku_values, goods_type_values):
                continue
            qty = _item_qty(item)
            if qty <= 0:
                continue
            total += qty
            barcode = _resolved_item_barcode(item)
            if barcode and (not barcode_values or barcode in barcode_values):
                barcode_qty[barcode] = barcode_qty.get(barcode, 0) + qty
        if total <= 0:
            continue
        result.append(
            {
                "code": str(box.get("code") or "").strip(),
                "qty": int(total),
                "barcode_qty": barcode_qty,
            }
        )
    return result


def _resolve_stock_otg_box_codes(
    payload: dict,
    pallet_code: str,
    *,
    agency_id: int | None = None,
) -> tuple[list[str], str]:
    explicit_codes = _otg_requested_box_codes(payload)
    if explicit_codes:
        return explicit_codes, ""

    requested_barcode_qty = _requested_barcode_qty(payload)
    barcode_values, sku_values, goods_type_values, requested_qty = _otg_selector_sets(payload)
    if barcode_values or sku_values or goods_type_values:
        matching = _matching_stock_boxes_for_pallet(
            pallet_code,
            barcode_values,
            sku_values,
            goods_type_values,
            agency_id=agency_id,
        )
        if not matching:
            return [], "На паллете нет подходящих коробов для доставки в OTG."
        if requested_barcode_qty:
            selected_codes = _select_otg_box_codes_by_barcode_qty(matching, requested_barcode_qty)
            if selected_codes:
                return selected_codes, ""
            return [], "Не удалось подобрать набор коробов под требуемое количество по ШК."
        if requested_qty <= 0:
            return [
                str(row.get("code") or "").strip()
                for row in matching
                if str(row.get("code") or "").strip()
            ], ""
        selected: list[str] = []
        total = 0
        for row in matching:
            code = str(row.get("code") or "").strip()
            if not code:
                continue
            selected.append(code)
            total += _parse_int_value(row.get("qty"))
            if total >= requested_qty:
                break
        if total < requested_qty:
            return [], f"Недостаточно коробов для доставки в OTG: требуется {requested_qty} шт., найдено {total} шт."
        return selected, ""

    all_codes = [
        str(box.get("code") or "").strip()
        for box in _all_stock_boxes_for_pallet(pallet_code, agency_id=agency_id)
        if str(box.get("code") or "").strip()
    ]
    if not all_codes:
        return [], "На паллете нет коробов для доставки в OTG."
    return all_codes, ""


def _resolve_stock_box_partial_codes(
    payload: dict,
    pallet_code: str,
    *,
    agency_id: int | None = None,
) -> tuple[list[str], str]:
    to_zone = str((payload.get("to_location") or {}).get("zone") or "").strip().upper()
    if to_zone == "OTG":
        return _resolve_stock_otg_box_codes(payload, pallet_code, agency_id=agency_id)

    requested_rows = _requested_partial_rows(payload)
    if requested_rows:
        seen = set()
        selected_rows: list[str] = []
        for row in requested_rows:
            code = _normalize_box_code(row.get("box_code"))
            key = code.lower()
            if not code or key in seen:
                continue
            seen.add(key)
            selected_rows.append(code)
        if selected_rows:
            return selected_rows, ""

    requested_box = _single_requested_box(payload)
    if requested_box:
        return [requested_box], ""

    requested_barcode_qty = _requested_barcode_qty(payload)
    barcode_values, sku_values, goods_type_values, requested_qty = _otg_selector_sets(payload)
    if barcode_values or sku_values or goods_type_values:
        matching = _matching_stock_boxes_for_pallet(
            pallet_code,
            barcode_values,
            sku_values,
            goods_type_values,
            agency_id=agency_id,
        )
        if not matching:
            return [], "На паллете нет подходящих коробов для отбора."
        if requested_barcode_qty:
            selected_codes = _select_otg_box_codes_by_barcode_qty(matching, requested_barcode_qty)
            if selected_codes:
                return selected_codes, ""
            return [], "Не удалось подобрать набор коробов под требуемое количество по ШК."
        if requested_qty <= 0:
            return [
                str(row.get("code") or "").strip()
                for row in matching
                if str(row.get("code") or "").strip()
            ], ""
        selected: list[str] = []
        total = 0
        for row in matching:
            code = str(row.get("code") or "").strip()
            if not code:
                continue
            selected.append(code)
            total += _parse_int_value(row.get("qty"))
            if total >= requested_qty:
                break
        if total < requested_qty:
            return [], f"Недостаточно коробов для отбора: требуется {requested_qty} шт., найдено {total} шт."
        return selected, ""

    return _payload_box_codes(payload), ""


def _planned_stock_box_codes_for_move(
    payload: dict,
    pallet_code: str,
    *,
    agency_id: int | None = None,
) -> list[str]:
    move_mode = _normalize_move_mode(payload.get("move_mode"), payload.get("pick_mode"))
    if move_mode == MOVE_MODE_BOX_PARTIAL:
        codes, _error = _resolve_stock_box_partial_codes(
            payload,
            pallet_code,
            agency_id=agency_id,
        )
        return codes
    requested_rows = _requested_partial_rows(payload)
    if requested_rows:
        seen = set()
        rows: list[str] = []
        for row in requested_rows:
            code = _normalize_box_code(row.get("box_code"))
            key = code.lower()
            if not code or key in seen:
                continue
            seen.add(key)
            rows.append(code)
        if rows:
            return rows
    requested_box = _single_requested_box(payload)
    if requested_box:
        return [requested_box]
    return _payload_box_codes(payload)


def _pallet_box_plan(
    payload: dict,
    pallet_code: str,
    agency_id: int | None = None,
    pallet_lookup: tuple[dict[tuple[int, str], tuple], dict[str, tuple]] | None = None,
) -> list[dict]:
    del pallet_lookup
    all_boxes = _all_stock_boxes_for_pallet(pallet_code, agency_id=agency_id)
    if not all_boxes:
        return []
    move_mode = _normalize_move_mode(payload.get("move_mode"), payload.get("pick_mode"))
    if move_mode == MOVE_MODE_PALLET_FULL:
        deliver_codes = {
            str(box.get("code") or "").strip().lower()
            for box in all_boxes
            if str(box.get("code") or "").strip()
        }
    else:
        deliver_codes = {
            str(code or "").strip().lower()
            for code in _planned_stock_box_codes_for_move(
                payload,
                pallet_code,
                agency_id=agency_id,
            )
            if str(code or "").strip()
        }
    planned: list[dict] = []
    for box in all_boxes:
        barcode_qty = dict(box.get("barcode_qty") or {})
        preview = "; ".join(f"{barcode} - {qty} шт." for barcode, qty in sorted(barcode_qty.items())[:4])
        if len(barcode_qty) > 4:
            preview += "; ..."
        action = "Доставить" if str(box.get("code") or "").strip().lower() in deliver_codes else "Вернуть"
        planned.append(
            {
                "box_code": box.get("code") or "-",
                "qty": int(box.get("qty") or 0),
                "barcode_preview": preview,
                "action": action,
                "sort_order": 0 if action == "Доставить" else 1,
            }
        )
    planned.sort(key=lambda row: (int(row.get("sort_order") or 0), str(row.get("box_code") or "")))
    return planned


def _box_execution_plan(
    payload: dict,
    pallet_code: str,
    agency_id: int | None = None,
    pallet_lookup: tuple[dict[tuple[int, str], tuple], dict[str, tuple]] | None = None,
    pallet_plan: list[dict] | None = None,
) -> list[dict]:
    pallet_plan = pallet_plan if pallet_plan is not None else _pallet_box_plan(
        payload,
        pallet_code,
        agency_id=agency_id,
        pallet_lookup=pallet_lookup,
    )
    if not pallet_plan:
        return []

    requested_by_box: dict[str, int] = {}
    for row in _requested_partial_rows(payload):
        box_code = _normalize_box_code(row.get("box_code"))
        qty = _parse_int_value(row.get("qty"))
        if not box_code or qty <= 0:
            continue
        requested_by_box[box_code.lower()] = int(requested_by_box.get(box_code.lower(), 0)) + qty

    requested_qty = _parse_int_value(payload.get("requested_qty"))
    requested_box = _single_requested_box(payload)
    if requested_box and requested_qty > 0 and requested_box.lower() not in requested_by_box:
        requested_by_box[requested_box.lower()] = requested_qty

    plan_rows: list[dict] = []
    for row in pallet_plan:
        box_code = str(row.get("box_code") or "").strip()
        box_qty = _parse_int_value(row.get("qty"))
        action = str(row.get("action") or "").strip()
        key = box_code.lower()
        to_obr_qty = min(_parse_int_value(requested_by_box.get(key, 0)), box_qty)
        if action == "Доставить" and to_obr_qty <= 0:
            to_obr_qty = box_qty
        remain_in_box_qty = max(box_qty - to_obr_qty, 0)
        return_back_qty = box_qty if action == "Вернуть" else remain_in_box_qty
        plan_rows.append(
            {
                "box_code": box_code or "-",
                "barcode_preview": row.get("barcode_preview") or "",
                "box_qty": box_qty,
                "to_obr_qty": to_obr_qty,
                "return_back_qty": return_back_qty,
                "action": action,
                "sort_order": 0 if action == "Доставить" else 1,
            }
        )
    plan_rows.sort(key=lambda row: (int(row.get("sort_order") or 0), str(row.get("box_code") or "")))
    return plan_rows


__all__ = [
    "MOVE_MODE_BOX_FULL",
    "MOVE_MODE_BOX_PARTIAL",
    "MOVE_MODE_PALLET_FULL",
    "_all_boxes_for_pallet",
    "_barcode_qty_total",
    "_box_execution_plan",
    "_consume_box_qty",
    "_consume_pallet_qty",
    "_matching_stock_boxes_for_pallet",
    "_move_boxes_to_otg",
    "_normalize_barcode_qty_map",
    "_normalize_box_code",
    "_normalize_move_mode",
    "_pallet_total_qty",
    "_pallet_box_plan",
    "_parse_box_codes",
    "_parse_json_list",
    "_partial_request_covers_full_pallet",
    "_planned_box_codes_for_move",
    "_payload_box_codes",
    "_remove_boxes_from_pallet",
    "_requested_barcode_qty",
    "_requested_partial_rows",
    "_resolve_box_partial_codes",
    "_resolve_otg_box_codes",
    "_single_requested_box",
]
