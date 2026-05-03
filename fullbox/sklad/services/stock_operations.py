from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from django.db import transaction
from django.db.models import Q

from sku.models import Agency, SKU, SKUBarcode
from sklad.models import StockPalletState
from sklad.stock_state import refresh_materialized_stock_state_for_keys

from .stock_availability import StockAvailabilityService


def _as_int(value) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _normalize_zone(zone: str | None) -> str:
    return str(zone or "").strip().upper() or "PR"


def _location_dict(*, zone: str, row: int = 0, section: int = 0, tier: int = 0, cell: int = 0) -> dict:
    normalized_zone = _normalize_zone(zone)
    return {
        "zone": normalized_zone,
        "row": row if row > 0 else "",
        "section": section if section > 0 else "",
        "tier": tier if tier > 0 else "",
        "cell": cell if cell > 0 else "",
    }


def _location_from_row(row: StockPalletState) -> dict:
    return _location_dict(
        zone=row.zone,
        row=int(row.row or 0),
        section=int(row.section or 0),
        tier=int(row.tier or 0),
        cell=int(row.cell or 0),
    )


def _location_from_payload(location: dict | None) -> dict:
    location = location or {}
    return _location_dict(
        zone=location.get("zone") or "PR",
        row=_as_int(location.get("row")),
        section=_as_int(location.get("section")),
        tier=_as_int(location.get("tier")),
        cell=_as_int(location.get("cell")),
    )


def _container_location(container: dict | None, default_zone: str) -> dict:
    container = container or {}
    return _location_dict(
        zone=container.get("zone") or container.get("location", {}).get("zone") or default_zone,
        row=_as_int(container.get("row") or container.get("location", {}).get("row")),
        section=_as_int(container.get("section") or container.get("location", {}).get("section")),
        tier=_as_int(container.get("tier") or container.get("location", {}).get("tier")),
        cell=_as_int(container.get("cell") or container.get("location", {}).get("cell")),
    )


def _location_label(location: dict) -> str:
    zone = _normalize_zone(location.get("zone"))
    row = _as_int(location.get("row"))
    section = _as_int(location.get("section"))
    tier = _as_int(location.get("tier"))
    cell = _as_int(location.get("cell"))
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
    return zone


def _parse_qty(raw) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _goods_type_label(order_type: str, payload: dict | None) -> str:
    payload = payload or {}
    raw_label = str(payload.get("goods_type_label") or "").strip()
    if raw_label:
        return raw_label
    raw_type = str(payload.get("goods_type") or "").strip().lower()
    label_map = {
        "op": "Оптовый",
        "gv": "Готовый",
        "br": "Брак",
        "vz": "Возврат",
        "rh": "Расходный",
        "no": "Не обработанный",
    }
    if raw_type in label_map:
        return label_map[raw_type]
    return "Готовый" if str(order_type or "").strip().lower() == "processing" else "Оптовый"


def _serialize_item_from_row(row: StockPalletState) -> dict:
    item = {
        "sku": str(row.sku or "").strip(),
        "sku_code": str(row.sku or "").strip(),
        "name": str(row.name or "").strip(),
        "size": str(row.size or "").strip(),
        "barcode": str(row.barcode or "").strip(),
        "qty": int(row.qty or 0),
    }
    marking_code = str(row.marking_code or "").strip()
    if marking_code:
        item["marking_code"] = marking_code
    goods_type = str(row.goods_type or "").strip()
    if goods_type:
        item["goods_type"] = goods_type
    return item


def _serialize_unit_from_row(row: StockPalletState) -> dict:
    return {
        "sku": str(row.sku or "").strip(),
        "sku_code": str(row.sku or "").strip(),
        "name": str(row.name or "").strip(),
        "size": str(row.size or "").strip(),
        "barcode": str(row.barcode or "").strip(),
        "marking_code": str(row.marking_code or "").strip(),
        "box_code": str(row.box_code or "").strip(),
        "pallet_code": str(row.pallet_code or "").strip(),
        "qty": int(row.qty or 0),
        "goods_type": str(row.goods_type or "").strip(),
        "location": _location_from_row(row),
        "location_label": str(row.location or "").strip() or _location_label(_location_from_row(row)),
    }


