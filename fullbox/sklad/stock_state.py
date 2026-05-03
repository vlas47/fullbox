import re
from collections import defaultdict

from django.db import models
from django.db import transaction

from audit.models import OrderAuditEntry
from sku.models import Agency, SKU, SKUBarcode
from sklad.services.stock_availability import StockAvailabilityService

from .models import StockPalletState

GOODS_TYPE_LABELS = {
    "op": "Оптовый",
    "gv": "Готовый",
    "br": "Брак",
    "vz": "Возврат",
    "rh": "Расходный",
    "no": "Не обработанный",
}
_ZONE_DEFAULT_BY_ORDER = {
    "receiving": "PR",
    "processing": "OBR",
}


def _parse_int_value(raw) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return 0


def _parse_qty_value(raw):
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _normalize_goods_type(raw: str | None) -> str:
    return StockAvailabilityService.normalize_goods_type(raw)


def _goods_type_label(raw_type: str | None, raw_label: str | None, default_label: str) -> str:
    label = str(raw_label or "").strip()
    if label:
        return label
    key = str(raw_type or "").strip().lower()
    if key in GOODS_TYPE_LABELS:
        return GOODS_TYPE_LABELS[key]
    return default_label


def _inventory_key(sku: str, size: str, goods_type: str) -> tuple[str, str, str]:
    return (
        (sku or "").strip().lower(),
        (size or "").strip().lower(),
        _normalize_goods_type(goods_type),
    )


def _order_goods_type_map(entries: list[OrderAuditEntry]) -> dict[tuple[str, str], str]:
    goods_type_by_order = {}
    for entry in entries:
        if entry.order_type not in {"receiving", "processing"}:
            continue
        order_key = (entry.order_type, entry.order_id)
        if order_key in goods_type_by_order:
            continue
        payload = entry.payload or {}
        default_label = GOODS_TYPE_LABELS.get("gv", "Готовый") if entry.order_type == "processing" else GOODS_TYPE_LABELS.get("op", "Оптовый")
        goods_type_by_order[order_key] = _goods_type_label(
            payload.get("goods_type"),
            payload.get("goods_type_label"),
            default_label,
        )
    return goods_type_by_order


def _latest_closed_placement_by_order(entries: list[OrderAuditEntry]) -> dict[tuple[str, str], OrderAuditEntry]:
    latest_by_order = {}
    blocked_orders = set()
    for entry in entries:
        order_key = (entry.order_type, entry.order_id)
        if order_key in latest_by_order or order_key in blocked_orders:
            continue
        payload = entry.payload or {}
        if payload.get("act") != "placement":
            continue
        state = (payload.get("act_state") or "closed").lower()
        if state != "closed":
            blocked_orders.add(order_key)
            continue
        latest_by_order[order_key] = entry
    return latest_by_order


def _processing_source_qty_map(
    entries: list[OrderAuditEntry],
    latest_by_order: dict[tuple[str, str], OrderAuditEntry],
    goods_type_by_order: dict[tuple[str, str], str],
) -> dict[tuple[str, str, str], int]:
    rows_by_order: dict[tuple[str, str], list[dict]] = {}
    for entry in entries:
        if entry.order_type != "processing":
            continue
        order_key = (entry.order_type, entry.order_id)
        if order_key in rows_by_order:
            continue
        payload = entry.payload or {}
        stock_rows = payload.get("stock_rows")
        if isinstance(stock_rows, list) and stock_rows:
            rows_by_order[order_key] = stock_rows
    source_map: dict[tuple[str, str, str], int] = {}
    default_goods = GOODS_TYPE_LABELS.get("op", "Оптовый")
    for order_key in latest_by_order:
        if order_key[0] != "processing":
            continue
        result_goods_value = (
            goods_type_by_order.get(order_key)
            or GOODS_TYPE_LABELS.get("gv", "Готовый")
        )
        result_goods_key = _normalize_goods_type(result_goods_value)
        for row in rows_by_order.get(order_key, []):
            sku = (row.get("article") or row.get("sku") or "").strip()
            if not sku:
                continue
            size = (row.get("size") or "").strip()
            qty = _parse_qty_value(row.get("qty")) or 0
            if qty <= 0:
                continue
            goods_value = (row.get("goods_type") or "").strip() or default_goods
            if _normalize_goods_type(goods_value) == result_goods_key:
                continue
            key = _inventory_key(sku, size, goods_value)
            source_map[key] = source_map.get(key, 0) + qty
    return source_map


