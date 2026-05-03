from __future__ import annotations

import re
from datetime import datetime

from django.db.models import Max, Q, Sum
from django.http import HttpResponseForbidden
from django.utils import timezone

from audit.models import OrderAuditEntry
from employees.access import get_request_role, is_staff_role
from shipping.models import ShippingOrder, ShippingReserve
from sku.models import Agency, SKU
from sklad.models import InventoryState, StockPalletState, WarehouseReserve, WarehouseStockSnapshot
from sklad.services.stock_availability import StockAvailabilityService
from sklad.services.warehouse_transitions import WarehouseStateCode
from sklad.services.warehouse_stock_rows import legacy_stock_rows, snapshot_stock_rows

_IP_PREFIX_RE = re.compile(r"\bиндивидуальный предприниматель\b", re.IGNORECASE)
_JOURNAL_HIDDEN_WAREHOUSE_STATES = {
    WarehouseStateCode.IN_PROCESSING_ZONE.value,
    WarehouseStateCode.PROCESSING_IN_PROGRESS.value,
}


def _shorten_ip_name(name: str) -> str:
    if not name:
        return "-"
    normalized = _IP_PREFIX_RE.sub("ИП", name)
    return " ".join(normalized.split())


def _client_agency_for_request(request):
    if not request.user.is_authenticated:
        return None
    return Agency.objects.filter(portal_user=request.user).first()


def _parse_qty_value(raw: object | None) -> int | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _reserve_key(
    agency_id: int | None,
    sku: str | None,
    size: str | None,
    goods_type: str | None,
) -> tuple[int, str, str, str]:
    return (
        int(agency_id or 0),
        (sku or "").strip().lower(),
        (size or "").strip().lower(),
        StockAvailabilityService.normalize_goods_type(goods_type or ""),
    )


def _processing_snapshot_order_id(snapshot: WarehouseStockSnapshot) -> str:
    operation = getattr(snapshot, "active_operation", None)
    if operation and str(getattr(operation, "context_type", "") or "").strip() == "processing":
        order_id = str(getattr(operation, "context_id", "") or "").strip()
        if order_id:
            return order_id
    reserve = (
        WarehouseReserve.objects.filter(
            agency=snapshot.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            sku_code=snapshot.sku_code,
            size=snapshot.size,
            barcode=snapshot.barcode,
            goods_type=snapshot.goods_type,
        )
        .exclude(
            status__in=[
                WarehouseReserve.STATUS_RELEASED,
                WarehouseReserve.STATUS_CANCELED,
            ]
        )
        .order_by("-updated_at", "-id")
        .first()
    )
    if reserve is None:
        return ""
    return str(reserve.context_id or "").strip()


