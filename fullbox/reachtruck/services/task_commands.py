from __future__ import annotations

import copy
import re
import unicodedata
from dataclasses import dataclass
from types import SimpleNamespace

from django.db import transaction
from django.utils import timezone

from audit.models import OrderAuditEntry, log_order_action, log_stock_move
from reachtruck.models import MoveTask
from sku.models import SKUBarcode
from sklad.models import WarehouseOperation
from sklad.services.stock_operations import OperationalStockService
from sklad.services.warehouse_write_path import WarehouseWritePathService

from .move_requests import (
    _as_int,
    _build_location,
    _normalize_goods_type,
    _normalize_zone_code,
    _os_line_display_label,
    sync_task_status_by_legacy_order_id,
)
from .pallet_ops import (
    MOVE_MODE_BOX_FULL,
    MOVE_MODE_BOX_PARTIAL,
    _barcode_qty_total,
    _box_match_breakdown,
    _consume_box_qty,
    _consume_pallet_qty,
    _find_box_in_placement,
    _find_pallet_in_placement,
    _matching_boxes_for_pallet,
    _move_boxes_to_otg,
    _normalize_barcode_qty_map,
    _normalize_box_code,
    _normalize_move_mode,
    _parse_json_list,
    _payload_box_codes,
    _remove_boxes_from_pallet,
    _resolved_item_barcode,
    _requested_barcode_qty,
    _requested_partial_rows,
    _resolve_otg_box_codes,
    _single_requested_box,
)


@dataclass
class MoveTaskCommandResult:
    ok: bool
    error: str = ""
    task: MoveTask | None = None
    payload: dict | None = None
    message: str = ""
    completed: bool = False


def _normalize_legacy_order_ids(legacy_order_ids) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in legacy_order_ids or []:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _load_request_tasks(legacy_order_ids) -> list[MoveTask]:
    normalized_ids = _normalize_legacy_order_ids(legacy_order_ids)
    if not normalized_ids:
        return []
    tasks = list(
        MoveTask.objects.select_related("request", "request__agency")
        .filter(legacy_order_id__in=normalized_ids)
        .order_by("created_at", "id")
    )
    by_id = {str(task.legacy_order_id or "").strip(): task for task in tasks}
    return [by_id[target_id] for target_id in normalized_ids if target_id in by_id]


def _request_task_runtime_state(task: MoveTask) -> dict:
    payload = dict(task.payload or {})
    execution = dict(payload.get("mobile_execution") or {})
    status = _task_payload_status(task, payload)
    assigned_to_id = _task_assignee_id(task, payload)
    pallet_code = display_scan_text(payload.get("pallet_code"))
    destination_code = _location_scan_code(payload.get("to_location") or {})
    destination_label = str(payload.get("to_label") or destination_code).strip() or destination_code
    return {
        "task": task,
        "payload": payload,
        "execution": execution,
        "status": status,
        "assigned_to_id": assigned_to_id,
        "pallet_code": pallet_code,
        "destination_code": destination_code,
        "destination_label": destination_label,
        "pallet_confirmed": bool(execution.get("pallet_confirmed")),
        "destination_confirmed": bool(execution.get("destination_confirmed")),
    }


def build_mobile_request_execution_snapshot(
    legacy_order_ids,
    *,
    employee_id: int | None = None,
) -> dict:
    tasks = _load_request_tasks(legacy_order_ids)
    if not tasks:
        return {}

    states = [_request_task_runtime_state(task) for task in tasks]
    remaining = [state for state in states if state["status"] != MoveTask.STATUS_DONE]
    active_task = next(
        (
            state
            for state in remaining
            if state["status"] == MoveTask.STATUS_IN_PROGRESS
            and state["assigned_to_id"]
            and (employee_id is None or state["assigned_to_id"] == employee_id)
            and state["pallet_confirmed"]
            and not state["destination_confirmed"]
        ),
        None,
    )
    taken_by_other = any(
        state["status"] == MoveTask.STATUS_IN_PROGRESS
        and state["assigned_to_id"]
        and employee_id is not None
        and state["assigned_to_id"] != employee_id
        for state in remaining
    )
    all_taken_by_current = bool(remaining) and all(
        state["status"] == MoveTask.STATUS_IN_PROGRESS
        and state["assigned_to_id"]
        and (employee_id is None or state["assigned_to_id"] == employee_id)
        for state in remaining
    )
    can_take = bool(remaining) and not taken_by_other and not all_taken_by_current
    can_scan = bool(remaining) and not taken_by_other and all_taken_by_current
    prompt = "Отсканируйте QR-код паллеты из заявки."
    expected_scan = ""
    current_step = "pallet"
    if active_task:
        prompt = f"Отвези -> {active_task['destination_code']}"
        expected_scan = active_task["destination_code"]
        current_step = "destination"
    return {
        "total_count": len(tasks),
        "remaining_count": len(remaining),
        "completed_count": max(len(tasks) - len(remaining), 0),
        "can_take": can_take,
        "can_scan": can_scan,
        "taken_by_other": taken_by_other,
        "current_step": current_step,
        "prompt": prompt,
        "expected_scan": expected_scan,
        "active_order_id": str(active_task["task"].legacy_order_id).strip() if active_task else "",
        "active_pallet_code": active_task["pallet_code"] if active_task else "",
        "active_destination_code": active_task["destination_code"] if active_task else "",
        "active_destination_label": active_task["destination_label"] if active_task else "",
    }


def _load_task(legacy_order_id: str) -> MoveTask | None:
    target_id = str(legacy_order_id or "").strip()
    if not target_id:
        return None
    return (
        MoveTask.objects.select_related("request", "request__agency")
        .filter(legacy_order_id=target_id)
        .order_by("-updated_at")
        .first()
    )


def _task_payload_status(task: MoveTask, payload: dict) -> str:
    task_status = str(task.status or "").strip().lower()
    if task_status:
        return task_status
    return str(payload.get("status") or "").strip().lower()


def _task_assignee_id(task: MoveTask, payload: dict) -> int | None:
    assigned_to_id = payload.get("assigned_to_id")
    if assigned_to_id in (None, ""):
        return int(task.assigned_to_id) if task.assigned_to_id else None
    try:
        return int(assigned_to_id)
    except (TypeError, ValueError):
        return int(task.assigned_to_id) if task.assigned_to_id else None


def _legacy_location_scan_code(location: dict | None) -> str:
    location = location or {}
    zone = _normalize_zone_code(location.get("zone") or "") or "PR"
    row = _as_int(location.get("row"))
    section = _as_int(location.get("section"))
    tier = _as_int(location.get("tier"))
    cell = _as_int(location.get("cell"))
    if zone == "OS":
        return f"OS-{row}-{section}-{tier}-{cell}"
    if zone == "MR":
        return f"MR-{row}" if row else "MR"
    return zone


def _location_scan_code(location: dict | None) -> str:
    location = location or {}
    zone = _normalize_zone_code(location.get("zone") or "") or "PR"
    row = _as_int(location.get("row"))
    section = _as_int(location.get("section"))
    tier = _as_int(location.get("tier"))
    cell = _as_int(location.get("cell"))
    if zone == "OS":
        line_label = _os_line_display_label(section)
        if line_label and row and tier and cell:
            return f"{line_label}-{row}/{tier}-{cell}"
        if line_label and row:
            return f"{line_label}-{row}"
    return _legacy_location_scan_code(location)


_SCAN_REPAIR_STEPS = (
    ("gb18030", "utf-8"),
    ("gbk", "utf-8"),
    ("cp1252", "utf-8"),
    ("cp1251", "utf-8"),
    ("latin1", "cp1251"),
    ("latin1", "utf-8"),
)
_SCAN_DASH_TRANSLATION = str.maketrans(
    {
        "–": "-",
        "—": "-",
        "−": "-",
        "‑": "-",
        "‐": "-",
        "‒": "-",
        "﹣": "-",
        "－": "-",
    }
)


def _normalized_scan_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.translate(_SCAN_DASH_TRANSLATION)
    text = text.replace("\u00a0", " ")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return "".join(text.split()).casefold()


def _scan_compare_variants(value: str | None) -> set[str]:
    source = str(value or "")
    queue = [source]
    seen_raw: set[str] = set()
    variants: set[str] = set()
    while queue and len(seen_raw) < 16:
        current = queue.pop(0)
        if current in seen_raw:
            continue
        seen_raw.add(current)
        normalized = _normalized_scan_text(current)
        if normalized:
            variants.add(normalized)
        for encoding, decoding in _SCAN_REPAIR_STEPS:
            try:
                repaired = current.encode(encoding).decode(decoding)
            except (UnicodeEncodeError, UnicodeDecodeError, LookupError):
                continue
            if repaired and repaired not in seen_raw:
                queue.append(repaired)
    if not variants:
        variants.add("")
    return variants