def _receiving_removed_qty_map(
    latest_by_order: dict[tuple[str, str], OrderAuditEntry],
    goods_type_by_order: dict[tuple[str, str], str],
) -> dict[tuple[str, str, str], int]:
    removed_map: dict[tuple[str, str, str], int] = {}
    for order_key, entry in latest_by_order.items():
        if order_key[0] != "receiving":
            continue
        payload = entry.payload or {}
        if not payload.get("act_items_removed"):
            continue
        goods_value = goods_type_by_order.get(order_key) or GOODS_TYPE_LABELS.get("op", "Оптовый")
        expected: dict[tuple[str, str], int] = {}
        factual: dict[tuple[str, str], int] = {}
        for item in payload.get("act_items") or []:
            sku = (item.get("sku") or item.get("sku_code") or "").strip()
            if not sku:
                continue
            size = (item.get("size") or "").strip()
            key = (sku.lower(), size.lower())
            qty = _parse_qty_value(item.get("qty"))
            if qty is None:
                qty = _parse_qty_value(item.get("actual_qty")) or 0
            expected[key] = expected.get(key, 0) + max(qty, 0)
        boxes = payload.get("act_boxes") or []
        pallets = payload.get("act_pallets") or []
        for box in boxes if isinstance(boxes, list) else []:
            for item in (box or {}).get("items") or []:
                sku = (item.get("sku") or item.get("sku_code") or "").strip()
                if not sku:
                    continue
                size = (item.get("size") or "").strip()
                key = (sku.lower(), size.lower())
                qty = _parse_qty_value(item.get("qty"))
                if qty is None:
                    qty = _parse_qty_value(item.get("actual_qty")) or 0
                factual[key] = factual.get(key, 0) + max(qty, 0)
        for pallet in pallets if isinstance(pallets, list) else []:
            for item in (pallet or {}).get("items") or []:
                sku = (item.get("sku") or item.get("sku_code") or "").strip()
                if not sku:
                    continue
                size = (item.get("size") or "").strip()
                key = (sku.lower(), size.lower())
                qty = _parse_qty_value(item.get("qty"))
                if qty is None:
                    qty = _parse_qty_value(item.get("actual_qty")) or 0
                factual[key] = factual.get(key, 0) + max(qty, 0)
        for sku_key, expected_qty in expected.items():
            removed_qty = max(expected_qty - factual.get(sku_key, 0), 0)
            if removed_qty <= 0:
                continue
            key = (sku_key[0], sku_key[1], _normalize_goods_type(goods_value))
            removed_map[key] = removed_map.get(key, 0) + removed_qty
    return removed_map


def _apply_processing_source_deductions(
    rows: list[dict],
    source_map: dict[tuple[str, str, str], int],
    removed_map: dict[tuple[str, str, str], int],
) -> list[dict]:
    if not rows or not source_map:
        return rows
    remaining = {}
    for key, source_qty in source_map.items():
        remaining[key] = max(source_qty - removed_map.get(key, 0), 0)
    if not any(remaining.values()):
        return rows
    # Списываем источник обработки из более ранних остатков,
    # чтобы новые выходные строки (после обработки) не "съедались" первыми.
    work_rows = [dict(row) for row in rows]
    ordered_indexes = sorted(
        range(len(work_rows)),
        key=lambda idx: (
            work_rows[idx].get("created_at") is None,
            work_rows[idx].get("created_at"),
            str(work_rows[idx].get("order_type") or "").lower() == "processing",
            idx,
        ),
    )
    for idx in ordered_indexes:
        row = work_rows[idx]
        key = _inventory_key(row.get("sku") or "", row.get("size") or "", row.get("goods_type") or "")
        need = remaining.get(key, 0)
        qty = _parse_qty_value(row.get("qty")) or 0
        if need > 0 and qty > 0:
            delta = min(need, qty)
            qty -= delta
            remaining[key] = need - delta
            row["qty"] = qty
    adjusted_rows: list[dict] = []
    for row in work_rows:
        qty = _parse_qty_value(row.get("qty")) or 0
        if qty <= 0:
            continue
        row["qty"] = qty
        adjusted_rows.append(row)
    return adjusted_rows