def _processing_reserve_groups(agency: Agency | None) -> tuple[list[dict], dict[tuple[int, str, str, str], int]]:
    warehouse_qs = WarehouseReserve.objects.filter(
        reserve_type=WarehouseReserve.TYPE_PROCESSING,
    ).exclude(
        status__in=[
            WarehouseReserve.STATUS_RELEASED,
            WarehouseReserve.STATUS_CANCELED,
        ]
    )
    if agency:
        warehouse_qs = warehouse_qs.filter(agency=agency)
    warehouse_groups = list(
        warehouse_qs
        .values("agency_id", "context_id", "sku_code", "size", "goods_type")
        .annotate(
            reserved_qty=Sum("qty_reserved"),
            satisfied_qty=Sum("qty_satisfied"),
            updated_at_max=Max("updated_at"),
        )
    )
    warehouse_order_keys = {
        (int(item.get("agency_id") or 0), str(item.get("context_id") or "").strip())
        for item in warehouse_groups
        if str(item.get("context_id") or "").strip()
    }
    in_progress_by_order_key: dict[tuple[int, str, tuple[int, str, str, str]], int] = {}
    in_progress_snapshots = (
        WarehouseStockSnapshot.objects.filter(
            warehouse_state_code=WarehouseStateCode.PROCESSING_IN_PROGRESS.value,
            processing_reserved_qty__gt=0,
            is_archived=False,
        )
        .select_related("agency", "active_operation")
        .order_by("id")
    )
    if agency:
        in_progress_snapshots = in_progress_snapshots.filter(agency=agency)
    for snapshot in in_progress_snapshots:
        order_id = _processing_snapshot_order_id(snapshot)
        if not order_id:
            continue
        order_key = (int(snapshot.agency_id or 0), order_id)
        reserve_key = _reserve_key(
            snapshot.agency_id,
            snapshot.sku_code,
            snapshot.size,
            snapshot.goods_type,
        )
        qty_value = int(snapshot.processing_reserved_qty or snapshot.qty or 0)
        if qty_value <= 0:
            continue
        combined_key = (order_key[0], order_key[1], reserve_key)
        in_progress_by_order_key[combined_key] = in_progress_by_order_key.get(combined_key, 0) + qty_value
    totals: dict[tuple[int, str, str, str], int] = {}
    normalized_groups: list[dict] = []
    for item in warehouse_groups:
        reserved_qty = int(item.get("reserved_qty") or 0)
        satisfied_qty = int(item.get("satisfied_qty") or 0)
        reserve_key = _reserve_key(
            item.get("agency_id"),
            item.get("sku_code"),
            item.get("size"),
            item.get("goods_type"),
        )
        order_key = (
            int(item.get("agency_id") or 0),
            str(item.get("context_id") or "").strip(),
            reserve_key,
        )
        waiting_qty = max(reserved_qty - satisfied_qty - int(in_progress_by_order_key.get(order_key, 0)), 0)
        if waiting_qty <= 0:
            continue
        normalized_item = {
            "agency_id": item.get("agency_id"),
            "sku": item.get("sku_code"),
            "size": item.get("size"),
            "goods_type": item.get("goods_type"),
            "reserved_qty": waiting_qty,
            "updated_at_max": item.get("updated_at_max"),
        }
        normalized_groups.append(normalized_item)
        totals[reserve_key] = totals.get(reserve_key, 0) + waiting_qty

    qs = InventoryState.objects.filter(state=InventoryState.STATE_PROCESSING)
    if agency:
        qs = qs.filter(agency=agency)
    groups = list(
        qs
        .values("agency_id", "order_id", "sku", "size", "goods_type")
        .annotate(reserved_qty=Sum("qty"), updated_at_max=Max("updated_at"))
    )
    for item in groups:
        order_key = (
            int(item.get("agency_id") or 0),
            str(item.get("order_id") or "").strip(),
        )
        if order_key in warehouse_order_keys:
            continue
        qty = int(item.get("reserved_qty") or 0)
        if qty <= 0:
            continue
        normalized_item = {
            "agency_id": item.get("agency_id"),
            "sku": item.get("sku"),
            "size": item.get("size"),
            "goods_type": item.get("goods_type"),
            "reserved_qty": qty,
            "updated_at_max": item.get("updated_at_max"),
        }
        normalized_groups.append(normalized_item)
        key = _reserve_key(
            normalized_item.get("agency_id"),
            normalized_item.get("sku"),
            normalized_item.get("size"),
            normalized_item.get("goods_type"),
        )
        totals[key] = totals.get(key, 0) + qty
    return normalized_groups, totals


def _shipping_reserve_groups(agency: Agency | None) -> tuple[list[dict], dict[tuple[int, str, str, str], int]]:
    qs = ShippingReserve.objects.all()
    if agency:
        qs = qs.filter(agency=agency)
    groups = list(
        qs
        .exclude(
            order__status__in=[
                ShippingOrder.STATUS_SHIPPED,
                ShippingOrder.STATUS_PARTIAL,
                ShippingOrder.STATUS_CANCELED,
            ]
        )
        .values("agency_id", "sku_code", "size", "goods_type")
        .annotate(reserved_qty=Sum("qty"), updated_at_max=Max("created_at"))
    )
    totals: dict[tuple[int, str, str, str], int] = {}
    for item in groups:
        qty = int(item.get("reserved_qty") or 0)
        if qty <= 0:
            continue
        key = _reserve_key(item.get("agency_id"), item.get("sku_code"), item.get("size"), item.get("goods_type"))
        totals[key] = totals.get(key, 0) + qty
    return groups, totals