def _display_scan_variants(value: str | None) -> list[str]:
    source = str(value or "").strip()
    if not source:
        return [""]
    queue = [source]
    seen_raw: set[str] = set()
    variants: list[str] = []
    while queue and len(seen_raw) < 16:
        current = queue.pop(0)
        if current in seen_raw:
            continue
        seen_raw.add(current)
        variants.append(current)
        for encoding, decoding in _SCAN_REPAIR_STEPS:
            try:
                repaired = current.encode(encoding).decode(decoding)
            except (UnicodeEncodeError, UnicodeDecodeError, LookupError):
                continue
            repaired = str(repaired or "").strip()
            if repaired and repaired not in seen_raw:
                queue.append(repaired)
    return variants or [source]


def _display_variant_score(value: str) -> tuple[int, int, int, int, int]:
    text = str(value or "")
    cyrillic = sum(1 for ch in text if "\u0400" <= ch <= "\u04FF")
    ascii_alnum = sum(1 for ch in text if ch.isascii() and ch.isalnum())
    cjk = sum(
        1
        for ch in text
        if (
            "\u3400" <= ch <= "\u4DBF"
            or "\u4E00" <= ch <= "\u9FFF"
            or "\uF900" <= ch <= "\uFAFF"
        )
    )
    replacements = text.count("�") + text.count("?")
    normalized = sum(1 for ch in text if ch in "-_/ " or ch.isalnum())
    return (cyrillic, -cjk, -replacements, normalized, ascii_alnum)


def display_scan_text(value: str | None) -> str:
    variants = _display_scan_variants(value)
    return max(variants, key=_display_variant_score)


def _same_scan_value(left: str | None, right: str | None) -> bool:
    return bool(_scan_compare_variants(left) & _scan_compare_variants(right))


def _pallet_tail_variants(value: str | None) -> set[str]:
    tails: set[str] = set()
    for variant in _scan_compare_variants(value):
        parts = [part for part in variant.split("-") if part]
        if len(parts) >= 2:
            tails.add("-".join(parts[1:]))
    return tails


def _pallet_numeric_variants(value: str | None) -> set[str]:
    variants: set[str] = set()
    for candidate in _scan_compare_variants(value):
        digits = "".join(re.findall(r"\d+", candidate))
        if len(digits) >= 8:
            variants.add(digits)
    return variants


def _same_pallet_code_scan(left: str | None, right: str | None) -> bool:
    if _same_scan_value(left, right):
        return True
    if _pallet_tail_variants(left) & _pallet_tail_variants(right):
        return True
    return bool(_pallet_numeric_variants(left) & _pallet_numeric_variants(right))


def _location_scan_variants(location: dict | None) -> set[str]:
    display_code = _location_scan_code(location)
    legacy_code = _legacy_location_scan_code(location)
    return {
        code
        for code in (display_code, legacy_code)
        if str(code or "").strip()
    }


def _same_location_scan(scan_value: str | None, location: dict | None) -> bool:
    return any(_same_scan_value(scan_value, candidate) for candidate in _location_scan_variants(location))


def _mobile_selector_sets(payload: dict) -> tuple[set[str], set[str], set[str], int]:
    requested_qty = _as_int(payload.get("requested_qty"))
    requested_barcode_qty = _requested_barcode_qty(payload)
    if requested_qty <= 0 and requested_barcode_qty:
        requested_qty = _barcode_qty_total(requested_barcode_qty)
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
            SKUBarcode.objects.filter(value__in=barcode_values).values_list("sku__sku_code", flat=True)
        )
        sku_values.discard(None)
    requested_goods_type = _normalize_goods_type(payload.get("requested_goods_type"))
    goods_type_values = {requested_goods_type} if requested_goods_type else set()
    return barcode_values, sku_values, goods_type_values, requested_qty


def _mobile_allocate_barcode_qty(
    source_qty: dict[str, int],
    qty_needed: int,
    remaining_by_barcode: dict[str, int] | None = None,
) -> dict[str, int]:
    available = {
        str(barcode).strip(): _as_int(qty)
        for barcode, qty in dict(source_qty or {}).items()
        if str(barcode or "").strip() and _as_int(qty) > 0
    }
    remaining = _as_int(qty_needed)
    result: dict[str, int] = {}
    if remaining_by_barcode:
        for barcode, needed in list(remaining_by_barcode.items()):
            take = min(_as_int(needed), _as_int(available.get(barcode)), remaining)
            if take <= 0:
                continue
            result[barcode] = result.get(barcode, 0) + take
            available[barcode] = _as_int(available.get(barcode)) - take
            remaining_by_barcode[barcode] = _as_int(remaining_by_barcode.get(barcode)) - take
            remaining -= take
            if remaining <= 0:
                break
    if remaining <= 0:
        return result
    for barcode, qty in available.items():
        take = min(_as_int(qty), remaining)
        if take <= 0:
            continue
        result[barcode] = result.get(barcode, 0) + take
        remaining -= take
        if remaining <= 0:
            break
    return result


def _mobile_build_row_barcode_qty(
    placement_payload: dict,
    pallet_code: str,
    box_code: str,
    payload: dict,
    qty_needed: int,
    explicit_barcode_qty: dict[str, int] | None = None,
) -> dict[str, int]:
    barcode_values, sku_values, goods_type_values, _requested_qty = _mobile_selector_sets(payload)
    _total, box_barcode_qty = _box_match_breakdown(
        placement_payload,
        pallet_code,
        box_code,
        barcode_values,
        sku_values,
        goods_type_values,
    )
    remaining_by_barcode = (
        {
            str(barcode).strip(): _as_int(qty)
            for barcode, qty in dict(explicit_barcode_qty or {}).items()
            if str(barcode or "").strip() and _as_int(qty) > 0
        }
        if explicit_barcode_qty
        else None
    )
    allocated = _mobile_allocate_barcode_qty(box_barcode_qty, qty_needed, remaining_by_barcode)
    if allocated:
        return allocated
    boxes, box_idx, box = _find_box_in_placement(placement_payload, box_code)
    if box_idx < 0 or not box:
        return {}
    remaining = _as_int(qty_needed)
    fallback: dict[str, int] = {}
    for item in box.get("items") or []:
        barcode = _resolved_item_barcode(item) or str(item.get("sku") or item.get("sku_code") or "").strip()
        qty = _as_int(item.get("qty"))
        if not barcode or qty <= 0:
            continue
        take = min(qty, remaining)
        if take <= 0:
            continue
        fallback[barcode] = fallback.get(barcode, 0) + take
        remaining -= take
        if remaining <= 0:
            break
    return fallback