def _shipping_shipped_qty_map(entries: list[OrderAuditEntry]) -> dict[tuple[str, str, str], int]:
    latest_shipping_payload: dict[str, dict] = {}
    for entry in entries:
        if entry.order_type != "shipping":
            continue
        if entry.order_id in latest_shipping_payload:
            continue
        payload = entry.payload or {}
        state = str(payload.get("shipping_state") or "").strip().lower()
        if state not in {"shipped", "partial_shipped"}:
            continue
        latest_shipping_payload[entry.order_id] = payload

    shipped_map: dict[tuple[str, str, str], int] = {}
    for payload in latest_shipping_payload.values():
        shipped_items = payload.get("shipped_items")
        if not isinstance(shipped_items, list):
            shipped_items = payload.get("items") or []
        for item in shipped_items:
            if not isinstance(item, dict):
                continue
            sku = str(item.get("sku") or item.get("sku_code") or item.get("article") or "").strip()
            if not sku:
                continue
            size = str(item.get("size") or "").strip()
            goods_type = str(item.get("goods_type") or "").strip()
            qty = _parse_qty_value(item.get("qty"))
            if qty is None:
                qty = _parse_qty_value(item.get("qty_shipped")) or 0
            if qty <= 0:
                continue
            key = _inventory_key(sku, size, goods_type)
            shipped_map[key] = shipped_map.get(key, 0) + qty
    return shipped_map


def _apply_shipping_deductions(
    rows: list[dict],
    shipped_map: dict[tuple[str, str, str], int],
) -> list[dict]:
    if not rows or not shipped_map:
        return rows
    remaining = {key: max(int(value or 0), 0) for key, value in shipped_map.items()}
    if not any(remaining.values()):
        return rows
    # Для отгрузки тоже списываем в хронологическом порядке, чтобы старые остатки уходили первыми.
    work_rows = [dict(row) for row in rows]
    ordered_indexes = sorted(
        range(len(work_rows)),
        key=lambda idx: (
            work_rows[idx].get("created_at") is None,
            work_rows[idx].get("created_at"),
            idx,
        ),
    )
    for idx in ordered_indexes:
        row = work_rows[idx]
        key = _inventory_key(row.get("sku") or "", row.get("size") or "", row.get("goods_type") or "")
        need = remaining.get(key, 0)
        qty = _parse_qty_value(row.get("qty")) or 0
        if need > 0 and qty > 0:
            delta = min(need, qty)
            qty -= delta
            remaining[key] = need - delta
            row["qty"] = qty
    adjusted_rows: list[dict] = []
    for row in work_rows:
        qty = _parse_qty_value(row.get("qty")) or 0
        if qty <= 0:
            continue
        row["qty"] = qty
        adjusted_rows.append(row)
    return adjusted_rows


def _normalize_location(pallet: dict, default_zone: str) -> dict:
    location_value = (pallet or {}).get("location")

    def normalize_zone(value: str) -> str:
        text = (value or "").strip()
        if not text:
            return ""
        if re.search(r"^obr$", text, re.IGNORECASE) or re.search(
            r"зона обработки|обработк", text, re.IGNORECASE
        ):
            return "OBR"
        if re.search(r"^pr$", text, re.IGNORECASE) or re.search(
            r"зона приемки|поле приемки", text, re.IGNORECASE
        ):
            return "PR"
        if re.search(r"^otg?$", text, re.IGNORECASE) or re.search(
            r"зона отгрузки|отгрузк", text, re.IGNORECASE
        ):
            return "OTG"
        if re.search(r"^mr$", text, re.IGNORECASE) or re.search(
            r"между ряд", text, re.IGNORECASE
        ):
            return "MR"
        if re.search(r"^os$", text, re.IGNORECASE) or re.search(
            r"основн|стеллаж|ряд|полк|секци|ярус|ячейк", text, re.IGNORECASE
        ):
            return "OS"
        return text.upper()

    zone = ""
    row = section = tier = cell = 0
    if isinstance(location_value, str):
        zone = normalize_zone(location_value)
    elif isinstance(location_value, dict):
        zone = normalize_zone(location_value.get("zone") or "")
        row = _parse_int_value(location_value.get("row") or pallet.get("row"))
        section = _parse_int_value(location_value.get("section"))
        tier = _parse_int_value(location_value.get("tier"))
        cell = _parse_int_value(location_value.get("cell"))
    if not zone:
        zone = normalize_zone((pallet or {}).get("zone") or "")
    zone = zone or default_zone
    if zone == "OBR":
        location = "OBR · Зона обработки"
    elif zone == "PR":
        location = "PR · Зона приемки"
    elif zone == "OTG":
        location = "OTG · Зона отгрузки"
    elif zone == "MR":
        location = f"MR · Между рядами · Ряд {row}" if row else "MR · Между рядами"
    elif zone == "OS":
        if row and section and tier and cell:
            location = f"OS · Ряд {row} · Секция {section} · Ярус {tier} · Ячейка {cell}"
        elif row:
            location = f"OS · Ряд {row}"
        else:
            location = "OS · Основной склад"
    else:
        location = zone
    return {
        "zone": zone,
        "row": row,
        "section": section,
        "tier": tier,
        "cell": cell,
        "location": location,
    }