def _processing_order_is_active(payload: dict | None) -> bool:
    if not isinstance(payload, dict):
        return False
    status_value = str(payload.get("status") or payload.get("submit_action") or "").strip().lower()
    status_label = str(payload.get("status_label") or "").strip().lower()
    if status_value in {"done", "completed", "canceled", "cancelled"}:
        return False
    if any(token in status_label for token in ("выполн", "заверш", "закрыт", "отмен")):
        return False
    return True


def _processing_in_progress_groups(agency: Agency | None) -> tuple[list[dict], dict[tuple[int, str, str, str], int]]:
    grouped: dict[tuple[int, str, str, str], dict] = {}
    totals: dict[tuple[int, str, str, str], int] = {}
    warehouse_covered_orders: set[tuple[int, str]] = set()
    warehouse_snapshots = (
        WarehouseStockSnapshot.objects.filter(
            warehouse_state_code=WarehouseStateCode.PROCESSING_IN_PROGRESS.value,
            is_archived=False,
        )
        .select_related("agency", "active_operation")
        .order_by("id")
    )
    if agency:
        warehouse_snapshots = warehouse_snapshots.filter(agency=agency)
    for snapshot in warehouse_snapshots:
        order_id = _processing_snapshot_order_id(snapshot)
        if not order_id:
            continue
        warehouse_covered_orders.add((int(snapshot.agency_id or 0), order_id))
        qty_value = int(snapshot.processing_reserved_qty or snapshot.qty or 0)
        if qty_value <= 0:
            continue
        key = _reserve_key(
            snapshot.agency_id,
            snapshot.sku_code,
            snapshot.size,
            snapshot.goods_type,
        )
        totals[key] = totals.get(key, 0) + qty_value
        row = grouped.get(key)
        if row is None:
            row = {
                "agency_id": int(snapshot.agency_id or 0),
                "sku": snapshot.sku_code,
                "size": snapshot.size or "-",
                "goods_type": snapshot.goods_type or "-",
                "in_progress_qty": 0,
                "updated_at_max": snapshot.updated_at,
            }
            grouped[key] = row
        row["in_progress_qty"] += qty_value
        if snapshot.updated_at and snapshot.updated_at > row["updated_at_max"]:
            row["updated_at_max"] = snapshot.updated_at

    entries = OrderAuditEntry.objects.filter(order_type="processing").select_related("agency").order_by("order_id", "created_at")
    if agency:
        entries = entries.filter(agency=agency)

    latest_by_order: dict[tuple[int, str], OrderAuditEntry] = {}
    for entry in entries:
        agency_id = int(entry.agency_id or 0)
        order_id = str(entry.order_id or "").strip()
        if not order_id:
            continue
        latest_by_order[(agency_id, order_id)] = entry

    remaining_by_order_key: dict[tuple[int, str, tuple[int, str, str, str]], int] = {}
    remaining_groups = (
        InventoryState.objects.filter(state=InventoryState.STATE_PROCESSING)
        .values("agency_id", "order_id", "sku", "size", "goods_type")
        .annotate(remaining_qty=Sum("qty"))
    )
    if agency:
        remaining_groups = remaining_groups.filter(agency=agency)
    for item in remaining_groups:
        reserve_key = _reserve_key(
            item.get("agency_id"),
            item.get("sku"),
            item.get("size"),
            item.get("goods_type"),
        )
        order_key = (
            int(item.get("agency_id") or 0),
            str(item.get("order_id") or "").strip(),
            reserve_key,
        )
        remaining_by_order_key[order_key] = int(item.get("remaining_qty") or 0)

    for (agency_id, order_id), latest in latest_by_order.items():
        if (agency_id, order_id) in warehouse_covered_orders:
            continue
        payload = dict(latest.payload or {}) if isinstance(latest.payload, dict) else {}
        if not _processing_order_is_active(payload):
            continue
        base_totals: dict[tuple[int, str, str, str], int] = {}
        for row in payload.get("stock_rows") or []:
            if not isinstance(row, dict):
                continue
            sku_value = (row.get("article") or row.get("sku") or "").strip()
            qty_value = _parse_qty_value(row.get("qty")) or 0
            if not sku_value or qty_value <= 0:
                continue
            key = _reserve_key(
                agency_id,
                sku_value,
                (row.get("size") or "").strip(),
                row.get("goods_type") or "",
            )
            base_totals[key] = base_totals.get(key, 0) + qty_value
        for key, base_qty in base_totals.items():
            remaining_qty = int(remaining_by_order_key.get((agency_id, order_id, key), 0))
            in_progress_qty = max(int(base_qty or 0) - remaining_qty, 0)
            if in_progress_qty <= 0:
                continue
            totals[key] = totals.get(key, 0) + in_progress_qty
            row = grouped.get(key)
            if row is None:
                row = {
                    "agency_id": agency_id,
                    "sku": key[1],
                    "size": key[2] or "-",
                    "goods_type": key[3] or "-",
                    "in_progress_qty": 0,
                    "updated_at_max": latest.created_at,
                }
                grouped[key] = row
            row["in_progress_qty"] += in_progress_qty
            if latest.created_at and latest.created_at > row["updated_at_max"]:
                row["updated_at_max"] = latest.created_at
    return list(grouped.values()), totals


