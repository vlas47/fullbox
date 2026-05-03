from __future__ import annotations

from django.db import models
from shipping.models import ShippingOrder, ShippingReserve
from sku.models import Agency, SKU

from sklad.models import (
    InventoryState,
    StockPalletState,
    WarehouseContainer,
    WarehouseReserve,
    WarehouseStockSnapshot,
)
from sklad.services.warehouse_stock_rows import snapshot_stock_rows

GOODS_TYPE_ALIASES = {
    "op": "оптовый",
    "gv": "готовый",
    "no": "не обработанный",
    "br": "брак",
    "vz": "возврат",
    "rh": "расходный",
    "необработанный": "не обработанный",
}
_PALLET_CONTAINER_TYPES = {
    WarehouseContainer.TYPE_PALLET,
    WarehouseContainer.TYPE_MIXED_PALLET,
}


def normalize_goods_type(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if not text or text == "-":
        return ""
    return GOODS_TYPE_ALIASES.get(text, text)


def build_processing_reserve_maps(
    agency: Agency | None,
    exclude_processing_order_id: str | None = None,
) -> tuple[dict[tuple[str, str, str], int], dict[tuple[str, str], int]]:
    if not agency:
        return {}, {}
    warehouse_reserves = WarehouseReserve.objects.filter(
        agency=agency,
        reserve_type=WarehouseReserve.TYPE_PROCESSING,
    ).exclude(
        status__in=[
            WarehouseReserve.STATUS_RELEASED,
            WarehouseReserve.STATUS_CANCELED,
        ]
    )
    if exclude_processing_order_id:
        warehouse_reserves = warehouse_reserves.exclude(
            context_type="processing",
            context_id=str(exclude_processing_order_id),
        )

    reserve_map: dict[tuple[str, str, str], int] = {}
    reserve_any: dict[tuple[str, str], int] = {}
    warehouse_seen = False
    for entry in warehouse_reserves:
        sku = (entry.sku_code or "").strip()
        if not sku:
            continue
        outstanding_qty = max(int(entry.qty_reserved or 0) - int(entry.qty_satisfied or 0), 0)
        if outstanding_qty <= 0:
            continue
        warehouse_seen = True
        size = (entry.size or "").strip()
        goods_key = normalize_goods_type(entry.goods_type)
        key = (sku.lower(), size.lower(), goods_key)
        reserve_map[key] = reserve_map.get(key, 0) + outstanding_qty
        any_key = (sku.lower(), size.lower())
        reserve_any[any_key] = reserve_any.get(any_key, 0) + outstanding_qty
    if warehouse_seen:
        return reserve_map, reserve_any

    reserves = InventoryState.objects.filter(agency=agency, state=InventoryState.STATE_PROCESSING)
    if exclude_processing_order_id:
        reserves = reserves.exclude(order_type="processing", order_id=str(exclude_processing_order_id))
    for entry in reserves:
        sku = (entry.sku or "").strip()
        if not sku:
            continue
        size = (entry.size or "").strip()
        goods_key = normalize_goods_type(entry.goods_type)
        qty = int(entry.qty or 0)
        if qty <= 0:
            continue
        key = (sku.lower(), size.lower(), goods_key)
        reserve_map[key] = reserve_map.get(key, 0) + qty
        any_key = (sku.lower(), size.lower())
        reserve_any[any_key] = reserve_any.get(any_key, 0) + qty
    return reserve_map, reserve_any


def build_shipping_reserve_maps(
    agency: Agency | None,
) -> tuple[dict[tuple[str, str, str], int], dict[tuple[str, str], int]]:
    if not agency:
        return {}, {}
    reserves = ShippingReserve.objects.filter(agency=agency).exclude(
        order__status__in=[
            ShippingOrder.STATUS_SHIPPED,
            ShippingOrder.STATUS_PARTIAL,
            ShippingOrder.STATUS_CANCELED,
        ]
    )
    reserve_map: dict[tuple[str, str, str], int] = {}
    reserve_any: dict[tuple[str, str], int] = {}
    for entry in reserves:
        sku = (entry.sku_code or "").strip()
        if not sku:
            continue
        size = (entry.size or "").strip()
        goods_key = normalize_goods_type(entry.goods_type)
        qty = int(entry.qty or 0)
        if qty <= 0:
            continue
        key = (sku.lower(), size.lower(), goods_key)
        reserve_map[key] = reserve_map.get(key, 0) + qty
        any_key = (sku.lower(), size.lower())
        reserve_any[any_key] = reserve_any.get(any_key, 0) + qty
    return reserve_map, reserve_any


def reserved_processing_qty(
    reserve_map: dict[tuple[str, str, str], int],
    reserve_any: dict[tuple[str, str], int],
    sku: str,
    size: str,
    goods_type: str | None = None,
) -> int:
    sku_key = (sku or "").strip().lower()
    if not sku_key:
        return 0
    size_key = (size or "").strip().lower()
    goods_key = normalize_goods_type(goods_type)
    if not goods_key:
        return reserve_any.get((sku_key, size_key), 0)
    return reserve_map.get((sku_key, size_key, goods_key), 0) + reserve_map.get(
        (sku_key, size_key, ""),
        0,
    )


def _barcode_value_for_sku(sku: SKU | None, size: str | None) -> str:
    if not sku:
        return "-"
    barcodes = list(getattr(sku, "barcodes", []).all())
    if not barcodes:
        return "-"
    size_value = (size or "").strip()
    if size_value:
        for barcode in barcodes:
            if (barcode.size or "").strip() == size_value:
                return barcode.value
    primary = next((barcode for barcode in barcodes if barcode.is_primary), None)
    return primary.value if primary else barcodes[0].value


def _normalize_photo_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith(("http://", "https://", "/")):
        return url
    return f"/{url}"


def _sku_photo_url(sku: SKU | None) -> str:
    if not sku:
        return ""
    url = (sku.img or "").strip()
    if url:
        return _normalize_photo_url(url)
    photos = list(getattr(sku, "photos", []).all())
    if photos:
        return _normalize_photo_url((photos[0].url or "").strip())
    return ""


def inventory_items_for_agency(
    agency: Agency | None,
    exclude_processing_order_id: str | None = None,
    exclude_shipping_order_id: str | None = None,
) -> list[dict]:
    if not agency:
        return []

    warehouse_rows = snapshot_stock_rows(agency=agency)
    if warehouse_rows:
        stock_rows = warehouse_rows
        use_materialized_availability = (
            exclude_processing_order_id is None
            and exclude_shipping_order_id is None
            and any(
                int(row.get("processing_reserved_qty") or 0) > 0
                or int(row.get("shipping_reserved_qty") or 0) > 0
                or int(row.get("available_qty") or 0) > 0
                for row in stock_rows
            )
        )
    else:
        stock_rows = list(
            StockPalletState.objects.filter(
                agency=agency,
                state=StockPalletState.STATE_WAREHOUSE,
            ).select_related("sku_ref")
        )
        use_materialized_availability = (
            exclude_processing_order_id is None
            and exclude_shipping_order_id is None
            and any(
                int(row.processing_reserved_qty or 0) > 0
                or int(row.shipping_reserved_qty or 0) > 0
                or int(row.available_qty or 0) > 0
                for row in stock_rows
            )
        )

    totals: dict[tuple[str, str, str, str], dict] = {}
    for row in stock_rows:
        if isinstance(row, dict):
            sku = (row.get("sku") or "").strip()
            name = (row.get("name") or "").strip()
            size = (row.get("size") or "").strip()
            goods_label = (row.get("goods_type") or "").strip() or "-"
            qty = int(row.get("available_qty") if use_materialized_availability else row.get("qty") or 0)
            sku_ref_id = int(row.get("sku_ref_id") or 0)
        else:
            sku = (row.sku or "").strip()
            name = (row.name or "").strip()
            size = (row.size or "").strip()
            goods_label = (row.goods_type or "").strip() or "-"
            qty = int(row.available_qty if use_materialized_availability else row.qty or 0)
            sku_ref_id = int(row.sku_ref_id or 0)
        if qty <= 0 or not any((sku, name, size)):
            continue
        key = (sku, name, size, goods_label)
        existing = totals.setdefault(
            key,
            {
                "sku": sku,
                "sku_id": sku_ref_id,
                "name": name,
                "size": size,
                "qty": 0,
                "goods_type": goods_label,
            },
        )
        if not existing.get("sku_id") and sku_ref_id:
            existing["sku_id"] = sku_ref_id
        existing["qty"] += qty

    sku_ids = {int(item["sku_id"]) for item in totals.values() if int(item.get("sku_id") or 0) > 0}
    sku_codes = {item["sku"] for item in totals.values() if item.get("sku")}
    sku_map_by_id: dict[int, SKU] = {}
    sku_map_by_code: dict[str, SKU] = {}
    sku_qs = SKU.objects.filter(agency=agency, deleted=False)
    if sku_ids and sku_codes:
        sku_qs = sku_qs.filter(models.Q(id__in=sku_ids) | models.Q(sku_code__in=sku_codes))
    elif sku_ids:
        sku_qs = sku_qs.filter(id__in=sku_ids)
    elif sku_codes:
        sku_qs = sku_qs.filter(sku_code__in=sku_codes)
    else:
        sku_qs = SKU.objects.none()
    for sku in sku_qs.prefetch_related("barcodes", "photos"):
        sku_map_by_id[int(sku.id)] = sku
        sku_map_by_code[sku.sku_code] = sku

    if exclude_processing_order_id is None and exclude_shipping_order_id is None and use_materialized_availability:
        reserve_map, reserve_any = {}, {}
        shipping_reserve_map, shipping_reserve_any = {}, {}
    else:
        reserve_map, reserve_any = build_processing_reserve_maps(
            agency,
            exclude_processing_order_id=exclude_processing_order_id,
        )
        shipping_reserve_map, shipping_reserve_any = build_shipping_reserve_maps(agency)
        if exclude_shipping_order_id:
            current_order_reserves = ShippingReserve.objects.filter(
                agency=agency,
                order__number=str(exclude_shipping_order_id),
            )
            for reserve in current_order_reserves:
                sku_key = (reserve.sku_code or "").strip().lower()
                if not sku_key:
                    continue
                size_key = (reserve.size or "").strip().lower()
                goods_key = normalize_goods_type(reserve.goods_type)
                qty_value = int(reserve.qty or 0)
                key = (sku_key, size_key, goods_key)
                shipping_reserve_map[key] = max(int(shipping_reserve_map.get(key, 0)) - qty_value, 0)
                any_key = (sku_key, size_key)
                shipping_reserve_any[any_key] = max(int(shipping_reserve_any.get(any_key, 0)) - qty_value, 0)
    items: list[dict] = []
    for item in totals.values():
        sku_obj = sku_map_by_id.get(int(item.get("sku_id") or 0)) or sku_map_by_code.get(item.get("sku"))
        processing_reserved_qty = reserved_processing_qty(
            reserve_map,
            reserve_any,
            item.get("sku") or "",
            item.get("size") or "",
            item.get("goods_type") or "",
        )
        shipping_reserved_qty = reserved_processing_qty(
            shipping_reserve_map,
            shipping_reserve_any,
            item.get("sku") or "",
            item.get("size") or "",
            item.get("goods_type") or "",
        )
        reserved_qty = processing_reserved_qty + shipping_reserved_qty
        available_qty = max((item.get("qty") or 0) - reserved_qty, 0)
        if available_qty <= 0:
            continue
        items.append(
            {
                "sku": item.get("sku") or "",
                "name": item.get("name") or "",
                "size": item.get("size") or "",
                "barcode": _barcode_value_for_sku(sku_obj, item.get("size")) if sku_obj else "-",
                "qty": available_qty,
                "goods_type": item.get("goods_type") or "-",
                "photo": _sku_photo_url(sku_obj),
            }
        )
    items.sort(
        key=lambda row: (
            row.get("name") or "",
            row.get("goods_type") or "",
            row.get("size") or "",
            row.get("sku") or "",
        )
    )
    return items


def _snapshot_pallet_code(snapshot: WarehouseStockSnapshot) -> str:
    if snapshot.parent_container_id and snapshot.parent_container is not None:
        return str(snapshot.parent_container.container_code or "").strip()
    if snapshot.container is not None and snapshot.container.container_type in _PALLET_CONTAINER_TYPES:
        return str(snapshot.container.container_code or "").strip()
    return str(snapshot.container_code or "").strip()


def _occupied_os_snapshot_rows(
    *,
    exclude_order_type: str | None = None,
    exclude_order_id: str | None = None,
    exclude_pallet_code: str | None = None,
) -> list[dict]:
    qs = (
        WarehouseStockSnapshot.objects.filter(is_archived=False, zone_code__iexact="OS")
        .select_related("location", "container", "parent_container")
        .order_by("id")
    )
    snapshots = list(qs)
    if not snapshots:
        return []
    normalized_pallet_code = str(exclude_pallet_code or "").strip().lower()
    rows: list[dict] = []
    for snapshot in snapshots:
        if exclude_order_type and exclude_order_id:
            if (
                str(snapshot.source_context_type or "").strip() == str(exclude_order_type or "").strip()
                and str(snapshot.source_context_id or "").strip() == str(exclude_order_id or "").strip()
            ):
                continue
        elif exclude_order_id and str(snapshot.source_context_id or "").strip() == str(exclude_order_id or "").strip():
            continue
        location = snapshot.location
        if location is None:
            continue
        pallet_code = _snapshot_pallet_code(snapshot)
        if normalized_pallet_code and pallet_code.lower() == normalized_pallet_code:
            continue
        rows.append(
            {
                "row": int(location.row_no or 0),
                "section": int(location.section_no or 0),
                "tier": int(location.tier_no or 0),
                "cell": int(location.cell_no or 0),
                "agency_id": int(snapshot.agency_id or 0),
            }
        )
    return rows


def occupied_os_cell_keys(
    exclude_order_type: str | None = None,
    exclude_order_id: str | None = None,
    exclude_pallet_code: str | None = None,
) -> set[tuple[int, int, int, int]]:
    rows = _occupied_os_snapshot_rows(
        exclude_order_type=exclude_order_type,
        exclude_order_id=exclude_order_id,
        exclude_pallet_code=exclude_pallet_code,
    )
    if not rows:
        rows = _occupied_os_rows_queryset(
            exclude_order_type=exclude_order_type,
            exclude_order_id=exclude_order_id,
            exclude_pallet_code=exclude_pallet_code,
        )
    keys: set[tuple[int, int, int, int]] = set()
    for row in rows:
        row_no = int((row.get("row") if isinstance(row, dict) else row.row) or 0)
        section = int((row.get("section") if isinstance(row, dict) else row.section) or 0)
        tier = int((row.get("tier") if isinstance(row, dict) else row.tier) or 0)
        cell = int((row.get("cell") if isinstance(row, dict) else row.cell) or 0)
        if not all((row_no, section, tier, cell)):
            continue
        keys.add((row_no, section, tier, cell))
    return keys


def occupied_os_cells(
    exclude_order_type: str | None = None,
    exclude_order_id: str | None = None,
    exclude_pallet_code: str | None = None,
    include_agency: bool = False,
) -> list[dict]:
    rows = _occupied_os_snapshot_rows(
        exclude_order_type=exclude_order_type,
        exclude_order_id=exclude_order_id,
        exclude_pallet_code=exclude_pallet_code,
    )
    if not rows:
        rows = _occupied_os_rows_queryset(
            exclude_order_type=exclude_order_type,
            exclude_order_id=exclude_order_id,
            exclude_pallet_code=exclude_pallet_code,
        )
    grouped: dict[tuple[int, int, int, int], set[int]] = {}
    for row in rows:
        row_no = int((row.get("row") if isinstance(row, dict) else row.row) or 0)
        section = int((row.get("section") if isinstance(row, dict) else row.section) or 0)
        tier = int((row.get("tier") if isinstance(row, dict) else row.tier) or 0)
        cell = int((row.get("cell") if isinstance(row, dict) else row.cell) or 0)
        if not all((row_no, section, tier, cell)):
            continue
        agency_id = int((row.get("agency_id") if isinstance(row, dict) else row.agency_id) or 0)
        grouped.setdefault((row_no, section, tier, cell), set()).add(agency_id)
    result: list[dict] = []
    for row_no, section, tier, cell in sorted(grouped.keys()):
        item = {
            "row": row_no,
            "section": section,
            "tier": tier,
            "cell": cell,
        }
        if include_agency:
            agency_ids = sorted(agency_id for agency_id in grouped[(row_no, section, tier, cell)] if agency_id > 0)
            item["agency_ids"] = agency_ids
            item["agency_id"] = agency_ids[0] if len(agency_ids) == 1 else 0
        result.append(item)
    return result


def occupied_os_section_agencies(
    exclude_order_type: str | None = None,
    exclude_order_id: str | None = None,
    exclude_pallet_code: str | None = None,
) -> dict[tuple[int, int], set[int]]:
    rows = _occupied_os_snapshot_rows(
        exclude_order_type=exclude_order_type,
        exclude_order_id=exclude_order_id,
        exclude_pallet_code=exclude_pallet_code,
    )
    if not rows:
        rows = _occupied_os_rows_queryset(
            exclude_order_type=exclude_order_type,
            exclude_order_id=exclude_order_id,
            exclude_pallet_code=exclude_pallet_code,
        )
    sections: dict[tuple[int, int], set[int]] = {}
    for row in rows:
        row_no = int((row.get("row") if isinstance(row, dict) else row.row) or 0)
        section = int((row.get("section") if isinstance(row, dict) else row.section) or 0)
        agency_id = int((row.get("agency_id") if isinstance(row, dict) else row.agency_id) or 0)
        if not row_no or not section or agency_id <= 0:
            continue
        sections.setdefault((row_no, section), set()).add(agency_id)
    return sections


def suggest_os_cell_for_agency(
    *,
    agency_id: int | None,
    row_sections: dict[int, int] | dict[str, int],
    tiers: int,
    cells_per_tier: int,
    occupied_keys: set[tuple[int, int, int, int]] | None = None,
    used_cell_keys: set[tuple[int, int, int, int]] | None = None,
    section_agencies: dict[tuple[int, int], set[int]] | None = None,
) -> dict | None:
    occupied_keys = set(occupied_keys or set())
    used_cell_keys = set(used_cell_keys or set())
    section_agencies = {
        (int(row), int(section)): {int(value) for value in values if int(value) > 0}
        for (row, section), values in (section_agencies or {}).items()
    }
    current_agency_id = int(agency_id or 0)
    buckets: dict[str, list[tuple[int, int]]] = {"same": [], "empty": [], "other": []}
    for row in sorted((int(value) for value in row_sections.keys()), key=int):
        for section in range(1, int(row_sections.get(row) or row_sections.get(str(row)) or 0) + 1):
            agencies = section_agencies.get((row, section), set())
            if current_agency_id > 0 and current_agency_id in agencies:
                bucket = "same"
            elif not agencies:
                bucket = "empty"
            else:
                bucket = "other"
            buckets[bucket].append((row, section))
    for bucket in ("same", "empty", "other"):
        for row, section in buckets[bucket]:
            for tier in range(1, int(tiers or 0) + 1):
                for cell in range(1, int(cells_per_tier or 0) + 1):
                    key = (int(row), int(section), int(tier), int(cell))
                    if key in occupied_keys or key in used_cell_keys:
                        continue
                    return {
                        "zone": "OS",
                        "row": int(row),
                        "section": int(section),
                        "tier": int(tier),
                        "cell": int(cell),
                    }
    return None


def _occupied_os_rows_queryset(
    *,
    exclude_order_type: str | None = None,
    exclude_order_id: str | None = None,
    exclude_pallet_code: str | None = None,
):
    rows = StockPalletState.objects.filter(
        state=StockPalletState.STATE_WAREHOUSE,
        zone__iexact="OS",
    )
    if exclude_order_type and exclude_order_id:
        rows = rows.exclude(order_type=exclude_order_type, order_id=str(exclude_order_id))
    elif exclude_order_id:
        rows = rows.exclude(order_id=str(exclude_order_id))
    if exclude_pallet_code:
        rows = rows.exclude(pallet_code=str(exclude_pallet_code).strip())
    return rows


class StockAvailabilityService:
    normalize_goods_type = staticmethod(normalize_goods_type)
    build_processing_reserve_maps = staticmethod(build_processing_reserve_maps)
    build_shipping_reserve_maps = staticmethod(build_shipping_reserve_maps)
    reserved_processing_qty = staticmethod(reserved_processing_qty)
    inventory_items_for_agency = staticmethod(inventory_items_for_agency)
    occupied_os_cell_keys = staticmethod(occupied_os_cell_keys)
    occupied_os_cells = staticmethod(occupied_os_cells)
    occupied_os_section_agencies = staticmethod(occupied_os_section_agencies)
    suggest_os_cell_for_agency = staticmethod(suggest_os_cell_for_agency)