def _location_label(zone: str, row: int = 0, section: int = 0, tier: int = 0, cell: int = 0) -> str:
    zone = str(zone or "").strip().upper()
    if zone == "OBR":
        return "OBR · Зона обработки"
    if zone == "PR":
        return "PR · Зона приемки"
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
    return zone or "PR"


def _ensure_pallet_location(row: dict) -> dict:
    normalized = dict(row)
    pallet_code = str(normalized.get("pallet_code") or "").strip()
    if not pallet_code:
        return normalized
    order_type = str(normalized.get("order_type") or "").strip()
    default_zone = _ZONE_DEFAULT_BY_ORDER.get(order_type, "PR")
    zone = str(normalized.get("zone") or "").strip().upper() or default_zone
    row_num = int(normalized.get("row") or 0)
    section = int(normalized.get("section") or 0)
    tier = int(normalized.get("tier") or 0)
    cell = int(normalized.get("cell") or 0)
    location = str(normalized.get("location") or "").strip()
    if not location:
        location = _location_label(zone, row=row_num, section=section, tier=tier, cell=cell)
    normalized["zone"] = zone
    normalized["row"] = row_num
    normalized["section"] = section
    normalized["tier"] = tier
    normalized["cell"] = cell
    normalized["location"] = location
    return normalized