def _apply_available_qty(
    rows: list[dict],
    processing_reserve_totals: dict[tuple[int, str, str, str], int],
    shipping_reserve_totals: dict[tuple[int, str, str, str], int],
) -> None:
    remaining_processing = {key: int(value or 0) for key, value in processing_reserve_totals.items()}
    remaining_shipping = {key: int(value or 0) for key, value in shipping_reserve_totals.items()}
    indexed_rows = list(enumerate(rows))
    indexed_rows.sort(
        key=lambda item: (
            _reserve_key(
                item[1].get("agency_id"),
                item[1].get("sku"),
                item[1].get("size"),
                item[1].get("goods_type"),
            ),
            item[1].get("created_at") or timezone.localtime(),
            item[1].get("order_id") or "",
            item[1].get("box_code") or "",
            item[1].get("pallet_code") or "",
            item[0],
        )
    )
    for _, row in indexed_rows:
        qty_value = int(row.get("qty") or 0)
        key = _reserve_key(row.get("agency_id"), row.get("sku"), row.get("size"), row.get("goods_type"))
        processing_left = int(remaining_processing.get(key, 0))
        processing_used = min(qty_value, processing_left)
        qty_after_processing = max(qty_value - processing_used, 0)
        shipping_left = int(remaining_shipping.get(key, 0))
        shipping_used = min(qty_after_processing, shipping_left)
        row["processing_reserved_qty"] = int(processing_used)
        row["shipping_reserved_qty"] = int(shipping_used)
        row["available_qty"] = int(max(qty_value - processing_used - shipping_used, 0))
        if processing_used > 0:
            remaining_processing[key] = max(processing_left - processing_used, 0)
        if shipping_used > 0:
            remaining_shipping[key] = max(shipping_left - shipping_used, 0)