def _mobile_plan_partial_rows(
    payload: dict,
    placement_payload: dict,
    pallet_code: str,
) -> list[dict]:
    requested_rows = _requested_partial_rows(payload)
    normalized_rows: list[dict] = []
    if requested_rows:
        for row in requested_rows:
            box_code = _normalize_box_code(row.get("box_code"))
            qty = _as_int(row.get("qty"))
            if not box_code or qty <= 0:
                continue
            barcode_qty = _mobile_build_row_barcode_qty(
                placement_payload,
                pallet_code,
                box_code,
                payload,
                qty,
                explicit_barcode_qty=_normalize_barcode_qty_map(row.get("barcode_qty")),
            )
            normalized_rows.append(
                {
                    "box_code": box_code,
                    "qty": qty,
                    "barcode_qty": barcode_qty,
                }
            )
        if normalized_rows:
            return normalized_rows

    requested_box = _single_requested_box(payload)
    requested_codes = _payload_box_codes(payload)
    requested_barcode_qty = _requested_barcode_qty(payload)
    barcode_values, sku_values, goods_type_values, requested_qty = _mobile_selector_sets(payload)
    if requested_box:
        qty = requested_qty or _barcode_qty_total(requested_barcode_qty)
        if qty <= 0:
            qty = _as_int(
                _box_match_breakdown(
                    placement_payload,
                    pallet_code,
                    requested_box,
                    barcode_values,
                    sku_values,
                    goods_type_values,
                )[0]
            )
        if qty > 0:
            return [
                {
                    "box_code": requested_box,
                    "qty": qty,
                    "barcode_qty": _mobile_build_row_barcode_qty(
                        placement_payload,
                        pallet_code,
                        requested_box,
                        payload,
                        qty,
                        explicit_barcode_qty=requested_barcode_qty,
                    ),
                }
            ]
    if requested_codes:
        result: list[dict] = []
        for code in requested_codes:
            total, _barcode_qty = _box_match_breakdown(
                placement_payload,
                pallet_code,
                code,
                barcode_values,
                sku_values,
                goods_type_values,
            )
            qty = total or requested_qty
            if qty <= 0:
                continue
            result.append(
                {
                    "box_code": code,
                    "qty": qty,
                    "barcode_qty": _mobile_build_row_barcode_qty(
                        placement_payload,
                        pallet_code,
                        code,
                        payload,
                        qty,
                        explicit_barcode_qty=requested_barcode_qty,
                    ),
                }
            )
        if result:
            return result

    candidates = _matching_boxes_for_pallet(
        placement_payload,
        pallet_code,
        barcode_values,
        sku_values,
        goods_type_values,
    )
    candidates.sort(key=lambda row: (_as_int(row.get("qty")) or 0, str(row.get("code") or "")))
    if not candidates:
        return []

    remaining_by_barcode = {
        str(barcode).strip(): _as_int(qty)
        for barcode, qty in requested_barcode_qty.items()
        if str(barcode or "").strip() and _as_int(qty) > 0
    }
    remaining_qty = requested_qty or _barcode_qty_total(remaining_by_barcode)
    rows: list[dict] = []
    for candidate in candidates:
        box_code = _normalize_box_code(candidate.get("code"))
        box_qty = _as_int(candidate.get("qty"))
        if not box_code or box_qty <= 0:
            continue
        if remaining_by_barcode:
            barcode_qty = _mobile_allocate_barcode_qty(
                candidate.get("barcode_qty") or {},
                box_qty,
                remaining_by_barcode,
            )
            qty = _barcode_qty_total(barcode_qty)
        else:
            qty = min(box_qty, remaining_qty)
            barcode_qty = _mobile_build_row_barcode_qty(
                placement_payload,
                pallet_code,
                box_code,
                payload,
                qty,
            )
        if qty <= 0:
            continue
        rows.append({"box_code": box_code, "qty": qty, "barcode_qty": barcode_qty})
        remaining_qty = max(remaining_qty - qty, 0)
        if remaining_qty <= 0 and all(qty <= 0 for qty in remaining_by_barcode.values()):
            break
    return rows


def _mobile_sync_execution_payload(task: MoveTask, payload: dict, placement_payload: dict) -> dict:
    move_mode = _normalize_move_mode(payload.get("move_mode"), payload.get("pick_mode"))
    if move_mode == MOVE_MODE_BOX_PARTIAL and not _requested_partial_rows(payload):
        planned_rows = _mobile_plan_partial_rows(payload, placement_payload, str(payload.get("pallet_code") or "").strip())
        if planned_rows:
            payload["requested_rows"] = planned_rows
            payload["requested_boxes"] = [row["box_code"] for row in planned_rows]
            payload["requested_box"] = planned_rows[0]["box_code"] if len(planned_rows) == 1 else ""
            payload["requested_qty"] = sum(_as_int(row.get("qty")) for row in planned_rows)
    payload.setdefault("mobile_execution", {})
    return payload


def _mobile_box_specs(payload: dict, placement_payload: dict) -> list[dict]:
    pallet_code = str(payload.get("pallet_code") or "").strip()
    move_mode = _normalize_move_mode(payload.get("move_mode"), payload.get("pick_mode"))
    pallets, _pallet_idx, pallet = _find_pallet_in_placement(placement_payload, pallet_code)
    pallet_box_codes = [
        _normalize_box_code(code)
        for code in ((pallet or {}).get("boxes") or [])
        if _normalize_box_code(code)
    ]
    if move_mode == MoveTask.MODE_PALLET_FULL:
        specs = []
        for box_code in pallet_box_codes:
            _boxes, _box_idx, box = _find_box_in_placement(placement_payload, box_code)
            box_qty = 0
            for item in (box or {}).get("items") or []:
                box_qty += _as_int(item.get("qty"))
            specs.append(
                {
                    "box_code": box_code,
                    "box_qty": box_qty,
                    "units_required": 0,
                    "unit_barcode_qty": {},
                    "return_required": False,
                }
            )
        return specs
    if move_mode == MoveTask.MODE_BOX_FULL:
        requested_codes = _payload_box_codes(payload)
        if not requested_codes:
            requested_codes = list(pallet_box_codes)
        specs = []
        for box_code in requested_codes:
            _boxes, _box_idx, box = _find_box_in_placement(placement_payload, box_code)
            box_qty = 0
            for item in (box or {}).get("items") or []:
                box_qty += _as_int(item.get("qty"))
            specs.append(
                {
                    "box_code": box_code,
                    "box_qty": box_qty,
                    "units_required": 0,
                    "unit_barcode_qty": {},
                    "return_required": False,
                }
            )
        return specs

    specs = []
    for row in _mobile_plan_partial_rows(payload, placement_payload, pallet_code):
        box_code = _normalize_box_code(row.get("box_code"))
        qty = _as_int(row.get("qty"))
        if not box_code or qty <= 0:
            continue
        _boxes, _box_idx, box = _find_box_in_placement(placement_payload, box_code)
        box_qty = 0
        for item in (box or {}).get("items") or []:
            box_qty += _as_int(item.get("qty"))
        specs.append(
            {
                "box_code": box_code,
                "box_qty": box_qty,
                "units_required": qty,
                "unit_barcode_qty": _normalize_barcode_qty_map(row.get("barcode_qty")),
                "return_required": box_qty > qty,
            }
        )
    return specs


def build_mobile_execution_snapshot(legacy_order_id: str) -> dict:
    task = _load_task(legacy_order_id)
    if not task:
        return {}
    payload = dict(task.payload or {})
    placement_entry = _find_placement_entry_for_pallet(
        str(payload.get("pallet_code") or "").strip(),
        receiving_order_id=str(payload.get("receiving_order_id") or "").strip() or None,
        agency_id=int(task.request.agency_id) if task.request and task.request.agency_id else None,
    )
    placement_payload = dict(placement_entry.payload or {}) if placement_entry and isinstance(placement_entry.payload, dict) else {}
    payload = _mobile_sync_execution_payload(task, payload, placement_payload)
    execution = dict(payload.get("mobile_execution") or {})
    scanned_boxes = {
        _normalize_box_code(code).lower()
        for code in (execution.get("boxes_scanned") or [])
        if _normalize_box_code(code)
    }
    scanned_units_raw = execution.get("units_scanned") or {}
    scanned_units: dict[str, dict[str, int]] = {}
    for box_code, values in dict(scanned_units_raw).items():
        normalized_box = _normalize_box_code(box_code)
        if not normalized_box or not isinstance(values, dict):
            continue
        scanned_units[normalized_box.lower()] = {
            str(barcode).strip(): _as_int(qty)
            for barcode, qty in values.items()
            if str(barcode or "").strip() and _as_int(qty) > 0
        }

    box_specs = []
    for spec in _mobile_box_specs(payload, placement_payload):
        box_code = _normalize_box_code(spec.get("box_code"))
        unit_plan = _normalize_barcode_qty_map(spec.get("unit_barcode_qty"))
        unit_scanned = scanned_units.get(box_code.lower(), {})
        unit_total = _barcode_qty_total(unit_plan)
        unit_scanned_total = min(_barcode_qty_total(unit_scanned), unit_total)
        requires_unit_scan = unit_total > 0
        box_scanned = box_code.lower() in scanned_boxes
        box_complete = box_scanned and (not requires_unit_scan or unit_scanned_total >= unit_total)
        box_specs.append(
            {
                "box_code": box_code,
                "box_qty": _as_int(spec.get("box_qty")),
                "units_required": _as_int(spec.get("units_required")),
                "unit_barcode_qty": unit_plan,
                "unit_barcode_preview": ", ".join(
                    f"{barcode} × {qty}" for barcode, qty in list(unit_plan.items())[:3]
                ),
                "unit_scanned_total": unit_scanned_total,
                "box_scanned": box_scanned,
                "requires_unit_scan": requires_unit_scan,
                "box_complete": box_complete,
                "return_required": bool(spec.get("return_required")),
            }
        )

    source_label = payload.get("from_label") or ""
    destination_label = payload.get("to_label") or ""
    source_code = _location_scan_code(payload.get("from_location") or {})
    destination_code = _location_scan_code(payload.get("to_location") or {})
    source_confirmed = bool(execution.get("source_confirmed"))
    pallet_confirmed = bool(execution.get("pallet_confirmed"))
    destination_confirmed = bool(execution.get("destination_confirmed"))
    boxes_completed = sum(1 for row in box_specs if row["box_complete"])
    boxes_total = len(box_specs)
    boxes_ready = source_confirmed and pallet_confirmed
    all_boxes_complete = bool(box_specs) and boxes_completed >= boxes_total
    expected_units_total = sum(_barcode_qty_total(row["unit_barcode_qty"]) for row in box_specs)
    scanned_units_total = sum(int(row["unit_scanned_total"]) for row in box_specs)
    current_step = "source"
    expected_scan = source_code
    prompt = f"Подъедь к месту {source_label or source_code} и отсканируй код места."
    if source_confirmed:
        current_step = "pallet"
        expected_scan = display_scan_text(payload.get("pallet_code"))
        prompt = f"Отсканируй паллету {expected_scan}."
    if source_confirmed and pallet_confirmed:
        current_step = "boxes"
        expected_scan = ", ".join(row["box_code"] for row in box_specs if not row["box_complete"]) or "-"
        prompt = "Сканируй нужные короба в любом порядке."
    if source_confirmed and pallet_confirmed and all_boxes_complete:
        current_step = "destination"
        expected_scan = destination_code
        prompt = f"Отвези -> {destination_code}"
    if destination_confirmed:
        current_step = "done"
        expected_scan = ""
        prompt = "Задание выполнено."
    return {
        "source_code": source_code,
        "source_label": source_label,
        "source_confirmed": source_confirmed,
        "pallet_code": display_scan_text(payload.get("pallet_code")),
        "pallet_confirmed": pallet_confirmed,
        "destination_code": destination_code,
        "destination_label": destination_label,
        "destination_confirmed": destination_confirmed,
        "boxes": box_specs,
        "boxes_pending": [row for row in box_specs if not row["box_complete"]],
        "boxes_found": [row for row in box_specs if row["box_complete"]],
        "boxes_total": boxes_total,
        "boxes_completed": boxes_completed,
        "boxes_ready": boxes_ready,
        "all_boxes_complete": all_boxes_complete,
        "expected_units_total": expected_units_total,
        "scanned_units_total": scanned_units_total,
        "current_step": current_step,
        "expected_scan": expected_scan,
        "prompt": prompt,
        "task_status": _task_payload_status(task, payload),
    }