def _placement_rows_for_entries(
    entries: list[OrderAuditEntry],
    latest_by_order: dict[tuple[str, str], OrderAuditEntry],
    goods_type_by_order: dict[tuple[str, str], str],
) -> list[dict]:
    rows: list[dict] = []
    default_zone_by_order = {
        "receiving": "PR",
        "processing": "OBR",
    }
    for entry in latest_by_order.values():
        payload = entry.payload or {}
        boxes = payload.get("act_boxes") or []
        pallets = payload.get("act_pallets") or []
        act_units = payload.get("act_units") or []
        order_key = (entry.order_type, entry.order_id)
        default_zone = default_zone_by_order.get(entry.order_type, "PR")
        goods_label = goods_type_by_order.get(order_key) or (
            GOODS_TYPE_LABELS.get("gv", "Готовый")
            if entry.order_type == "processing"
            else GOODS_TYPE_LABELS.get("op", "Оптовый")
        )
        box_to_pallet = {}
        pallet_locations = {}
        for pallet in pallets:
            if not isinstance(pallet, dict):
                continue
            pallet_code = str((pallet or {}).get("code") or "").strip()
            if not pallet_code:
                continue
            pallet_locations[pallet_code] = _normalize_location(pallet, default_zone)
            for box_code in (pallet or {}).get("boxes") or []:
                box_text = str(box_code or "").strip()
                if box_text and box_text not in box_to_pallet:
                    box_to_pallet[box_text] = pallet_code

        def append_row(item, box_code="", pallet_code="", location_parts_override=None):
            if not isinstance(item, dict):
                return
            sku = (item.get("sku") or item.get("sku_code") or "").strip()
            name = (item.get("name") or "").strip()
            size = (item.get("size") or "").strip()
            barcode = (item.get("barcode") or "").strip()
            marking_code = (item.get("marking_code") or item.get("cz_code") or item.get("code") or "").strip()
            qty = _parse_qty_value(item.get("qty"))
            if qty is None:
                qty = _parse_qty_value(item.get("actual_qty")) or 0
            if qty <= 0 or not any((sku, name, size, barcode)):
                return
            if location_parts_override:
                location_parts = location_parts_override
            elif pallet_code:
                location_parts = pallet_locations.get(pallet_code, {
                    "zone": default_zone,
                    "row": 0,
                    "section": 0,
                    "tier": 0,
                    "cell": 0,
                    "location": default_zone,
                })
            else:
                location_parts = {
                    "zone": default_zone,
                    "row": 0,
                    "section": 0,
                    "tier": 0,
                    "cell": 0,
                    "location": default_zone,
                }
            rows.append(
                _ensure_pallet_location(
                    {
                        "agency_id": entry.agency_id,
                        "order_type": entry.order_type,
                        "order_id": entry.order_id,
                        "created_at": entry.created_at,
                        "sku": sku,
                        "name": name,
                        "size": size,
                        "barcode": barcode,
                        "marking_code": marking_code,
                        "goods_type": goods_label,
                        "qty": qty,
                        "box_code": box_code,
                        "pallet_code": pallet_code,
                        "zone": location_parts.get("zone") or default_zone,
                        "row": int(location_parts.get("row") or 0),
                        "section": int(location_parts.get("section") or 0),
                        "tier": int(location_parts.get("tier") or 0),
                        "cell": int(location_parts.get("cell") or 0),
                        "location": location_parts.get("location") or default_zone,
                    }
                )
            )

        if isinstance(act_units, list) and act_units:
            for item in act_units:
                unit_box_code = str((item or {}).get("box_code") or "").strip()
                unit_pallet_code = str((item or {}).get("pallet_code") or "").strip()
                unit_location_override = None
                if unit_box_code and not unit_pallet_code:
                    unit_pallet_code = box_to_pallet.get(unit_box_code, "")
                if unit_box_code and not unit_pallet_code:
                    source_box = next(
                        (
                            box
                            for box in boxes
                            if isinstance(box, dict) and str((box or {}).get("code") or "").strip() == unit_box_code
                        ),
                        None,
                    )
                    if source_box:
                        unit_location_override = _normalize_location(source_box, default_zone)
                append_row(
                    {
                        **item,
                        "qty": 1,
                    },
                    box_code=unit_box_code,
                    pallet_code=unit_pallet_code,
                    location_parts_override=unit_location_override,
                )
            continue

        for box in boxes if isinstance(boxes, list) else []:
            box_code = str((box or {}).get("code") or "").strip()
            pallet_code = box_to_pallet.get(box_code, "")
            box_location_override = None
            if not pallet_code:
                box_location_override = _normalize_location(box, default_zone)
            for item in (box or {}).get("items") or []:
                append_row(
                    item,
                    box_code=box_code,
                    pallet_code=pallet_code,
                    location_parts_override=box_location_override,
                )

        for pallet in pallets if isinstance(pallets, list) else []:
            pallet_code = str((pallet or {}).get("code") or "").strip()
            for item in (pallet or {}).get("items") or []:
                append_row(item, box_code="", pallet_code=pallet_code)

        if not boxes and not pallets and not payload.get("act_items_removed"):
            for item in payload.get("act_items") or []:
                append_row(item, box_code="", pallet_code="")

    processing_source_map = _processing_source_qty_map(entries, latest_by_order, goods_type_by_order)
    receiving_removed_map = _receiving_removed_qty_map(latest_by_order, goods_type_by_order)
    shipping_shipped_map = _shipping_shipped_qty_map(entries)
    rows = _apply_processing_source_deductions(rows, processing_source_map, receiving_removed_map)
    return _apply_shipping_deductions(rows, shipping_shipped_map)


def _barcode_lookup_for_agency(
    agency: Agency | None,
    rows: list[dict],
) -> tuple[dict[tuple[str, str], str], dict[str, str]]:
    if not agency or not rows:
        return {}, {}
    sku_codes = {
        str(row.get("sku") or "").strip()
        for row in rows
        if str(row.get("sku") or "").strip() and not str(row.get("barcode") or "").strip()
    }
    if not sku_codes:
        return {}, {}
    by_sku_size: dict[tuple[str, str], str] = {}
    by_sku_default: dict[str, str] = {}
    qs = (
        SKUBarcode.objects.select_related("sku")
        .filter(
            sku__agency=agency,
            sku__deleted=False,
            sku__sku_code__in=sku_codes,
        )
        .order_by("sku__sku_code", "-is_primary", "id")
    )
    for barcode in qs:
        value = str(barcode.value or "").strip()
        if not value:
            continue
        sku_code = str(barcode.sku.sku_code or "").strip().lower()
        if not sku_code:
            continue
        if sku_code not in by_sku_default:
            by_sku_default[sku_code] = value
        size_key = str(barcode.size or "").strip().lower()
        if size_key and (sku_code, size_key) not in by_sku_size:
            by_sku_size[(sku_code, size_key)] = value
    return by_sku_size, by_sku_default