def build_inventory_journal_page(*, request):
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    role = get_request_role(request)
    staff_view = request.user.is_staff or is_staff_role(role)
    client_agency = None
    if staff_view:
        client_id = request.GET.get("client") or request.GET.get("agency")
        if client_id:
            client_agency = Agency.objects.filter(pk=client_id).first()
    else:
        client_agency = _client_agency_for_request(request)
        if not client_agency:
            return HttpResponseForbidden("Доступ запрещен")
    rows = []
    order_type_codes = {
        "receiving": "ПРМ",
        "processing": "ОБР",
    }
    order_type_titles = {
        "receiving": "Приемка",
        "processing": "Обработка",
    }
    warehouse_rows = snapshot_stock_rows(agency=client_agency) if client_agency else snapshot_stock_rows()
    use_warehouse_rows = bool(warehouse_rows)
    if use_warehouse_rows:
        storage_rows = [
            item
            for item in warehouse_rows
            if str(item.get("warehouse_state_code") or "").strip() not in _JOURNAL_HIDDEN_WAREHOUSE_STATES
        ]
        use_materialized_availability = any(
            int(item.get("processing_reserved_qty") or 0) > 0
            or int(item.get("shipping_reserved_qty") or 0) > 0
            or int(item.get("available_qty") or 0) > 0
            for item in storage_rows
        )
    else:
        qs = StockPalletState.objects.filter(state=StockPalletState.STATE_WAREHOUSE).select_related("agency")
        if client_agency:
            qs = qs.filter(agency=client_agency)
        storage_rows = legacy_stock_rows(qs)
        use_materialized_availability = any(
            int(item.get("processing_reserved_qty") or 0) > 0
            or int(item.get("shipping_reserved_qty") or 0) > 0
            or int(item.get("available_qty") or 0) > 0
            for item in storage_rows
        )
    processing_reserve_groups, processing_reserve_totals = _processing_reserve_groups(client_agency)
    processing_in_progress_groups, processing_in_progress_totals = _processing_in_progress_groups(client_agency)
    shipping_reserve_groups, shipping_reserve_totals = _shipping_reserve_groups(client_agency)
    for item in storage_rows:
        agency_obj = item.get("agency")
        client_name = _shorten_ip_name(
            (agency_obj.agn_name if agency_obj else "")
            or (agency_obj.fio_agn if agency_obj else "")
            or str(agency_obj or "")
        )
        rows.append(
            {
                "created_at": item.get("updated_at"),
                "order_id": item.get("order_id"),
                "order_type": item.get("order_type"),
                "order_type_code": order_type_codes.get(item.get("order_type"), (item.get("order_type") or "").upper()),
                "order_type_label": order_type_titles.get(item.get("order_type"), item.get("order_type")),
                "client_label": client_name or "-",
                "agency_id": int(item.get("agency_id") or 0),
                "sku": (item.get("sku") or "-").strip() or "-",
                "name": (item.get("name") or "-").strip() or "-",
                "size": (item.get("size") or "-").strip() or "-",
                "goods_type": (item.get("goods_type") or "-").strip() or "-",
                "qty": int(item.get("qty") or 0),
                "processing_reserved_qty": int(item.get("processing_reserved_qty") or 0) if use_materialized_availability else 0,
                "processing_in_progress_qty": 0,
                "shipping_reserved_qty": int(item.get("shipping_reserved_qty") or 0) if use_materialized_availability else 0,
                "available_qty": int(item.get("available_qty") or 0) if use_materialized_availability else int(item.get("qty") or 0),
                "box_code": (item.get("box_code") or "-").strip() or "-",
                "pallet_code": (item.get("pallet_code") or "-").strip() or "-",
                "location": (item.get("location") or item.get("zone") or "-").strip() or "-",
            }
        )

    if not staff_view:
        grouped = {}
        for row in rows:
            key = (row.get("sku"), row.get("name"), row.get("size"), row.get("goods_type"))
            qty_value = _parse_qty_value(row.get("qty")) or 0
            existing = grouped.get(key)
            if existing:
                existing["qty"] += qty_value
                existing["processing_reserved_qty"] += int(row.get("processing_reserved_qty") or 0)
                existing["processing_in_progress_qty"] += int(row.get("processing_in_progress_qty") or 0)
                existing["shipping_reserved_qty"] += int(row.get("shipping_reserved_qty") or 0)
                existing["available_qty"] += int(row.get("available_qty") or 0)
                if row.get("created_at") and row["created_at"] > existing.get("created_at"):
                    existing["created_at"] = row["created_at"]
            else:
                item = dict(row)
                item["qty"] = qty_value
                grouped[key] = item
        rows = list(grouped.values())
        for row in rows:
            key = _reserve_key(row.get("agency_id"), row.get("sku"), row.get("size"), row.get("goods_type"))
            row["processing_in_progress_qty"] = processing_in_progress_totals.get(key, 0)
        if not use_materialized_availability:
            for row in rows:
                key = _reserve_key(row.get("agency_id"), row.get("sku"), row.get("size"), row.get("goods_type"))
                row["processing_reserved_qty"] = processing_reserve_totals.get(key, 0)
                row["shipping_reserved_qty"] = shipping_reserve_totals.get(key, 0)
                row["available_qty"] = max(
                    int(row.get("qty") or 0)
                    - int(row.get("processing_reserved_qty") or 0)
                    - int(row.get("shipping_reserved_qty") or 0),
                    0,
                )
        present_keys = {
            _reserve_key(row.get("agency_id"), row.get("sku"), row.get("size"), row.get("goods_type"))
            for row in rows
        }
        name_by_sku = {
            (row.get("sku") or "").strip().lower(): (row.get("name") or "-").strip() or "-"
            for row in rows
            if (row.get("sku") or "").strip()
        }
        missing_skus = {
            sku_value
            for item in list(processing_reserve_groups) + list(processing_in_progress_groups) + list(shipping_reserve_groups)
            for sku_value in [
                (item.get("sku") or item.get("sku_code") or "").strip()
            ]
            if sku_value and sku_value.lower() not in name_by_sku
        }
        if missing_skus:
            for sku_obj in SKU.objects.filter(
                agency=client_agency,
                deleted=False,
                sku_code__in=missing_skus,
            ):
                sku_key = (sku_obj.sku_code or "").strip().lower()
                if sku_key and sku_key not in name_by_sku:
                    name_by_sku[sku_key] = (sku_obj.name or "-").strip() or "-"
        client_name = _shorten_ip_name(
            (client_agency.agn_name if client_agency else "")
            or (client_agency.fio_agn if client_agency else "")
            or str(client_agency or "")
        )
        reserve_only_rows: dict[tuple[int, str, str, str], dict] = {}

        def _upsert_reserve_only_row(item: dict, *, source: str) -> None:
            sku_value = (item.get("sku") or item.get("sku_code") or "").strip()
            if not sku_value:
                return
            size_value = (item.get("size") or "").strip()
            goods_raw = (item.get("goods_type") or "").strip()
            key = _reserve_key(
                int(client_agency.id or 0) if client_agency else 0,
                sku_value,
                size_value,
                goods_raw,
            )
            qty_value = int(item.get("reserved_qty") or 0)
            if qty_value <= 0:
                return
            row = reserve_only_rows.get(key)
            if row is None:
                row = {
                    "created_at": item.get("updated_at_max") or timezone.localtime(),
                    "order_id": "-",
                    "order_type": "processing",
                    "order_type_code": order_type_codes.get("processing", "ОБР"),
                    "order_type_label": order_type_titles.get("processing", "Обработка"),
                    "client_label": client_name or "-",
                    "agency_id": int(client_agency.id or 0) if client_agency else 0,
                    "sku": sku_value or "-",
                    "name": name_by_sku.get(sku_value.lower(), "-"),
                    "size": size_value or "-",
                    "goods_type": goods_raw or "-",
                    "qty": 0,
                    "processing_reserved_qty": 0,
                    "processing_in_progress_qty": 0,
                    "shipping_reserved_qty": 0,
                    "available_qty": 0,
                    "box_code": "-",
                    "pallet_code": "-",
                    "location": "-",
                }
                reserve_only_rows[key] = row
            if item.get("updated_at_max") and item["updated_at_max"] > row["created_at"]:
                row["created_at"] = item["updated_at_max"]
            if goods_raw and row.get("goods_type") in {"", "-"}:
                row["goods_type"] = goods_raw
            if source == "processing":
                row["processing_reserved_qty"] += qty_value
            elif source == "processing_in_progress":
                row["processing_in_progress_qty"] += qty_value
            elif source == "shipping":
                row["shipping_reserved_qty"] += qty_value

        for item in processing_reserve_groups:
            _upsert_reserve_only_row(item, source="processing")
        for item in processing_in_progress_groups:
            _upsert_reserve_only_row(
                {
                    "agency_id": item.get("agency_id"),
                    "sku": item.get("sku"),
                    "size": item.get("size"),
                    "goods_type": item.get("goods_type"),
                    "reserved_qty": item.get("in_progress_qty"),
                    "updated_at_max": item.get("updated_at_max"),
                },
                source="processing_in_progress",
            )
        for item in shipping_reserve_groups:
            _upsert_reserve_only_row(item, source="shipping")

        for key, row in reserve_only_rows.items():
            if key in present_keys:
                continue
            present_keys.add(key)
            rows.append(row)
    elif not use_materialized_availability:
        _apply_available_qty(rows, processing_reserve_totals, shipping_reserve_totals)
    q = (request.GET.get("q") or "").strip()
    date_from_raw = (request.GET.get("date_from") or "").strip()
    date_to_raw = (request.GET.get("date_to") or "").strip()
    date_from = None
    date_to = None
    try:
        if date_from_raw:
            date_from = datetime.strptime(date_from_raw, "%Y-%m-%d").date()
    except ValueError:
        date_from = None
        date_from_raw = ""
    try:
        if date_to_raw:
            date_to = datetime.strptime(date_to_raw, "%Y-%m-%d").date()
    except ValueError:
        date_to = None
        date_to_raw = ""
    if date_from or date_to or q:
        q_lower = q.lower()
        filtered_rows = []
        for row in rows:
            created_at = row.get("created_at")
            if created_at:
                created_date = created_at.date()
                if date_from and created_date < date_from:
                    continue
                if date_to and created_date > date_to:
                    continue
            if q_lower:
                order_display = f"{row.get('order_type_code', '')}-{row.get('order_id', '')}"
                search_blob = " ".join(
                    str(value)
                    for value in (
                        row.get("client_label"),
                        row.get("order_id"),
                        row.get("order_type_label"),
                        order_display,
                        row.get("sku"),
                        row.get("name"),
                        row.get("size"),
                        row.get("goods_type"),
                        row.get("qty"),
                        row.get("processing_reserved_qty"),
                        row.get("processing_in_progress_qty"),
                        row.get("shipping_reserved_qty"),
                        row.get("available_qty"),
                        row.get("box_code"),
                        row.get("pallet_code"),
                        row.get("location"),
                    )
                    if value not in (None, "")
                ).lower()
                if q_lower not in search_blob:
                    continue
            filtered_rows.append(row)
        rows = filtered_rows
    rows.sort(key=lambda item: item["created_at"], reverse=True)
    role = get_request_role(request)
    if not staff_view:
        template_name = "client_cabinet/inventory_journal.html"
    elif role == "manager":
        template_name = "teammanager/inventory_journal.html"
    else:
        template_name = "sklad/inventory_journal.html"
    return {
        "template_name": template_name,
        "context": {
            "rows": rows,
            "client_agency": client_agency,
            "staff_view": staff_view,
            "q": q,
            "date_from": date_from_raw,
            "date_to": date_to_raw,
        },
    }