def scan_move_task_step(
    *,
    legacy_order_id: str,
    scan_value: str,
    user,
    employee_id: int | None,
    employee_name: str,
) -> MoveTaskCommandResult:
    task = _load_task(legacy_order_id)
    if not task:
        return MoveTaskCommandResult(ok=False, error="Задание не найдено.")
    if not employee_id:
        return MoveTaskCommandResult(ok=False, error="Профиль сотрудника не найден.")

    payload = dict(task.payload or {})
    status = _task_payload_status(task, payload)
    assigned_to_id = _task_assignee_id(task, payload)
    if status != MoveTask.STATUS_IN_PROGRESS:
        return MoveTaskCommandResult(ok=False, error="Сначала возьмите задание в работу.")
    if assigned_to_id and assigned_to_id != employee_id:
        return MoveTaskCommandResult(ok=False, error="Задание назначено другому водителю.")

    pallet_code = str(payload.get("pallet_code") or "").strip()
    pallet_code_display = display_scan_text(pallet_code)
    placement_entry = _find_placement_entry_for_pallet(
        pallet_code,
        receiving_order_id=str(payload.get("receiving_order_id") or "").strip() or None,
        agency_id=int(task.request.agency_id) if task.request and task.request.agency_id else None,
    )
    if not placement_entry:
        return MoveTaskCommandResult(ok=False, error="Паллета не найдена в размещении.")
    placement_payload = dict(placement_entry.payload or {}) if isinstance(placement_entry.payload, dict) else {}
    payload = _mobile_sync_execution_payload(task, payload, placement_payload)
    execution = dict(payload.get("mobile_execution") or {})
    execution.setdefault("boxes_scanned", [])
    execution.setdefault("units_scanned", {})
    scan_code = str(scan_value or "").strip()
    if not scan_code:
        return MoveTaskCommandResult(ok=False, error="Отсканируйте код.")

    snapshot = build_mobile_execution_snapshot(legacy_order_id)
    if not snapshot:
        return MoveTaskCommandResult(ok=False, error="Не удалось подготовить сценарий сканирования.")
    source_code = snapshot["source_code"]
    destination_code = snapshot["destination_code"]
    from_location = payload.get("from_location") or {}
    to_location = payload.get("to_location") or {}

    if not snapshot["source_confirmed"]:
        if not _same_location_scan(scan_code, from_location):
            return MoveTaskCommandResult(
                ok=False,
                error=f"Неверный код места. Ожидалось: {source_code}.",
            )
        execution["source_confirmed"] = True
        execution["last_scan"] = scan_code
        payload["mobile_execution"] = execution
        task.payload = payload
        task.save(update_fields=["payload", "updated_at"])
        return MoveTaskCommandResult(ok=True, task=task, payload=payload, message="Место хранения подтверждено.")

    if not snapshot["pallet_confirmed"]:
        if not _same_pallet_code_scan(scan_code, pallet_code):
            return MoveTaskCommandResult(
                ok=False,
                error=f"Неверный код паллеты. Ожидалось: {pallet_code_display}.",
            )
        execution["pallet_confirmed"] = True
        execution["last_scan"] = scan_code
        payload["mobile_execution"] = execution
        task.payload = payload
        task.save(update_fields=["payload", "updated_at"])
        return MoveTaskCommandResult(ok=True, task=task, payload=payload, message="Паллета подтверждена.")

    pending_boxes = [row for row in snapshot["boxes"] if not row["box_complete"]]
    scanned_boxes = {
        _normalize_box_code(code).lower()
        for code in (execution.get("boxes_scanned") or [])
        if _normalize_box_code(code)
    }
    units_scanned = dict(execution.get("units_scanned") or {})
    for row in pending_boxes:
        if _same_scan_value(scan_code, row["box_code"]):
            box_key = row["box_code"].lower()
            if box_key not in scanned_boxes:
                execution["boxes_scanned"] = list(execution.get("boxes_scanned") or []) + [row["box_code"]]
                execution["last_scan"] = scan_code
                payload["mobile_execution"] = execution
                task.payload = payload
                task.save(update_fields=["payload", "updated_at"])
                if row["requires_unit_scan"]:
                    return MoveTaskCommandResult(
                        ok=True,
                        task=task,
                        payload=payload,
                        message=(
                            f"Короб {row['box_code']} найден. Сканируйте товар: "
                            f"{row['unit_scanned_total']} из {row['units_required']}."
                        ),
                    )
                return MoveTaskCommandResult(
                    ok=True,
                    task=task,
                    payload=payload,
                    message=f"Короб {row['box_code']} подтвержден.",
                )
            break

    scanned_box_specs = [
        row for row in pending_boxes if row["box_code"].lower() in scanned_boxes and row["requires_unit_scan"]
    ]
    for row in scanned_box_specs:
        box_key = row["box_code"].lower()
        allowed = row["unit_barcode_qty"]
        if scan_code not in allowed:
            continue
        scanned_for_box = {
            str(barcode).strip(): _as_int(qty)
            for barcode, qty in dict(units_scanned.get(row["box_code"]) or units_scanned.get(box_key) or {}).items()
            if str(barcode or "").strip()
        }
        if _as_int(scanned_for_box.get(scan_code)) >= _as_int(allowed.get(scan_code)):
            return MoveTaskCommandResult(
                ok=False,
                error=f"Товар {scan_code} уже набран полностью для короба {row['box_code']}.",
            )
        scanned_for_box[scan_code] = _as_int(scanned_for_box.get(scan_code)) + 1
        units_scanned[row["box_code"]] = scanned_for_box
        execution["units_scanned"] = units_scanned
        execution["last_scan"] = scan_code
        payload["mobile_execution"] = execution
        task.payload = payload
        task.save(update_fields=["payload", "updated_at"])
        scanned_total = _barcode_qty_total(scanned_for_box)
        message = f"Товар {scan_code} подтвержден: {scanned_total} из {row['units_required']}."
        if scanned_total >= row["units_required"] and row["return_required"]:
            message += f" Верни короб {row['box_code']} на место."
        return MoveTaskCommandResult(ok=True, task=task, payload=payload, message=message)

    snapshot = build_mobile_execution_snapshot(legacy_order_id)
    if snapshot["all_boxes_complete"] and not snapshot["destination_confirmed"]:
        if not _same_location_scan(scan_code, to_location):
            return MoveTaskCommandResult(
                ok=False,
                error=f"Неверный код места назначения. Ожидалось: {destination_code}.",
            )
        execution = dict((task.payload or {}).get("mobile_execution") or {})
        execution["destination_confirmed"] = True
        execution["last_scan"] = scan_code
        payload = dict(task.payload or {})
        payload["mobile_execution"] = execution
        task.payload = payload
        task.save(update_fields=["payload", "updated_at"])
        complete_result = complete_move_task(
            legacy_order_id=legacy_order_id,
            user=user,
            employee_id=employee_id,
            employee_name=employee_name,
        )
        if not complete_result.ok:
            return complete_result
        return MoveTaskCommandResult(
            ok=True,
            task=complete_result.task,
            payload=complete_result.payload,
            message="Место назначения подтверждено. Задание выполнено.",
            completed=True,
        )

    expected = snapshot.get("expected_scan") or "следующий шаг задания"
    return MoveTaskCommandResult(
        ok=False,
        error=f"Неверный код. Сейчас ожидается: {expected}.",
    )