def _fill_missing_barcodes_from_nomenclature(
    agency: Agency | None,
    rows: list[dict],
) -> list[dict]:
    if not rows:
        return rows
    by_sku_size, by_sku_default = _barcode_lookup_for_agency(agency, rows)
    if not by_sku_size and not by_sku_default:
        return rows
    for row in rows:
        existing = str(row.get("barcode") or "").strip()
        if existing:
            continue
        sku_key = str(row.get("sku") or "").strip().lower()
        if not sku_key:
            continue
        size_key = str(row.get("size") or "").strip().lower()
        restored = by_sku_size.get((sku_key, size_key)) or by_sku_default.get(sku_key) or ""
        if restored:
            row["barcode"] = restored
    return rows


def _sku_id_lookup_for_agency(
    agency: Agency | None,
    rows: list[dict],
) -> dict[str, int]:
    if not agency or not rows:
        return {}
    sku_codes = {
        str(row.get("sku") or "").strip()
        for row in rows
        if str(row.get("sku") or "").strip()
    }
    if not sku_codes:
        return {}
    result: dict[str, int] = {}
    for sku in SKU.objects.filter(
        agency=agency,
        deleted=False,
        sku_code__in=sku_codes,
    ).only("id", "sku_code"):
        sku_code = str(sku.sku_code or "").strip().lower()
        if sku_code and sku_code not in result:
            result[sku_code] = int(sku.id)
    return result


def _reserve_state_key(row: dict) -> tuple[str, str, str]:
    return (
        str(row.get("sku") or "").strip().lower(),
        str(row.get("size") or "").strip().lower(),
        _normalize_goods_type(str(row.get("goods_type") or "").strip()),
    )


def _reserve_state_key_from_state_row(row: StockPalletState) -> tuple[str, str, str]:
    return (
        str(row.sku or "").strip().lower(),
        str(row.size or "").strip().lower(),
        _normalize_goods_type(str(row.goods_type or "").strip()),
    )


def _apply_materialized_reserves(
    rows: list[dict],
    processing_totals: dict[tuple[str, str, str], int],
    shipping_totals: dict[tuple[str, str, str], int],
) -> None:
    processing_left = {key: int(value or 0) for key, value in (processing_totals or {}).items()}
    shipping_left = {key: int(value or 0) for key, value in (shipping_totals or {}).items()}
    rows.sort(
        key=lambda row: (
            _reserve_state_key(row),
            str(row.get("order_type") or ""),
            str(row.get("order_id") or ""),
            str(row.get("box_code") or ""),
            str(row.get("pallet_code") or ""),
            str(row.get("location") or ""),
        )
    )
    for row in rows:
        qty_value = int(row.get("qty") or 0)
        key = _reserve_state_key(row)
        processing_used = min(qty_value, int(processing_left.get(key, 0)))
        remaining_qty = max(qty_value - processing_used, 0)
        shipping_used = min(remaining_qty, int(shipping_left.get(key, 0)))
        row["processing_reserved_qty"] = int(max(processing_used, 0))
        row["shipping_reserved_qty"] = int(max(shipping_used, 0))
        row["available_qty"] = int(max(qty_value - processing_used - shipping_used, 0))
        if processing_used > 0:
            processing_left[key] = max(int(processing_left.get(key, 0)) - processing_used, 0)
        if shipping_used > 0:
            shipping_left[key] = max(int(shipping_left.get(key, 0)) - shipping_used, 0)