def _template_key_from_row(row: StockPalletState) -> tuple[str, str, str, str, str]:
    return (
        str(row.sku or "").strip().lower(),
        str(row.size or "").strip().lower(),
        str(row.barcode or "").strip(),
        str(row.marking_code or "").strip(),
        StockAvailabilityService.normalize_goods_type(row.goods_type),
    )


def _template_key_from_item(item: dict) -> tuple[str, str, str, str, str]:
    return (
        str(item.get("sku") or item.get("sku_code") or "").strip().lower(),
        str(item.get("size") or "").strip().lower(),
        str(item.get("barcode") or "").strip(),
        str(item.get("marking_code") or "").strip(),
        StockAvailabilityService.normalize_goods_type(item.get("goods_type")),
    )


def _reserve_refresh_key(sku: str | None, size: str | None, goods_type: str | None) -> tuple[str, str, str]:
    return (
        str(sku or "").strip().lower(),
        str(size or "").strip().lower(),
        StockAvailabilityService.normalize_goods_type(goods_type),
    )


@dataclass
class StockPalletTree:
    agency: Agency
    pallet_code: str
    payload: dict
    source_rows: list[StockPalletState]
    context_order_type: str
    context_order_id: str

    @property
    def synthetic_entry(self):
        return SimpleNamespace(
            order_id=self.context_order_id,
            order_type=self.context_order_type,
            agency=self.agency,
            agency_id=int(self.agency.id or 0),
            payload=self.payload,
        )