def _find_placement_entry_for_pallet(
    pallet_code: str,
    *,
    receiving_order_id: str | None = None,
    agency_id: int | None = None,
) -> SimpleNamespace | None:
    target = str(pallet_code or "").strip()
    if not target:
        return None
    stock_tree = OperationalStockService.get_pallet_tree(target, agency_id=agency_id)
    if stock_tree is not None:
        return SimpleNamespace(
            order_id=stock_tree.context_order_id,
            order_type=stock_tree.context_order_type,
            agency=stock_tree.agency,
            agency_id=int(stock_tree.agency.id or 0),
            payload=stock_tree.payload,
            stock_tree=stock_tree,
        )
    return None


def _delivered_processing_specs_from_payload(payload: dict) -> list[dict]:
    specs: list[dict] = []
    requested_rows = _requested_partial_rows(payload)
    if requested_rows:
        payload_goods_type = _normalize_goods_type(payload.get("requested_goods_type"))
        for row in requested_rows:
            barcode_qty = _normalize_barcode_qty_map(row.get("barcode_qty"))
            if barcode_qty:
                for barcode, qty in barcode_qty.items():
                    specs.append(
                        {
                            "barcode": barcode,
                            "sku": "",
                            "goods_type": payload_goods_type,
                            "qty": qty,
                        }
                    )
                continue
            row_qty = _as_int(row.get("qty"))
            if row_qty <= 0:
                continue
            specs.append(
                {
                    "barcode": "",
                    "sku": str(payload.get("requested_sku") or "").strip(),
                    "goods_type": payload_goods_type,
                    "qty": row_qty,
                }
            )
        return specs

    requested_barcode_qty = _requested_barcode_qty(payload)
    if requested_barcode_qty:
        payload_goods_type = _normalize_goods_type(payload.get("requested_goods_type"))
        for barcode, qty in requested_barcode_qty.items():
            specs.append(
                {
                    "barcode": barcode,
                    "sku": "",
                    "goods_type": payload_goods_type,
                    "qty": qty,
                }
            )
        return specs

    request_items = payload.get("request_items") or []
    if isinstance(request_items, list):
        for item in request_items:
            if not isinstance(item, dict):
                continue
            item_qty = _as_int(item.get("requested_qty"))
            if item_qty <= 0:
                continue
            item_goods_type = _normalize_goods_type(item.get("requested_goods_type"))
            item_barcodes = [
                str(value).strip()
                for value in (item.get("requested_barcodes") or [])
                if str(value or "").strip()
            ]
            if len(item_barcodes) == 1:
                specs.append(
                    {
                        "barcode": item_barcodes[0],
                        "sku": "",
                        "goods_type": item_goods_type,
                        "qty": item_qty,
                    }
                )
            else:
                specs.append(
                    {
                        "barcode": "",
                        "sku": str(item.get("requested_article") or "").strip(),
                        "goods_type": item_goods_type,
                        "qty": item_qty,
                    }
                )
    if specs:
        return specs

    requested_qty = _as_int(payload.get("requested_qty"))
    if requested_qty > 0:
        specs.append(
            {
                "barcode": "",
                "sku": str(payload.get("requested_sku") or "").strip(),
                "goods_type": _normalize_goods_type(payload.get("requested_goods_type")),
                "qty": requested_qty,
            }
        )
    return specs


@transaction.atomic
def take_move_task(
    *,
    legacy_order_id: str,
    user,
    employee_id: int | None,
    employee_name: str,
) -> MoveTaskCommandResult:
    task = _load_task(legacy_order_id)
    if not task:
        return MoveTaskCommandResult(ok=False, error="Задание не найдено.")
    if not employee_id:
        return MoveTaskCommandResult(ok=False, error="Профиль сотрудника не найден.")

    payload = dict(task.payload or {})
    status = _task_payload_status(task, payload)
    assigned_to_id = _task_assignee_id(task, payload)
    if status == MoveTask.STATUS_DONE:
        return MoveTaskCommandResult(ok=False, error="Задание уже выполнено.")
    if status == MoveTask.STATUS_IN_PROGRESS and assigned_to_id and assigned_to_id != employee_id:
        return MoveTaskCommandResult(ok=False, error="Задание уже взято другим водителем.")

    payload["status"] = MoveTask.STATUS_IN_PROGRESS
    payload["status_label"] = "В работе"
    payload["assigned_to_id"] = employee_id
    payload["assigned_to_name"] = employee_name
    payload["taken_at"] = timezone.localtime().isoformat()

    authenticated_user = user if getattr(user, "is_authenticated", False) else None
    agency = getattr(task.request, "agency", None)
    log_order_action(
        "status",
        order_id=task.legacy_order_id,
        order_type="stock_move",
        user=authenticated_user,
        agency=agency,
        description=f"Задание {task.legacy_order_id} взято в работу",
        payload=payload,
    )
    log_stock_move(
        "update",
        user=authenticated_user,
        agency=agency,
        description=f"Задание {task.legacy_order_id} взято в работу",
        snapshot={
            "move_id": task.legacy_order_id,
            "pallet_code": payload.get("pallet_code"),
            "from_location": payload.get("from_location"),
            "to_location": payload.get("to_location"),
            "from_label": payload.get("from_label"),
            "to_label": payload.get("to_label"),
            "receiving_order_id": payload.get("receiving_order_id"),
            "status": MoveTask.STATUS_IN_PROGRESS,
            "assigned_to": employee_name,
        },
    )
    sync_task_status_by_legacy_order_id(
        task.legacy_order_id,
        status=MoveTask.STATUS_IN_PROGRESS,
        assigned_to=authenticated_user,
        assigned_to_name=employee_name,
    )
    task.refresh_from_db()
    task.payload = payload
    task.save(update_fields=["payload", "updated_at"])
    return MoveTaskCommandResult(ok=True, task=task, payload=payload)


@transaction.atomic
def take_move_request(
    *,
    legacy_order_ids,
    user,
    employee_id: int | None,
    employee_name: str,
) -> MoveTaskCommandResult:
    tasks = _load_request_tasks(legacy_order_ids)
    if not tasks:
        return MoveTaskCommandResult(ok=False, error="Заявка ричтрака не найдена.")
    if not employee_id:
        return MoveTaskCommandResult(ok=False, error="Профиль сотрудника не найден.")

    authenticated_user = user if getattr(user, "is_authenticated", False) else None
    taken_count = 0
    for task in tasks:
        payload = dict(task.payload or {})
        status = _task_payload_status(task, payload)
        assigned_to_id = _task_assignee_id(task, payload)
        if status == MoveTask.STATUS_DONE:
            continue
        if status == MoveTask.STATUS_IN_PROGRESS and assigned_to_id and assigned_to_id != employee_id:
            return MoveTaskCommandResult(ok=False, error="Часть паллет заявки уже взята другим водителем.")

    for task in tasks:
        payload = dict(task.payload or {})
        status = _task_payload_status(task, payload)
        assigned_to_id = _task_assignee_id(task, payload)
        if status == MoveTask.STATUS_DONE:
            continue
        execution = dict(payload.get("mobile_execution") or {})
        if status != MoveTask.STATUS_IN_PROGRESS or assigned_to_id != employee_id:
            payload["status"] = MoveTask.STATUS_IN_PROGRESS
            payload["status_label"] = "В работе"
            payload["assigned_to_id"] = employee_id
            payload["assigned_to_name"] = employee_name
            payload["taken_at"] = timezone.localtime().isoformat()
            task.status = MoveTask.STATUS_IN_PROGRESS
            task.assigned_to = authenticated_user
            task.assigned_to_name = employee_name
            task.started_at = task.started_at or timezone.now()
            taken_count += 1
        payload["mobile_request_batch_mode"] = True
        execution.setdefault("pallet_confirmed", False)
        execution.setdefault("destination_confirmed", False)
        payload["mobile_execution"] = execution
        task.save(
            update_fields=[
                "status",
                "assigned_to",
                "assigned_to_name",
                "started_at",
                "updated_at",
            ]
        )
        sync_task_status_by_legacy_order_id(
            task.legacy_order_id,
            status=MoveTask.STATUS_IN_PROGRESS,
            assigned_to=authenticated_user,
            assigned_to_name=employee_name,
        )
        task.refresh_from_db()
        task.payload = payload
        task.started_at = task.started_at or timezone.now()
        task.save(update_fields=["payload", "started_at", "updated_at"])
        agency = getattr(task.request, "agency", None)
        log_order_action(
            "status",
            order_id=task.legacy_order_id,
            order_type="stock_move",
            user=authenticated_user,
            agency=agency,
            description=f"Задание {task.legacy_order_id} взято в работу в составе заявки",
            payload=payload,
        )
        log_stock_move(
            "update",
            user=authenticated_user,
            agency=agency,
            description=f"Задание {task.legacy_order_id} взято в работу в составе заявки",
            snapshot={
                "move_id": task.legacy_order_id,
                "pallet_code": payload.get("pallet_code"),
                "from_location": payload.get("from_location"),
                "to_location": payload.get("to_location"),
                "from_label": payload.get("from_label"),
                "to_label": payload.get("to_label"),
                "status": MoveTask.STATUS_IN_PROGRESS,
                "assigned_to": employee_name,
                "request_batch_mode": True,
            },
        )
    message = "Заявка взята в работу." if taken_count else "Заявка уже в работе."
    return MoveTaskCommandResult(ok=True, message=message)