@transaction.atomic
def rebuild_stock_snapshot_for_agency(agency: Agency | None) -> dict:
    if not agency:
        return {"agency_id": None, "rows": 0}
    entries = list(
        OrderAuditEntry.objects.filter(
            order_type__in=("receiving", "processing", "shipping"),
            agency=agency,
        ).order_by("-created_at")
    )
    goods_type_by_order = _order_goods_type_map(entries)
    latest_by_order = _latest_closed_placement_by_order(entries)
    rows = _placement_rows_for_entries(entries, latest_by_order, goods_type_by_order)
    rows = _fill_missing_barcodes_from_nomenclature(agency, rows)
    processing_reserve_map, _ = StockAvailabilityService.build_processing_reserve_maps(agency)
    shipping_reserve_map, _ = StockAvailabilityService.build_shipping_reserve_maps(agency)
    _apply_materialized_reserves(rows, processing_reserve_map, shipping_reserve_map)
    sku_id_map = _sku_id_lookup_for_agency(agency, rows)

    StockPalletState.objects.filter(agency=agency).delete()
    if not rows:
        return {"agency_id": agency.id, "rows": 0}

    bucket = defaultdict(
        lambda: {
            "qty": 0,
            "processing_reserved_qty": 0,
            "shipping_reserved_qty": 0,
            "available_qty": 0,
        }
    )
    sample = {}
    for row in rows:
        key = (
            row.get("order_type") or "",
            str(row.get("order_id") or ""),
            row.get("sku") or "",
            row.get("name") or "",
            row.get("size") or "",
            row.get("barcode") or "",
            row.get("marking_code") or "",
            row.get("goods_type") or "",
            row.get("box_code") or "",
            row.get("pallet_code") or "",
            row.get("zone") or "",
            int(row.get("row") or 0),
            int(row.get("section") or 0),
            int(row.get("tier") or 0),
            int(row.get("cell") or 0),
            row.get("location") or "",
        )
        qty = int(row.get("qty") or 0)
        if qty <= 0:
            continue
        bucket[key]["qty"] += qty
        bucket[key]["processing_reserved_qty"] += int(row.get("processing_reserved_qty") or 0)
        bucket[key]["shipping_reserved_qty"] += int(row.get("shipping_reserved_qty") or 0)
        bucket[key]["available_qty"] += int(row.get("available_qty") or 0)
        if key not in sample:
            sample[key] = row

    objects = []
    for key, totals in bucket.items():
        row = sample[key]
        objects.append(
            StockPalletState(
                agency=agency,
                sku_ref_id=sku_id_map.get(str(row.get("sku") or "").strip().lower()),
                order_type=row.get("order_type") or "receiving",
                order_id=str(row.get("order_id") or ""),
                sku=row.get("sku") or "",
                name=row.get("name") or "",
                size=row.get("size") or "",
                barcode=row.get("barcode") or "",
                marking_code=row.get("marking_code") or "",
                goods_type=row.get("goods_type") or "",
                qty=int(totals["qty"]),
                processing_reserved_qty=int(totals["processing_reserved_qty"]),
                shipping_reserved_qty=int(totals["shipping_reserved_qty"]),
                available_qty=int(totals["available_qty"]),
                box_code=row.get("box_code") or "",
                pallet_code=row.get("pallet_code") or "",
                zone=row.get("zone") or "",
                row=int(row.get("row") or 0),
                section=int(row.get("section") or 0),
                tier=int(row.get("tier") or 0),
                cell=int(row.get("cell") or 0),
                location=row.get("location") or "",
                state=StockPalletState.STATE_WAREHOUSE,
            )
        )
    StockPalletState.objects.bulk_create(objects, batch_size=2000)
    return {"agency_id": agency.id, "rows": len(objects)}


@transaction.atomic
def refresh_materialized_stock_state_for_agency(agency: Agency | None) -> dict:
    if not agency:
        return {"agency_id": None, "rows": 0}
    state_rows = list(
        StockPalletState.objects.filter(
            agency=agency,
            state=StockPalletState.STATE_WAREHOUSE,
        ).order_by("created_at", "id")
    )
    if not state_rows:
        return {"agency_id": agency.id, "rows": 0}

    rows: list[dict] = []
    for state_row in state_rows:
        rows.append(
            {
                "_obj": state_row,
                "sku": state_row.sku,
                "size": state_row.size,
                "goods_type": state_row.goods_type,
                "qty": int(state_row.qty or 0),
                "order_type": state_row.order_type,
                "order_id": state_row.order_id,
                "box_code": state_row.box_code,
                "pallet_code": state_row.pallet_code,
                "location": state_row.location,
            }
        )

    processing_reserve_map, _ = StockAvailabilityService.build_processing_reserve_maps(agency)
    shipping_reserve_map, _ = StockAvailabilityService.build_shipping_reserve_maps(agency)
    _apply_materialized_reserves(rows, processing_reserve_map, shipping_reserve_map)

    dirty_objects: list[StockPalletState] = []
    for row in rows:
        state_row = row["_obj"]
        state_row.processing_reserved_qty = int(row.get("processing_reserved_qty") or 0)
        state_row.shipping_reserved_qty = int(row.get("shipping_reserved_qty") or 0)
        state_row.available_qty = int(row.get("available_qty") or 0)
        dirty_objects.append(state_row)
    StockPalletState.objects.bulk_update(
        dirty_objects,
        ["processing_reserved_qty", "shipping_reserved_qty", "available_qty", "updated_at"],
        batch_size=2000,
    )
    return {"agency_id": agency.id, "rows": len(dirty_objects)}