class OperationalStockService:
    DEFAULT_ZONE_BY_ORDER = {
        "receiving": "PR",
        "processing": "OBR",
    }

    @staticmethod
    def _warehouse_rows_queryset(
        *,
        agency_id: int | None = None,
        pallet_code: str | None = None,
        box_code: str | None = None,
        include_boxless: bool = True,
    ):
        rows_qs = StockPalletState.objects.filter(
            state=StockPalletState.STATE_WAREHOUSE,
        ).select_related("agency", "sku_ref")
        if agency_id:
            rows_qs = rows_qs.filter(agency_id=int(agency_id))
        if pallet_code is not None:
            rows_qs = rows_qs.filter(pallet_code=str(pallet_code or "").strip())
        if box_code is not None:
            rows_qs = rows_qs.filter(box_code=str(box_code or "").strip())
        if not include_boxless:
            rows_qs = rows_qs.exclude(box_code="")
        return rows_qs.order_by("pallet_code", "box_code", "marking_code", "created_at", "id")

    @staticmethod
    def clear_order_placement(
        agency: Agency | None,
        order_type: str,
        order_id: str | int | None,
        *,
        refresh_keys: bool = True,
    ) -> int:
        if not agency or not order_id:
            return 0
        existing_rows = list(
            StockPalletState.objects.filter(
                agency=agency,
                order_type=str(order_type or "").strip() or "receiving",
                order_id=str(order_id),
                state=StockPalletState.STATE_WAREHOUSE,
            ).only("sku", "size", "goods_type")
        )
        deleted, _ = StockPalletState.objects.filter(
            agency=agency,
            order_type=str(order_type or "").strip() or "receiving",
            order_id=str(order_id),
            state=StockPalletState.STATE_WAREHOUSE,
        ).delete()
        if refresh_keys and existing_rows:
            refresh_materialized_stock_state_for_keys(
                agency,
                {
                    _reserve_refresh_key(row.sku, row.size, row.goods_type)
                    for row in existing_rows
                    if str(row.sku or "").strip()
                },
            )
        return int(deleted or 0)

    @staticmethod
    @transaction.atomic
    def replace_order_placement(
        agency: Agency | None,
        order_type: str,
        order_id: str | int | None,
        payload: dict | None,
    ) -> int:
        if not agency or not order_id:
            return 0

        order_type = str(order_type or "").strip() or "receiving"
        order_id = str(order_id)
        payload = payload or {}
        default_zone = OperationalStockService.DEFAULT_ZONE_BY_ORDER.get(order_type, "PR")
        goods_type = _goods_type_label(order_type, payload)
        existing_rows = list(
            StockPalletState.objects.filter(
                agency=agency,
                order_type=order_type,
                order_id=order_id,
                state=StockPalletState.STATE_WAREHOUSE,
            ).only("sku", "size", "goods_type")
        )
        boxes = payload.get("act_boxes") or []
        pallets = payload.get("act_pallets") or []
        act_units = payload.get("act_units") or []
        act_items_removed = bool(payload.get("act_items_removed"))

        box_to_pallet: dict[str, str] = {}
        pallet_locations: dict[str, dict] = {}
        for pallet in pallets if isinstance(pallets, list) else []:
            if not isinstance(pallet, dict):
                continue
            pallet_code = str(pallet.get("code") or "").strip()
            if not pallet_code:
                continue
            pallet_locations[pallet_code] = _container_location(pallet, default_zone)
            for box_code in pallet.get("boxes") or []:
                box_text = str(box_code or "").strip()
                if box_text and box_text not in box_to_pallet:
                    box_to_pallet[box_text] = pallet_code

        prepared_rows: list[dict] = []

        def append_row(
            item: dict | None,
            *,
            box_code: str = "",
            pallet_code: str = "",
            location_override: dict | None = None,
        ) -> None:
            if not isinstance(item, dict):
                return
            sku = str(item.get("sku") or item.get("sku_code") or "").strip()
            name = str(item.get("name") or "").strip()
            size = str(item.get("size") or "").strip()
            barcode = str(item.get("barcode") or "").strip()
            marking_code = str(item.get("marking_code") or item.get("cz_code") or item.get("code") or "").strip()
            qty = _parse_qty(item.get("qty") if item.get("qty") is not None else item.get("actual_qty"))
            if qty <= 0 or not any((sku, name, size, barcode, marking_code)):
                return
            location = location_override or (
                pallet_locations.get(pallet_code) if pallet_code else _container_location({}, default_zone)
            )
            prepared_rows.append(
                {
                    "sku": sku,
                    "name": name,
                    "size": size,
                    "barcode": barcode,
                    "marking_code": marking_code,
                    "goods_type": goods_type,
                    "qty": qty,
                    "box_code": str(box_code or "").strip(),
                    "pallet_code": str(pallet_code or "").strip(),
                    "location": location,
                }
            )

        if isinstance(act_units, list) and act_units:
            for unit in act_units:
                if not isinstance(unit, dict):
                    continue
                unit_box_code = str(unit.get("box_code") or "").strip()
                unit_pallet_code = str(unit.get("pallet_code") or "").strip()
                location_override = None
                if unit_box_code and not unit_pallet_code:
                    unit_pallet_code = box_to_pallet.get(unit_box_code, "")
                if unit_box_code and not unit_pallet_code:
                    source_box = next(
                        (
                            box
                            for box in boxes
                            if isinstance(box, dict) and str(box.get("code") or "").strip() == unit_box_code
                        ),
                        None,
                    )
                    if source_box:
                        location_override = _container_location(source_box, default_zone)
                append_row(
                    {
                        **unit,
                        "qty": 1,
                    },
                    box_code=unit_box_code,
                    pallet_code=unit_pallet_code,
                    location_override=location_override,
                )
        else:
            for box in boxes if isinstance(boxes, list) else []:
                if not isinstance(box, dict):
                    continue
                box_code = str(box.get("code") or "").strip()
                pallet_code = box_to_pallet.get(box_code, "")
                box_location = None if pallet_code else _container_location(box, default_zone)
                for item in box.get("items") or []:
                    append_row(
                        item,
                        box_code=box_code,
                        pallet_code=pallet_code,
                        location_override=box_location,
                    )
            for pallet in pallets if isinstance(pallets, list) else []:
                if not isinstance(pallet, dict):
                    continue
                pallet_code = str(pallet.get("code") or "").strip()
                for item in pallet.get("items") or []:
                    append_row(item, pallet_code=pallet_code)
            if not prepared_rows and not act_items_removed:
                for item in payload.get("act_items") or []:
                    append_row(item)

        sku_codes = {
            str(row.get("sku") or "").strip()
            for row in prepared_rows
            if str(row.get("sku") or "").strip()
        }
        sku_ref_by_code: dict[str, int] = {}
        barcode_by_sku_size: dict[tuple[str, str], str] = {}
        barcode_by_sku_default: dict[str, str] = {}
        if sku_codes:
            for sku in SKU.objects.filter(agency=agency, deleted=False, sku_code__in=sku_codes).only("id", "sku_code"):
                sku_code = str(sku.sku_code or "").strip().lower()
                if sku_code and sku_code not in sku_ref_by_code:
                    sku_ref_by_code[sku_code] = int(sku.id)
            barcode_qs = (
                SKUBarcode.objects.select_related("sku")
                .filter(
                    sku__agency=agency,
                    sku__deleted=False,
                    sku__sku_code__in=sku_codes,
                )
                .order_by("sku__sku_code", "-is_primary", "id")
            )
            for barcode in barcode_qs:
                value = str(barcode.value or "").strip()
                if not value:
                    continue
                sku_code = str(barcode.sku.sku_code or "").strip().lower()
                if not sku_code:
                    continue
                if sku_code not in barcode_by_sku_default:
                    barcode_by_sku_default[sku_code] = value
                size_key = str(barcode.size or "").strip().lower()
                if size_key and (sku_code, size_key) not in barcode_by_sku_size:
                    barcode_by_sku_size[(sku_code, size_key)] = value

        new_rows: list[StockPalletState] = []
        for row in prepared_rows:
            sku = str(row.get("sku") or "").strip()
            sku_key = sku.lower()
            size = str(row.get("size") or "").strip()
            barcode = str(row.get("barcode") or "").strip()
            if not barcode and sku_key:
                barcode = barcode_by_sku_size.get((sku_key, size.lower())) or barcode_by_sku_default.get(sku_key, "")
            location = _location_from_payload(row.get("location"))
            new_rows.append(
                StockPalletState(
                    agency=agency,
                    sku_ref_id=sku_ref_by_code.get(sku_key),
                    order_type=order_type,
                    order_id=order_id,
                    sku=sku,
                    name=str(row.get("name") or "").strip(),
                    size=size,
                    barcode=barcode,
                    marking_code=str(row.get("marking_code") or "").strip(),
                    goods_type=str(row.get("goods_type") or goods_type or "").strip(),
                    qty=int(row.get("qty") or 0),
                    processing_reserved_qty=0,
                    shipping_reserved_qty=0,
                    available_qty=int(row.get("qty") or 0),
                    box_code=str(row.get("box_code") or "").strip(),
                    pallet_code=str(row.get("pallet_code") or "").strip(),
                    zone=str(location.get("zone") or ""),
                    row=_as_int(location.get("row")),
                    section=_as_int(location.get("section")),
                    tier=_as_int(location.get("tier")),
                    cell=_as_int(location.get("cell")),
                    location=_location_label(location),
                    state=StockPalletState.STATE_WAREHOUSE,
                )
            )

        affected_keys = {
            _reserve_refresh_key(row.sku, row.size, row.goods_type)
            for row in existing_rows
            if str(row.sku or "").strip()
        }
        affected_keys.update(
            {
                _reserve_refresh_key(row.get("sku"), row.get("size"), row.get("goods_type"))
                for row in prepared_rows
                if str(row.get("sku") or "").strip()
            }
        )

        OperationalStockService.clear_order_placement(agency, order_type, order_id, refresh_keys=False)
        if new_rows:
            StockPalletState.objects.bulk_create(new_rows, batch_size=1000)
        if affected_keys:
            refresh_materialized_stock_state_for_keys(agency, affected_keys)
        return len(new_rows)

    @staticmethod
    def get_pallet_tree(
        pallet_code: str,
        *,
        agency_id: int | None = None,
    ) -> StockPalletTree | None:
        target_code = str(pallet_code or "").strip()
        if not target_code:
            return None
        source_rows = list(
            OperationalStockService._warehouse_rows_queryset(
                agency_id=agency_id,
                pallet_code=target_code,
            )
        )
        if not source_rows:
            return None

        agency = source_rows[0].agency
        context_order_type = str(source_rows[0].order_type or "").strip() or "receiving"
        context_order_id = str(source_rows[0].order_id or "").strip()
        pallet_location = _location_from_row(source_rows[0])

        boxes_map: dict[str, dict] = {}
        pallet_items: list[dict] = []
        for row in source_rows:
            item = _serialize_item_from_row(row)
            box_code = str(row.box_code or "").strip()
            if box_code:
                box = boxes_map.setdefault(
                    box_code,
                    {
                        "code": box_code,
                        "items": [],
                    },
                )
                box["items"].append(item)
            else:
                pallet_items.append(item)

        act_boxes = [boxes_map[key] for key in sorted(boxes_map.keys())]
        payload = {
            "act": "placement",
            "act_state": "closed",
            "act_pallets": [
                {
                    "code": target_code,
                    "boxes": [box["code"] for box in act_boxes],
                    "items": pallet_items,
                    "location": pallet_location,
                }
            ],
            "act_boxes": act_boxes,
        }
        return StockPalletTree(
            agency=agency,
            pallet_code=target_code,
            payload=payload,
            source_rows=source_rows,
            context_order_type=context_order_type,
            context_order_id=context_order_id,
        )

    @staticmethod
    def get_pallet_boxes(
        pallet_code: str,
        *,
        agency_id: int | None = None,
    ) -> list[dict]:
        target_code = str(pallet_code or "").strip()
        if not target_code:
            return []
        source_rows = list(
            OperationalStockService._warehouse_rows_queryset(
                agency_id=agency_id,
                pallet_code=target_code,
                include_boxless=False,
            )
        )
        if not source_rows:
            return []

        boxes_map: dict[str, dict] = {}
        for row in source_rows:
            box_code = str(row.box_code or "").strip()
            if not box_code:
                continue
            box = boxes_map.setdefault(
                box_code,
                {
                    "code": box_code,
                    "pallet_code": target_code,
                    "location": _location_from_row(row),
                    "location_label": str(row.location or "").strip() or _location_label(_location_from_row(row)),
                    "qty": 0,
                    "barcode_qty": {},
                    "items": [],
                    "marked_units": [],
                },
            )
            item = _serialize_item_from_row(row)
            box["items"].append(item)
            item_qty = int(row.qty or 0)
            if item_qty > 0:
                box["qty"] += item_qty
            barcode = str(row.barcode or "").strip()
            if barcode and item_qty > 0:
                box["barcode_qty"][barcode] = int(box["barcode_qty"].get(barcode, 0)) + item_qty
            if str(row.marking_code or "").strip():
                box["marked_units"].append(_serialize_unit_from_row(row))

        return [boxes_map[key] for key in sorted(boxes_map.keys())]

    @staticmethod
    def get_box_items(
        box_code: str,
        *,
        agency_id: int | None = None,
        pallet_code: str | None = None,
    ) -> list[dict]:
        target_code = str(box_code or "").strip()
        if not target_code:
            return []
        rows = list(
            OperationalStockService._warehouse_rows_queryset(
                agency_id=agency_id,
                pallet_code=pallet_code,
                box_code=target_code,
            )
        )
        return [_serialize_item_from_row(row) for row in rows]

    @staticmethod
    def get_marked_units(
        *,
        agency_id: int | None = None,
        pallet_code: str | None = None,
        box_code: str | None = None,
        sku: str | None = None,
        barcode: str | None = None,
    ) -> list[dict]:
        rows_qs = OperationalStockService._warehouse_rows_queryset(
            agency_id=agency_id,
            pallet_code=pallet_code,
            box_code=box_code,
        ).exclude(marking_code="")
        if sku:
            rows_qs = rows_qs.filter(sku=str(sku or "").strip())
        if barcode:
            rows_qs = rows_qs.filter(barcode=str(barcode or "").strip())
        return [_serialize_unit_from_row(row) for row in rows_qs]

    @staticmethod
    @transaction.atomic
    def replace_pallet_tree(tree: StockPalletTree, payload: dict) -> int:
        if not tree.source_rows:
            return 0

        agency = tree.agency
        affected_keys = {
            _reserve_refresh_key(row.sku, row.size, row.goods_type)
            for row in tree.source_rows
            if str(row.sku or "").strip()
        }
        source_ids = [int(row.id) for row in tree.source_rows if int(row.id or 0) > 0]
        template_pools: dict[tuple[str, str, str, str, str], list[dict]] = {}
        sku_ref_by_code: dict[str, int] = {}
        for row in tree.source_rows:
            key = _template_key_from_row(row)
            template_pools.setdefault(key, []).append(
                {
                    "remaining_qty": int(row.qty or 0),
                    "sku_ref_id": int(row.sku_ref_id or 0),
                    "order_type": str(row.order_type or "").strip() or tree.context_order_type,
                    "order_id": str(row.order_id or "").strip() or tree.context_order_id,
                    "name": str(row.name or "").strip(),
                    "goods_type": str(row.goods_type or "").strip(),
                }
            )
            sku_code = str(row.sku or "").strip().lower()
            if sku_code and row.sku_ref_id and sku_code not in sku_ref_by_code:
                sku_ref_by_code[sku_code] = int(row.sku_ref_id)

        payload = payload or {}
        act_pallets = payload.get("act_pallets") or []
        act_boxes = payload.get("act_boxes") or []
        linked_box_codes: set[str] = set()
        pallet_by_code: dict[str, dict] = {}
        for pallet in act_pallets:
            if not isinstance(pallet, dict):
                continue
            code = str(pallet.get("code") or "").strip()
            if not code:
                continue
            pallet_by_code[code] = pallet
            for box_code in pallet.get("boxes") or []:
                text = str(box_code or "").strip()
                if text:
                    linked_box_codes.add(text)

        def allocate_templates(item: dict, qty: int) -> list[dict]:
            remaining = int(qty or 0)
            key = _template_key_from_item(item)
            templates = template_pools.get(key, [])
            allocations: list[dict] = []
            for template in templates:
                if remaining <= 0:
                    break
                available = int(template.get("remaining_qty") or 0)
                if available <= 0:
                    continue
                take_qty = min(available, remaining)
                template["remaining_qty"] = available - take_qty
                remaining -= take_qty
                allocations.append(
                    {
                        "qty": take_qty,
                        "sku_ref_id": int(template.get("sku_ref_id") or 0),
                        "order_type": str(template.get("order_type") or "").strip() or tree.context_order_type,
                        "order_id": str(template.get("order_id") or "").strip() or tree.context_order_id,
                        "name": str(template.get("name") or "").strip(),
                        "goods_type": str(template.get("goods_type") or "").strip(),
                    }
                )
            if remaining > 0:
                sku_code = str(item.get("sku") or item.get("sku_code") or "").strip().lower()
                allocations.append(
                    {
                        "qty": remaining,
                        "sku_ref_id": int(sku_ref_by_code.get(sku_code, 0) or 0),
                        "order_type": tree.context_order_type,
                        "order_id": tree.context_order_id,
                        "name": str(item.get("name") or "").strip(),
                        "goods_type": str(item.get("goods_type") or "").strip(),
                    }
                )
            return allocations

        new_rows: list[StockPalletState] = []

        def append_items(
            items: list,
            *,
            pallet_code: str,
            box_code: str,
            location: dict,
        ) -> None:
            location_parts = _location_from_payload(location)
            location_label = _location_label(location_parts)
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                qty = _as_int(item.get("qty"))
                sku = str(item.get("sku") or item.get("sku_code") or "").strip()
                if not sku or qty <= 0:
                    continue
                size = str(item.get("size") or "").strip()
                barcode = str(item.get("barcode") or "").strip()
                marking_code = str(item.get("marking_code") or "").strip()
                goods_type = str(item.get("goods_type") or "").strip()
                allocations = allocate_templates(item, qty)
                for allocation in allocations:
                    row_qty = int(allocation.get("qty") or 0)
                    if row_qty <= 0:
                        continue
                    new_rows.append(
                        StockPalletState(
                            agency=agency,
                            sku_ref_id=int(allocation.get("sku_ref_id") or 0) or None,
                            order_type=str(allocation.get("order_type") or "").strip() or tree.context_order_type,
                            order_id=str(allocation.get("order_id") or "").strip() or tree.context_order_id,
                            sku=sku,
                            name=str(allocation.get("name") or item.get("name") or "").strip(),
                            size=size,
                            barcode=barcode,
                            marking_code=marking_code,
                            goods_type=str(allocation.get("goods_type") or goods_type or "").strip(),
                            qty=row_qty,
                            processing_reserved_qty=0,
                            shipping_reserved_qty=0,
                            available_qty=0,
                            box_code=box_code,
                            pallet_code=pallet_code,
                            zone=str(location_parts.get("zone") or ""),
                            row=_as_int(location_parts.get("row")),
                            section=_as_int(location_parts.get("section")),
                            tier=_as_int(location_parts.get("tier")),
                            cell=_as_int(location_parts.get("cell")),
                            location=location_label,
                            state=StockPalletState.STATE_WAREHOUSE,
                        )
                    )
                    affected_keys.add(_reserve_refresh_key(sku, size, str(allocation.get("goods_type") or goods_type or "").strip()))

        for pallet in act_pallets:
            if not isinstance(pallet, dict):
                continue
            current_pallet_code = str(pallet.get("code") or "").strip()
            if not current_pallet_code:
                continue
            pallet_location = pallet.get("location") or {}
            append_items(
                pallet.get("items") or [],
                pallet_code=current_pallet_code,
                box_code="",
                location=pallet_location,
            )

        for box in act_boxes:
            if not isinstance(box, dict):
                continue
            current_box_code = str(box.get("code") or "").strip()
            if not current_box_code:
                continue
            if current_box_code in linked_box_codes:
                owning_pallet_code = next(
                    (
                        pallet_code
                        for pallet_code, pallet in pallet_by_code.items()
                        if current_box_code in {str(code or "").strip() for code in pallet.get("boxes") or []}
                    ),
                    tree.pallet_code,
                )
                location = (pallet_by_code.get(owning_pallet_code) or {}).get("location") or {}
                append_items(
                    box.get("items") or [],
                    pallet_code=owning_pallet_code,
                    box_code=current_box_code,
                    location=location,
                )
            else:
                append_items(
                    box.get("items") or [],
                    pallet_code="",
                    box_code=current_box_code,
                    location=box.get("location") or {},
                )

        if source_ids:
            StockPalletState.objects.filter(id__in=source_ids).delete()

        if new_rows:
            missing_sku_codes = {
                str(row.sku or "").strip()
                for row in new_rows
                if str(row.sku or "").strip() and not row.sku_ref_id
            }
            if missing_sku_codes:
                for sku in SKU.objects.filter(agency=agency, deleted=False, sku_code__in=missing_sku_codes).only("id", "sku_code"):
                    sku_ref_by_code.setdefault(str(sku.sku_code or "").strip().lower(), int(sku.id))
                for row in new_rows:
                    if row.sku_ref_id:
                        continue
                    row.sku_ref_id = sku_ref_by_code.get(str(row.sku or "").strip().lower())
            StockPalletState.objects.bulk_create(new_rows, batch_size=1000)
        if affected_keys:
            refresh_materialized_stock_state_for_keys(agency, affected_keys)
        return len(new_rows)


__all__ = ["OperationalStockService", "StockPalletTree"]