@transaction.atomic
def scan_move_request_step(
    *,
    legacy_order_ids,
    scan_value: str,
    user,
    employee_id: int | None,
    employee_name: str,
) -> MoveTaskCommandResult:
    tasks = _load_request_tasks(legacy_order_ids)
    if not tasks:
        return MoveTaskCommandResult(ok=False, error="Заявка ричтрака не найдена.")
    if not employee_id:
        return MoveTaskCommandResult(ok=False, error="Профиль сотрудника не найден.")

    snapshot = build_mobile_request_execution_snapshot(legacy_order_ids, employee_id=employee_id)
    if not snapshot:
        return MoveTaskCommandResult(ok=False, error="Не удалось подготовить заявку к сканированию.")
    if not snapshot["can_scan"]:
        return MoveTaskCommandResult(ok=False, error="Сначала возьмите всю заявку в работу.")

    scan_code = str(scan_value or "").strip()
    if not scan_code:
        return MoveTaskCommandResult(ok=False, error="Отсканируйте код.")

    task_map = {str(task.legacy_order_id or "").strip(): task for task in tasks}
    authenticated_user = user if getattr(user, "is_authenticated", False) else None
    active_order_id = str(snapshot.get("active_order_id") or "").strip()
    if active_order_id:
        active_task = task_map.get(active_order_id)
        if not active_task:
            return MoveTaskCommandResult(ok=False, error="Активная паллета заявки не найдена.")
        active_payload = dict(active_task.payload or {})
        expected_code = str(snapshot.get("active_destination_code") or "").strip()
        if not _same_location_scan(scan_code, active_payload.get("to_location") or {}):
            return MoveTaskCommandResult(
                ok=False,
                error=f"Неверный код места. Ожидалось: {expected_code}.",
            )
        payload = active_payload
        execution = dict(payload.get("mobile_execution") or {})
        execution["destination_confirmed"] = True
        execution["last_scan"] = scan_code
        payload["mobile_execution"] = execution
        active_task.payload = payload
        active_task.save(update_fields=["payload", "updated_at"])
        complete_result = complete_move_task(
            legacy_order_id=active_order_id,
            user=user,
            employee_id=employee_id,
            employee_name=employee_name,
        )
        if not complete_result.ok:
            return complete_result
        for task in tasks:
            task.refresh_from_db(fields=["status", "payload", "updated_at"])
        remaining_count = sum(1 for task in tasks if task.status != MoveTask.STATUS_DONE)
        if remaining_count <= 0:
            return MoveTaskCommandResult(
                ok=True,
                message="Место подтверждено. Все паллеты по заявке доставлены.",
                completed=True,
            )
        return MoveTaskCommandResult(
            ok=True,
            message="Место подтверждено. Паллета доставлена, сканируйте следующую.",
            completed=False,
        )

    candidate = None
    for task in tasks:
        payload = dict(task.payload or {})
        status = _task_payload_status(task, payload)
        assigned_to_id = _task_assignee_id(task, payload)
        if status != MoveTask.STATUS_IN_PROGRESS:
            continue
        if assigned_to_id and assigned_to_id != employee_id:
            continue
        if not _same_pallet_code_scan(scan_code, payload.get("pallet_code")):
            continue
        candidate = task
        break
    if not candidate:
        return MoveTaskCommandResult(
            ok=False,
            error="Паллета не относится к этой заявке или уже доставлена.",
        )

    payload = dict(candidate.payload or {})
    execution = dict(payload.get("mobile_execution") or {})
    execution["source_confirmed"] = True
    execution["pallet_confirmed"] = True
    execution["last_scan"] = scan_code
    payload["mobile_request_batch_mode"] = True
    payload["mobile_execution"] = execution
    candidate.payload = payload
    candidate.save(update_fields=["payload", "updated_at"])
    pallet_code = display_scan_text(payload.get("pallet_code") or scan_code)
    destination_code = _location_scan_code(payload.get("to_location") or {})
    agency = getattr(candidate.request, "agency", None)
    log_stock_move(
        "update",
        user=authenticated_user,
        agency=agency,
        description=f"Для заявки выбрана паллета {payload.get('pallet_code')}",
        snapshot={
            "move_id": candidate.legacy_order_id,
            "pallet_code": payload.get("pallet_code"),
            "to_label": payload.get("to_label"),
            "request_batch_mode": True,
        },
    )
    return MoveTaskCommandResult(
        ok=True,
        task=candidate,
        payload=payload,
        message=f"Паллета {pallet_code} подтверждена. Отвези -> {destination_code}.",
        completed=False,
    )