@transaction.atomic
def refresh_materialized_stock_state_for_keys(
    agency: Agency | None,
    reserve_keys: set[tuple[str, str, str]] | list[tuple[str, str, str]] | tuple[tuple[str, str, str], ...] | None,
) -> dict:
    if not agency:
        return {"agency_id": None, "rows": 0}
    normalized_keys = {
        (
            str((key or ("", "", ""))[0] or "").strip().lower(),
            str((key or ("", "", ""))[1] or "").strip().lower(),
            _normalize_goods_type(str((key or ("", "", ""))[2] or "").strip()),
        )
        for key in (reserve_keys or [])
        if isinstance(key, (list, tuple)) and len(key) >= 3 and str((key or ("", "", ""))[0] or "").strip()
    }
    if not normalized_keys:
        return {"agency_id": agency.id, "rows": 0}

    sku_filter = None
    for sku_key, _size_key, _goods_key in normalized_keys:
        if not sku_key:
            continue
        condition = models.Q(sku__iexact=sku_key)
        sku_filter = condition if sku_filter is None else (sku_filter | condition)
    if sku_filter is None:
        return {"agency_id": agency.id, "rows": 0}

    state_rows = list(
        StockPalletState.objects.filter(
            agency=agency,
            state=StockPalletState.STATE_WAREHOUSE,
        )
        .filter(sku_filter)
        .order_by("created_at", "id")
    )
    if not state_rows:
        return {"agency_id": agency.id, "rows": 0}

    target_rows = [row for row in state_rows if _reserve_state_key_from_state_row(row) in normalized_keys]
    if not target_rows:
        return {"agency_id": agency.id, "rows": 0}

    rows: list[dict] = []
    for state_row in target_rows:
        rows.append(
            {
                "_obj": state_row,
                "sku": state_row.sku,
                "size": state_row.size,
                "goods_type": state_row.goods_type,
                "qty": int(state_row.qty or 0),
                "order_type": state_row.order_type,
                "order_id": state_row.order_id,
                "box_code": state_row.box_code,
                "pallet_code": state_row.pallet_code,
                "location": state_row.location,
            }
        )

    processing_reserve_map, _ = StockAvailabilityService.build_processing_reserve_maps(agency)
    shipping_reserve_map, _ = StockAvailabilityService.build_shipping_reserve_maps(agency)
    _apply_materialized_reserves(rows, processing_reserve_map, shipping_reserve_map)

    dirty_objects: list[StockPalletState] = []
    for row in rows:
        state_row = row["_obj"]
        state_row.processing_reserved_qty = int(row.get("processing_reserved_qty") or 0)
        state_row.shipping_reserved_qty = int(row.get("shipping_reserved_qty") or 0)
        state_row.available_qty = int(row.get("available_qty") or 0)
        dirty_objects.append(state_row)
    StockPalletState.objects.bulk_update(
        dirty_objects,
        ["processing_reserved_qty", "shipping_reserved_qty", "available_qty", "updated_at"],
        batch_size=2000,
    )
    return {"agency_id": agency.id, "rows": len(dirty_objects)}


def rebuild_stock_snapshot() -> dict:
    agencies = Agency.objects.all().order_by("id")
    total = 0
    by_agency = {}
    for agency in agencies:
        result = rebuild_stock_snapshot_for_agency(agency)
        by_agency[str(agency.id)] = result.get("rows", 0)
        total += result.get("rows", 0)
    return {"total_rows": total, "agencies": by_agency}