@transaction.atomic
def complete_move_task(
    *,
    legacy_order_id: str,
    user,
    employee_id: int | None,
    employee_name: str,
) -> MoveTaskCommandResult:
    task = _load_task(legacy_order_id)
    if not task:
        return MoveTaskCommandResult(ok=False, error="Задание не найдено.")
    if not employee_id:
        return MoveTaskCommandResult(ok=False, error="Профиль сотрудника не найден.")

    payload = dict(task.payload or {})
    status = _task_payload_status(task, payload)
    assigned_to_id = _task_assignee_id(task, payload)
    if status != MoveTask.STATUS_IN_PROGRESS:
        return MoveTaskCommandResult(ok=False, error="Задание еще не взято в работу.")
    if assigned_to_id and assigned_to_id != employee_id:
        return MoveTaskCommandResult(ok=False, error="Задание назначено другому водителю.")

    pallet_code = str(payload.get("pallet_code") or "").strip()
    pallet_code_display = display_scan_text(pallet_code)
    if not pallet_code:
        return MoveTaskCommandResult(ok=False, error="Не найден код паллеты в задании.")

    placement_entry = _find_placement_entry_for_pallet(
        pallet_code,
        receiving_order_id=str(payload.get("receiving_order_id") or "").strip() or None,
        agency_id=int(task.request.agency_id) if task.request and task.request.agency_id else None,
    )
    if not placement_entry:
        return MoveTaskCommandResult(ok=False, error="Паллета не найдена в размещении.")

    placement_payload = copy.deepcopy(placement_entry.payload or {})
    pallets = placement_payload.get("act_pallets") or []
    boxes = placement_payload.get("act_boxes") or []
    to_location = payload.get("to_location") or {}
    to_zone = _normalize_zone_code(to_location.get("zone") or "")
    pick_mode = str(payload.get("pick_mode") or "full").strip().lower()
    move_mode = _normalize_move_mode(payload.get("move_mode"), pick_mode)
    placement_description = f"Перемещение паллеты {pallet_code}"
    done_status_label = "Перемещено"

    if to_zone == "OTG":
        from_location = payload.get("from_location") or {}
        return_location = _build_location(
            from_location.get("zone"),
            _as_int(from_location.get("row")),
            _as_int(from_location.get("section")),
            _as_int(from_location.get("tier")),
            _as_int(from_location.get("cell")),
        )
        otg_location = _build_location(
            to_location.get("zone") or "OTG",
            _as_int(to_location.get("row")),
            _as_int(to_location.get("section")),
            _as_int(to_location.get("tier")),
            _as_int(to_location.get("cell")),
        )
        requested_boxes, resolve_error = _resolve_otg_box_codes(
            placement_payload,
            pallet_code,
            payload,
        )
        if resolve_error:
            return MoveTaskCommandResult(ok=False, error=resolve_error)
        moved_ok, moved_error, moved_meta = _move_boxes_to_otg(
            placement_payload,
            pallet_code,
            requested_boxes,
            otg_location=otg_location,
            return_location=return_location,
        )
        if not moved_ok:
            return MoveTaskCommandResult(
                ok=False,
                error=moved_error or "Не удалось доставить короба в OTG.",
            )
        moved_boxes = moved_meta.get("moved_boxes") or []
        payload["picked_boxes"] = moved_boxes
        requested_qty_value = _as_int(payload.get("requested_qty"))
        payload["picked_qty"] = requested_qty_value if requested_qty_value > 0 else len(moved_boxes)
        payload["picked_rows"] = [{"box_code": code} for code in moved_boxes]
        payload["source_pallet_deleted"] = bool(moved_meta.get("pallet_deleted"))
        payload["source_pallet_returned"] = bool(moved_meta.get("pallet_returned"))
        placement_payload["act_items_removed"] = True
        if moved_meta.get("pallet_deleted"):
            placement_description = (
                f"Короба ({', '.join(moved_boxes)}) доставлены в OTG; "
                f"исходная паллета {pallet_code} пуста и удалена"
            )
            done_status_label = "Короба доставлены в OTG, паллета удалена"
        else:
            placement_description = (
                f"Короба ({', '.join(moved_boxes)}) доставлены в OTG; "
                f"паллета {pallet_code} с остатком возвращена на исходное место"
            )
            done_status_label = "Короба доставлены в OTG, паллета возвращена"
    elif to_zone == "OBR" and move_mode == MOVE_MODE_BOX_PARTIAL:
        requested_qty = _as_int(payload.get("requested_qty"))
        requested_sku = str(payload.get("requested_sku") or "").strip()
        requested_barcode_qty = _requested_barcode_qty(payload)
        requested_rows = _requested_partial_rows(payload)
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
        requested_goods_type = _normalize_goods_type(payload.get("requested_goods_type"))
        goods_type_values = {requested_goods_type} if requested_goods_type else set()
        sku_values = {requested_sku} if requested_sku else set()
        if barcode_values:
            sku_values.update(
                SKUBarcode.objects.filter(value__in=barcode_values)
                .values_list("sku__sku_code", flat=True)
            )
            sku_values.discard(None)
        if requested_barcode_qty:
            barcode_values.update(requested_barcode_qty.keys())
        if not (barcode_values or sku_values):
            return MoveTaskCommandResult(ok=False, error="В задании не указан товар для отбора.")
        picked_qty = 0
        if requested_rows:
            rows_total = sum(_as_int(row.get("qty")) for row in requested_rows)
            if rows_total <= 0:
                return MoveTaskCommandResult(ok=False, error="В задании не указано количество к отбору.")
            if requested_qty > 0 and rows_total != requested_qty:
                return MoveTaskCommandResult(
                    ok=False,
                    error=(
                        f"Сумма отбора по коробам ({rows_total}) не совпадает "
                        f"с количеством в задании ({requested_qty})."
                    ),
                )
            requested_qty = rows_total
            picked_rows = []
            for row_plan in requested_rows:
                row_box = _normalize_box_code(row_plan.get("box_code"))
                row_qty = _as_int(row_plan.get("qty"))
                row_barcode_qty = _normalize_barcode_qty_map(row_plan.get("barcode_qty"))
                if not row_box or row_qty <= 0:
                    return MoveTaskCommandResult(
                        ok=False,
                        error="Некорректные данные по строкам отбора в задании.",
                    )
                row_picked = 0
                if row_barcode_qty:
                    row_total = _barcode_qty_total(row_barcode_qty)
                    if row_total != row_qty:
                        return MoveTaskCommandResult(
                            ok=False,
                            error=(
                                f"В коробе {row_box} разбивка ШК ({row_total}) "
                                f"не совпадает с количеством ({row_qty})."
                            ),
                        )
                    for barcode, barcode_qty in row_barcode_qty.items():
                        consumed_ok, consume_error, picked_part = _consume_box_qty(
                            placement_payload,
                            pallet_code,
                            row_box,
                            barcode_qty,
                            {barcode},
                            set(),
                            goods_type_values,
                        )
                        if not consumed_ok:
                            return MoveTaskCommandResult(
                                ok=False,
                                error=consume_error or f"Не удалось выполнить отбор по ШК {barcode}.",
                            )
                        row_picked += picked_part
                else:
                    consumed_ok, consume_error, row_picked = _consume_box_qty(
                        placement_payload,
                        pallet_code,
                        row_box,
                        row_qty,
                        barcode_values,
                        sku_values,
                        goods_type_values,
                    )
                    if not consumed_ok:
                        return MoveTaskCommandResult(
                            ok=False,
                            error=consume_error or "Не удалось выполнить отбор.",
                        )
                picked_qty += row_picked
                picked_rows.append({"box_code": row_box, "picked_qty": row_picked})
            payload["picked_rows"] = picked_rows
        else:
            if requested_qty <= 0:
                if requested_barcode_qty:
                    requested_qty = _barcode_qty_total(requested_barcode_qty)
                if requested_qty <= 0:
                    return MoveTaskCommandResult(ok=False, error="В задании не указано количество к отбору.")
            if requested_barcode_qty:
                plan_total = _barcode_qty_total(requested_barcode_qty)
                if requested_qty > 0 and plan_total != requested_qty:
                    return MoveTaskCommandResult(
                        ok=False,
                        error=(
                            f"Разбивка по ШК ({plan_total}) не совпадает "
                            f"с количеством в задании ({requested_qty})."
                        ),
                    )
            requested_box = _single_requested_box(payload)
            use_pallet_level_pick = not requested_box and not _payload_box_codes(payload)
            if requested_barcode_qty:
                for barcode, barcode_qty in requested_barcode_qty.items():
                    if use_pallet_level_pick:
                        consumed_ok, consume_error, picked_part = _consume_pallet_qty(
                            placement_payload,
                            pallet_code,
                            barcode_qty,
                            {barcode},
                            set(),
                            goods_type_values,
                        )
                    else:
                        consumed_ok, consume_error, picked_part = _consume_box_qty(
                            placement_payload,
                            pallet_code,
                            requested_box,
                            barcode_qty,
                            {barcode},
                            set(),
                            goods_type_values,
                        )
                    if not consumed_ok:
                        return MoveTaskCommandResult(
                            ok=False,
                            error=consume_error or f"Не удалось выполнить отбор по ШК {barcode}.",
                        )
                    picked_qty += picked_part
                if picked_qty <= 0:
                    return MoveTaskCommandResult(
                        ok=False,
                        error="Не удалось выполнить отбор по заявленной разбивке ШК.",
                    )
            else:
                if use_pallet_level_pick:
                    consumed_ok, consume_error, picked_qty = _consume_pallet_qty(
                        placement_payload,
                        pallet_code,
                        requested_qty,
                        barcode_values,
                        sku_values,
                        goods_type_values,
                    )
                else:
                    consumed_ok, consume_error, picked_qty = _consume_box_qty(
                        placement_payload,
                        pallet_code,
                        requested_box,
                        requested_qty,
                        barcode_values,
                        sku_values,
                        goods_type_values,
                    )
                if not consumed_ok:
                    return MoveTaskCommandResult(
                        ok=False,
                        error=consume_error or "Не удалось выполнить отбор.",
                    )
        payload["picked_qty"] = picked_qty
        placement_payload["act_items_removed"] = True
        if requested_rows and len(requested_rows) > 1:
            placement_description = (
                f"Частичный отбор {picked_qty} шт. из {len(requested_rows)} коробов "
                f"на паллете {pallet_code}"
            )
            done_status_label = "Отбор из коробов выполнен"
        else:
            requested_box = _single_requested_box(payload)
            if requested_box:
                placement_description = (
                    f"Частичный отбор {picked_qty} шт. из короба {requested_box} "
                    f"на паллете {pallet_code}"
                )
                done_status_label = "Отбор из короба выполнен"
            else:
                placement_description = f"Частичный отбор {picked_qty} шт. с паллеты {pallet_code}"
                done_status_label = "Отбор с паллеты выполнен"
    elif to_zone == "OBR" and move_mode == MOVE_MODE_BOX_FULL:
        requested_boxes = _payload_box_codes(payload)
        removed_ok, remove_error, removed_count = _remove_boxes_from_pallet(
            placement_payload,
            pallet_code,
            requested_boxes,
        )
        if not removed_ok:
            return MoveTaskCommandResult(
                ok=False,
                error=remove_error or "Не удалось отобрать короба.",
            )
        payload["picked_boxes"] = requested_boxes
        placement_payload["act_items_removed"] = True
        placement_description = (
            f"Отобраны короба ({', '.join(requested_boxes)}) с паллеты {pallet_code}"
        )
        done_status_label = (
            f"Отобраны короба ({removed_count})"
            if removed_count
            else "Короба переданы в обработку"
        )
    elif to_zone == "OBR":
        updated = False
        removed_boxes = set()
        updated_pallets = []
        for pallet in pallets:
            if not isinstance(pallet, dict):
                continue
            if str(pallet.get("code") or "").strip() == pallet_code:
                updated = True
                for box_code in pallet.get("boxes") or []:
                    code = str(box_code or "").strip()
                    if code:
                        removed_boxes.add(code)
                continue
            updated_pallets.append(pallet)
        if removed_boxes:
            boxes = [
                box
                for box in boxes
                if str((box or {}).get("code") or "").strip() not in removed_boxes
            ]
        if not updated:
            return MoveTaskCommandResult(ok=False, error="Не удалось удалить паллету из размещения.")
        placement_payload["act_pallets"] = updated_pallets
        placement_payload["act_boxes"] = boxes
        placement_payload["act_items_removed"] = True
        placement_description = f"Паллета {pallet_code} передана в зону обработки"
        done_status_label = "Паллета передана в обработку"
    else:
        updated = False
        for pallet in pallets:
            if not isinstance(pallet, dict):
                continue
            if str(pallet.get("code") or "").strip() == pallet_code:
                pallet["location"] = _build_location(
                    to_location.get("zone"),
                    _as_int(to_location.get("row")),
                    _as_int(to_location.get("section")),
                    _as_int(to_location.get("tier")),
                    _as_int(to_location.get("cell")),
                )
                updated = True
                break
        if not updated:
            return MoveTaskCommandResult(ok=False, error="Не удалось обновить локацию паллеты.")
        placement_payload["act_pallets"] = pallets

    placement_payload["act"] = "placement"
    placement_payload["act_state"] = "closed"
    authenticated_user = user if getattr(user, "is_authenticated", False) else None
    move_agency = getattr(task.request, "agency", None)
    warehouse_operation_id = int(payload.get("warehouse_operation_id") or 0)
    warehouse_operation_handled = False
    if warehouse_operation_id:
        putaway_operation = (
            WarehouseOperation.objects.filter(
                id=warehouse_operation_id,
                operation_type=WarehouseOperation.TYPE_PUTAWAY,
            )
            .order_by("id")
            .first()
        )
        if putaway_operation and putaway_operation.status != WarehouseOperation.STATUS_DONE:
            try:
                WarehouseWritePathService.complete_putaway_operation(
                    operation=putaway_operation,
                    performed_by=authenticated_user,
                )
                warehouse_operation_handled = True
            except ValueError:
                pass
        if to_zone == "OBR":
            processing_move_operation = (
                WarehouseOperation.objects.filter(
                    id=warehouse_operation_id,
                    operation_type=WarehouseOperation.TYPE_MOVE_TO_PROCESSING,
                )
                .order_by("id")
                .first()
            )
            if processing_move_operation and processing_move_operation.status != WarehouseOperation.STATUS_DONE:
                try:
                    if processing_move_operation.status != WarehouseOperation.STATUS_IN_PROGRESS:
                        WarehouseWritePathService.start_move_to_processing(
                            operation=processing_move_operation,
                            performed_by=authenticated_user,
                        )
                    WarehouseWritePathService.complete_move_to_processing(
                        operation=processing_move_operation,
                        performed_by=authenticated_user,
                    )
                    warehouse_operation_handled = True
                except ValueError:
                    pass
            processing_order_id = str(
                payload.get("processing_order_id")
                or getattr(processing_move_operation, "context_id", "")
                or ""
            ).strip()
            if processing_order_id and processing_move_operation and processing_move_operation.agency:
                latest_processing_entry = (
                    OrderAuditEntry.objects.filter(order_id=processing_order_id, order_type="processing")
                    .order_by("-created_at", "-id")
                    .first()
                )
                status_payload = latest_processing_entry.payload or {} if latest_processing_entry else {}
                status_value = str(
                    status_payload.get("status") or status_payload.get("submit_action") or ""
                ).strip().lower()
                status_label = str(status_payload.get("status_label") or "").strip().lower()
                if status_value == "processing_in_work" or "взята" in status_label:
                    WarehouseWritePathService.start_processing_if_ready(
                        agency=processing_move_operation.agency,
                        order_id=processing_order_id,
                        started_by=authenticated_user,
                        started_by_role="reachtruck",
                    )
    stock_tree = getattr(placement_entry, "stock_tree", None)
    if not warehouse_operation_handled:
        if stock_tree is not None:
            OperationalStockService.replace_pallet_tree(stock_tree, placement_payload)
        else:
            OperationalStockService.replace_order_placement(
                placement_entry.agency,
                placement_entry.order_type,
                placement_entry.order_id,
                placement_payload,
            )
    log_order_action(
        "status",
        order_id=placement_entry.order_id,
        order_type=placement_entry.order_type,
        user=authenticated_user,
        agency=placement_entry.agency,
        description=placement_description,
        payload=placement_payload,
    )

    payload["status"] = MoveTask.STATUS_DONE
    payload["status_label"] = done_status_label
    payload["completed_at"] = timezone.localtime().isoformat()
    payload["completed_by_name"] = employee_name
    log_order_action(
        "status",
        order_id=task.legacy_order_id,
        order_type="stock_move",
        user=authenticated_user,
        agency=move_agency,
        description=f"Задание {task.legacy_order_id} выполнено",
        payload=payload,
    )
    log_stock_move(
        "update",
        user=authenticated_user,
        agency=move_agency,
        description=f"Задание {task.legacy_order_id} выполнено",
        snapshot={
            "move_id": task.legacy_order_id,
            "pallet_code": pallet_code,
            "from_location": payload.get("from_location"),
            "to_location": payload.get("to_location"),
            "from_label": payload.get("from_label"),
            "to_label": payload.get("to_label"),
            "receiving_order_id": placement_entry.order_id,
            "status": MoveTask.STATUS_DONE,
            "completed_by": employee_name,
        },
    )
    sync_task_status_by_legacy_order_id(
        task.legacy_order_id,
        status=MoveTask.STATUS_DONE,
        assigned_to=authenticated_user,
        assigned_to_name=employee_name,
        qty_done=_as_int(payload.get("picked_qty") or payload.get("requested_qty")),
    )
    task.refresh_from_db()
    task.payload = payload
    task.save(update_fields=["payload", "updated_at"])
    if warehouse_operation_id and not warehouse_operation_handled:
        operation = (
            WarehouseOperation.objects.filter(
                id=warehouse_operation_id,
                operation_type=WarehouseOperation.TYPE_PUTAWAY,
            )
            .order_by("id")
            .first()
        )
        if operation and operation.status != WarehouseOperation.STATUS_DONE:
            try:
                WarehouseWritePathService.complete_putaway_operation(
                    operation=operation,
                    performed_by=authenticated_user,
                )
            except ValueError:
                pass
    if to_zone == "OBR" and warehouse_operation_id:
        operation = (
            WarehouseOperation.objects.filter(
                id=warehouse_operation_id,
                operation_type=WarehouseOperation.TYPE_MOVE_TO_PROCESSING,
            )
            .order_by("id")
            .first()
        )
        if operation and operation.status != WarehouseOperation.STATUS_DONE:
            try:
                if operation.status != WarehouseOperation.STATUS_IN_PROGRESS:
                    WarehouseWritePathService.start_move_to_processing(
                        operation=operation,
                        performed_by=authenticated_user,
                    )
                WarehouseWritePathService.complete_move_to_processing(
                    operation=operation,
                    performed_by=authenticated_user,
                )
            except ValueError:
                pass
        processing_order_id = str(payload.get("processing_order_id") or operation.context_id or "").strip()
        if processing_order_id and operation and operation.agency:
            latest_processing_entry = (
                OrderAuditEntry.objects.filter(order_id=processing_order_id, order_type="processing")
                .order_by("-created_at", "-id")
                .first()
            )
            status_payload = latest_processing_entry.payload or {} if latest_processing_entry else {}
            status_value = str(
                status_payload.get("status") or status_payload.get("submit_action") or ""
            ).strip().lower()
            status_label = str(status_payload.get("status_label") or "").strip().lower()
            if status_value == "processing_in_work" or "взята" in status_label:
                WarehouseWritePathService.start_processing_if_ready(
                    agency=operation.agency,
                    order_id=processing_order_id,
                    started_by=authenticated_user,
                    started_by_role="reachtruck",
                )
    return MoveTaskCommandResult(ok=True, task=task, payload=payload)
