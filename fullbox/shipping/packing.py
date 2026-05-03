from __future__ import annotations

import re

from django.db import transaction
from django.utils import timezone

from audit.models import OrderAuditEntry, log_order_action
from reachtruck.models import MoveTask
from sklad.models import WarehouseContainer, WarehouseReserve, WarehouseStockSnapshot
from sklad.services.warehouse_transitions import WarehouseStateCode
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sku.models import Agency

from .models import ShippingOrder
from .services import close_storekeeper_task, ensure_logistician_task, order_payload

_SHIPPING_PALLET_CODE_RE = re.compile(r"[^A-Z0-9]+")
_SHIPPING_PALLET_LABEL_RE = re.compile(r"^SHIP-\d+-PAL-(\d+)-(.*)$")


def _to_int(value) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _shipping_task_matches_order(order: ShippingOrder, payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    order_number = str(order.number or "").strip()
    payload_number = str(payload.get("shipping_order_id") or payload.get("order_id") or "").strip()
    if payload_number and payload_number == order_number:
        return True
    payload_pk = payload.get("shipping_order_pk") or payload.get("order_pk")
    return str(payload_pk or "").strip() == str(order.pk)


def _shipping_packing_entry(order: ShippingOrder) -> OrderAuditEntry | None:
    return (
        OrderAuditEntry.objects.filter(
            order_id=order.number,
            order_type="shipping",
            payload__act="shipping_packing",
        )
        .order_by("-created_at")
        .first()
    )


def _normalize_shipping_pallet_label(value: str | None) -> str:
    return str(value or "").strip()


def _shipping_box_row_key(box: dict | None, index: int | None = None) -> str:
    if isinstance(box, dict):
        explicit = str(box.get("row_key") or "").strip()
        if explicit:
            return explicit
        box_code = str(box.get("box_code") or box.get("code") or "").strip() or "BOX"
        receiving_order_id = str(box.get("receiving_order_id") or "").strip() or "NA"
        suffix = int(index or box.get("ui_index") or 0)
        if suffix > 0:
            return f"{receiving_order_id}::{box_code}::{suffix}"
        return f"{receiving_order_id}::{box_code}"
    suffix = int(index or 0)
    return f"BOX::{suffix}" if suffix > 0 else "BOX"


def _shipping_pallet_code(order: ShippingOrder, label: str, index: int) -> str:
    cleaned = _SHIPPING_PALLET_CODE_RE.sub("-", label.upper()).strip("-")
    suffix = cleaned[:24] if cleaned else str(index)
    return f"SHIP-{int(order.pk or 0)}-PAL-{index}-{suffix}"


def _shipping_box_items_preview(items: list[dict] | None) -> str:
    prepared = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        barcode = str(item.get("barcode") or item.get("sku_code") or item.get("sku") or "").strip() or "-"
        qty = _to_int(item.get("qty") or item.get("actual_qty") or item.get("count"))
        if qty <= 0:
            continue
        prepared.append(f"{barcode} - {qty} шт.")
    return "; ".join(prepared[:4]) + ("; ..." if len(prepared) > 4 else "")


def _shipping_box_qty(items: list[dict] | None) -> int:
    total = 0
    for item in items or []:
        if not isinstance(item, dict):
            continue
        total += _to_int(item.get("qty") or item.get("actual_qty") or item.get("count"))
    return int(max(total, 0))


def _shipping_receiving_placement_box_map(agency: Agency | None, receiving_order_id: str) -> dict[str, dict]:
    if not agency or not str(receiving_order_id or "").strip():
        return {}
    entries = (
        OrderAuditEntry.objects.filter(
            agency=agency,
            order_type="receiving",
            order_id=str(receiving_order_id).strip(),
            payload__act="placement",
        )
        .order_by("-created_at", "-id")
    )
    box_map: dict[str, dict] = {}
    for entry in entries:
        payload = entry.payload if isinstance(entry.payload, dict) else {}
        for raw_box in payload.get("act_boxes") or []:
            if not isinstance(raw_box, dict):
                continue
            box_code = str(raw_box.get("code") or "").strip()
            if not box_code:
                continue
            box_map.setdefault(box_code.lower(), dict(raw_box))
    return box_map


def _shipping_delivered_boxes_from_warehouse(order: ShippingOrder) -> list[dict]:
    order_key = str(order.number or "").strip()
    if not order_key:
        return []
    snapshots = list(
        WarehouseStockSnapshot.objects.select_related("container", "parent_container", "last_event")
        .filter(
            agency=order.agency,
            shipping_reserved_qty__gt=0,
            is_archived=False,
            last_event__stock_context_type="shipping",
            last_event__stock_context_id=order_key,
            warehouse_state_code__in=["in_otg", "palletization_in_progress", "ready_for_loading"],
        )
        .order_by("id")
    )
    if not snapshots:
        return []

    boxes: dict[str, dict] = {}
    for snapshot in snapshots:
        container = snapshot.container
        box_code = ""
        if container is not None and str(container.container_type or "").strip() == "box":
            box_code = str(container.container_code or "").strip()
        elif str(snapshot.container_code or "").strip():
            box_code = str(snapshot.container_code or "").strip()
        if not box_code:
            continue
        receiving_order_id = (
            str(snapshot.source_context_id or "").strip()
            if str(snapshot.source_context_type or "").strip() == "receiving"
            else ""
        )
        entry = boxes.setdefault(
            box_code.lower(),
            {
                "box_code": box_code,
                "qty": 0,
                "items": [],
                "receiving_order_id": receiving_order_id,
            },
        )
        item_qty = int(snapshot.shipping_reserved_qty or snapshot.qty or 0)
        if item_qty <= 0:
            continue
        entry["qty"] += item_qty
        entry["items"].append(
            {
                "sku_code": str(snapshot.sku_code or "").strip(),
                "name": str(snapshot.name or "").strip(),
                "size": str(snapshot.size or "").strip(),
                "barcode": str(snapshot.barcode or "").strip(),
                "goods_type": str(snapshot.goods_type or "").strip(),
                "qty": item_qty,
            }
        )

    delivered: list[dict] = []
    for index, box in enumerate(sorted(boxes.values(), key=lambda item: str(item.get("box_code") or "").lower()), start=1):
        items = list(box.get("items") or [])
        delivered.append(
            {
                "row_key": _shipping_box_row_key(
                    {
                        "box_code": box.get("box_code"),
                        "receiving_order_id": box.get("receiving_order_id") or "",
                    },
                    index,
                ),
                "box_code": str(box.get("box_code") or "").strip(),
                "qty": int(box.get("qty") or 0),
                "items": items,
                "barcode_preview": _shipping_box_items_preview(items),
                "receiving_order_id": str(box.get("receiving_order_id") or "").strip(),
            }
        )
    return [row for row in delivered if row["box_code"] and int(row.get("qty") or 0) > 0]


def _shipping_warehouse_snapshots(order: ShippingOrder) -> list[WarehouseStockSnapshot]:
    order_key = str(order.number or "").strip()
    if not order_key:
        return []
    snapshots = list(
        WarehouseStockSnapshot.objects.select_related("container", "parent_container", "location", "active_operation")
        .filter(
            agency=order.agency,
            shipping_reserved_qty__gt=0,
            is_archived=False,
            warehouse_state_code__in=[
                WarehouseStateCode.IN_OTG.value,
                WarehouseStateCode.PALLETIZING.value,
                WarehouseStateCode.READY_FOR_LOADING.value,
            ],
        )
        .order_by("id")
    )
    active_statuses = [
        WarehouseReserve.STATUS_ACTIVE,
        WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
        WarehouseReserve.STATUS_ALLOCATED,
        WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
        WarehouseReserve.STATUS_SATISFIED,
    ]
    prepared: list[WarehouseStockSnapshot] = []
    for snapshot in snapshots:
        has_reserve = WarehouseReserve.objects.filter(
            agency=snapshot.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id=order_key,
            sku_code=snapshot.sku_code,
            size=snapshot.size,
            barcode=snapshot.barcode,
            goods_type=snapshot.goods_type,
            status__in=active_statuses,
        ).exists()
        if has_reserve:
            prepared.append(snapshot)
    return prepared


def _shipping_sync_warehouse_packing(
    order: ShippingOrder,
    *,
    act_data: dict,
    user,
) -> bool:
    snapshots = _shipping_warehouse_snapshots(order)
    if not snapshots:
        return False

    operation = next(
        (
            snapshot.active_operation
            for snapshot in snapshots
            if snapshot.active_operation is not None
            and str(snapshot.active_operation.operation_type or "").strip() == "palletization"
            and str(snapshot.active_operation.status or "").strip() in {"created", "planned", "in_progress", "partial"}
        ),
        None,
    )
    if operation is None and any(snapshot.warehouse_state_code == WarehouseStateCode.IN_OTG.value for snapshot in snapshots):
        operation = WarehouseWritePathService.start_palletization(
            agency=order.agency,
            order_id=order.number,
            started_by=user,
        )
        snapshots = _shipping_warehouse_snapshots(order)

    pallet_specs = {
        str(pallet.get("label") or "").strip(): dict(pallet)
        for pallet in (act_data.get("act_pallets") or [])
        if isinstance(pallet, dict) and str(pallet.get("label") or "").strip() and str(pallet.get("code") or "").strip()
    }
    if not pallet_specs:
        return False

    pallet_containers: dict[str, WarehouseContainer] = {}
    for label, pallet_spec in pallet_specs.items():
        pallet_code = str(pallet_spec.get("code") or "").strip()
        location = next((snapshot.location for snapshot in snapshots if snapshot.location_id), None)
        pallet_container, _ = WarehouseContainer.objects.get_or_create(
            agency=order.agency,
            container_code=pallet_code,
            defaults={
                "container_type": WarehouseContainer.TYPE_MIXED_PALLET,
                "current_location": location,
                "created_by": user if getattr(user, "is_authenticated", False) else None,
                "source_context_type": "shipping",
                "source_context_id": str(order.number or "").strip(),
            },
        )
        updates: list[str] = []
        if pallet_container.container_type != WarehouseContainer.TYPE_MIXED_PALLET:
            pallet_container.container_type = WarehouseContainer.TYPE_MIXED_PALLET
            updates.append("container_type")
        if location is not None and pallet_container.current_location_id != location.id:
            pallet_container.current_location = location
            updates.append("current_location")
        if str(pallet_container.source_context_type or "").strip() != "shipping":
            pallet_container.source_context_type = "shipping"
            updates.append("source_context_type")
        if str(pallet_container.source_context_id or "").strip() != str(order.number or "").strip():
            pallet_container.source_context_id = str(order.number or "").strip()
            updates.append("source_context_id")
        if updates:
            pallet_container.save(update_fields=updates + ["updated_at"])
        pallet_containers[pallet_code.lower()] = pallet_container

    target_pallet_by_box_code = {
        str(box.get("code") or "").strip().lower(): str(box.get("pallet_code") or "").strip().lower()
        for box in (act_data.get("act_boxes") or [])
        if isinstance(box, dict) and str(box.get("code") or "").strip() and str(box.get("pallet_code") or "").strip()
    }
    if not target_pallet_by_box_code:
        return False

    for snapshot in snapshots:
        container = snapshot.container
        box_code = ""
        if container is not None and str(container.container_type or "").strip() == WarehouseContainer.TYPE_BOX:
            box_code = str(container.container_code or "").strip()
        elif str(snapshot.container_code or "").strip():
            box_code = str(snapshot.container_code or "").strip()
        target_code = target_pallet_by_box_code.get(box_code.lower())
        target_pallet = pallet_containers.get(str(target_code or "").strip().lower())
        if not target_pallet or not box_code:
            continue
        if container is not None and str(container.container_type or "").strip() == WarehouseContainer.TYPE_BOX:
            box_updates: list[str] = []
            if container.parent_container_id != target_pallet.id:
                container.parent_container = target_pallet
                box_updates.append("parent_container")
            if target_pallet.current_location_id and container.current_location_id != target_pallet.current_location_id:
                container.current_location = target_pallet.current_location
                box_updates.append("current_location")
            if box_updates:
                container.save(update_fields=box_updates + ["updated_at"])
        snapshot_updates: list[str] = []
        if snapshot.parent_container_id != target_pallet.id:
            snapshot.parent_container = target_pallet
            snapshot_updates.append("parent_container")
        if target_pallet.current_location_id and snapshot.location_id != target_pallet.current_location_id:
            snapshot.location = target_pallet.current_location
            snapshot.zone_code = str(target_pallet.current_location.zone_code or "").strip()
            snapshot.zone_kind = str(target_pallet.current_location.zone_kind or "").strip()
            snapshot_updates.extend(["location", "zone_code", "zone_kind"])
        if snapshot_updates:
            snapshot.save(update_fields=snapshot_updates + ["updated_at"])

    if operation is not None and str(operation.status or "").strip() != "done":
        WarehouseWritePathService.complete_palletization(
            operation=operation,
            performed_by=user,
        )
    return True


def _shipping_delivered_boxes(order: ShippingOrder) -> list[dict]:
    warehouse_boxes = _shipping_delivered_boxes_from_warehouse(order)
    if warehouse_boxes:
        return warehouse_boxes
    done_tasks = (
        MoveTask.objects.filter(
            request__agency=order.agency,
            status=MoveTask.STATUS_DONE,
            to_zone="OTG",
        )
        .order_by("id")
    )
    placement_cache: dict[str, dict[str, dict]] = {}
    delivered: list[dict] = []
    seen_codes: set[str] = set()
    row_index = 0
    for task in done_tasks:
        payload = task.payload if isinstance(task.payload, dict) else {}
        if not _shipping_task_matches_order(order, payload):
            continue
        receiving_order_id = str(payload.get("receiving_order_id") or "").strip()
        placement_boxes = placement_cache.get(receiving_order_id)
        if placement_boxes is None:
            placement_boxes = _shipping_receiving_placement_box_map(order.agency, receiving_order_id)
            placement_cache[receiving_order_id] = placement_boxes
        raw_codes = payload.get("picked_boxes") or []
        if not isinstance(raw_codes, list):
            raw_codes = []
        if not raw_codes:
            raw_codes = [
                str(row.get("box_code") or "").strip()
                for row in (payload.get("picked_rows") or [])
                if isinstance(row, dict) and str(row.get("box_code") or "").strip()
            ]
        for raw_code in raw_codes:
            box_code = str(raw_code or "").strip()
            if not box_code or box_code in seen_codes:
                continue
            seen_codes.add(box_code)
            row_index += 1
            box_payload = placement_boxes.get(box_code.lower()) if placement_boxes else None
            items = list(box_payload.get("items") or []) if isinstance(box_payload, dict) else []
            delivered.append(
                {
                    "row_key": _shipping_box_row_key(
                        {
                            "box_code": box_code,
                            "receiving_order_id": receiving_order_id,
                        },
                        row_index,
                    ),
                    "box_code": box_code,
                    "qty": _shipping_box_qty(items),
                    "items": items,
                    "barcode_preview": _shipping_box_items_preview(items),
                    "receiving_order_id": receiving_order_id,
                }
            )
    return delivered


def _shipping_boxes_from_packing_payload(payload: dict | None) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    prepared: list[dict] = []
    for index, raw_box in enumerate(payload.get("act_boxes") or [], start=1):
        if not isinstance(raw_box, dict):
            continue
        box_code = str(raw_box.get("code") or "").strip()
        if not box_code:
            continue
        items = [dict(item) for item in (raw_box.get("items") or []) if isinstance(item, dict)]
        prepared.append(
            {
                "row_key": _shipping_box_row_key(raw_box, index),
                "box_code": box_code,
                "qty": _to_int(raw_box.get("qty")) or _shipping_box_qty(items),
                "items": items,
                "barcode_preview": str(raw_box.get("barcode_preview") or _shipping_box_items_preview(items) or "-"),
                "receiving_order_id": str(raw_box.get("receiving_order_id") or "").strip(),
            }
        )
    return prepared


def _shipping_build_packing_act(
    order: ShippingOrder,
    delivered_boxes: list[dict],
    assignments: dict[str, str],
    source_codes_by_label: dict[str, str] | None = None,
) -> dict:
    pallet_order: list[str] = []
    pallet_codes: dict[str, str] = {}
    pallets_by_label: dict[str, dict] = {}
    act_boxes: list[dict] = []
    item_totals: dict[tuple[str, str, str, str, str], dict] = {}

    for index, box in enumerate(delivered_boxes, start=1):
        row_key = _shipping_box_row_key(box, index)
        box_code = str(box.get("box_code") or "").strip()
        if not box_code:
            continue
        label = _normalize_shipping_pallet_label(assignments.get(row_key))
        if not label:
            continue
        if label not in pallet_codes:
            pallet_order.append(label)
            pallet_codes[label] = _shipping_pallet_code(order, label, len(pallet_order))
        pallet_code = pallet_codes[label]
        source_code = str((source_codes_by_label or {}).get(label) or "").strip()
        items = [dict(item) for item in (box.get("items") or []) if isinstance(item, dict)]
        box_qty = _shipping_box_qty(items) or _to_int(box.get("qty"))
        act_box = {
            "row_key": row_key,
            "code": box_code,
            "qty": int(max(box_qty, 0)),
            "items": items,
            "barcode_preview": _shipping_box_items_preview(items) or str(box.get("barcode_preview") or ""),
            "pallet_code": pallet_code,
            "pallet_label": label,
            "pallet_source_code": source_code,
            "location": {"zone": "OTG"},
            "sealed": True,
        }
        act_boxes.append(act_box)
        pallet_entry = pallets_by_label.setdefault(
            label,
            {
                "code": pallet_code,
                "label": label,
                "source_code": source_code,
                "boxes": [],
                "items": [],
                "location": {"zone": "OTG"},
                "sealed": True,
                "qty": 0,
            },
        )
        pallet_entry["boxes"].append(row_key)
        pallet_entry["qty"] = int(pallet_entry.get("qty") or 0) + int(max(box_qty, 0))
        for item in items:
            key = (
                str(item.get("sku_code") or item.get("sku") or "").strip(),
                str(item.get("name") or "").strip(),
                str(item.get("size") or "").strip(),
                str(item.get("barcode") or "").strip(),
                str(item.get("goods_type") or "").strip(),
            )
            row = item_totals.setdefault(
                key,
                {
                    "sku_code": key[0],
                    "name": key[1],
                    "size": key[2],
                    "barcode": key[3],
                    "goods_type": key[4],
                    "qty": 0,
                },
            )
            row["qty"] += _to_int(item.get("qty") or item.get("actual_qty") or item.get("count"))

    act_pallets: list[dict] = []
    for label in pallet_order:
        pallet_entry = pallets_by_label[label]
        pallet_entry["box_count"] = len(pallet_entry.get("boxes") or [])
        act_pallets.append(pallet_entry)

    return {
        "act": "shipping_packing",
        "act_state": "closed",
        "act_boxes": act_boxes,
        "act_pallets": act_pallets,
        "act_items": list(item_totals.values()),
        "delivered_box_count": len(act_boxes),
        "pallet_count": len(act_pallets),
    }


def _shipping_pallet_label_from_code(container_code: str | None) -> str:
    raw = str(container_code or "").strip()
    if not raw:
        return "-"
    match = _SHIPPING_PALLET_LABEL_RE.match(raw)
    if not match:
        return raw
    suffix = str(match.group(2) or "").strip("-")
    if suffix:
        return suffix
    return str(match.group(1) or "").strip() or raw


def _shipping_packing_summary_from_warehouse(order: ShippingOrder) -> dict | None:
    order_key = str(order.number or "").strip()
    if not order_key:
        return None
    snapshots = list(
        WarehouseStockSnapshot.objects.select_related("container", "parent_container")
        .filter(
            agency=order.agency,
            shipping_reserved_qty__gt=0,
            is_archived=False,
            warehouse_state_code__in=[
                WarehouseStateCode.READY_FOR_LOADING.value,
                "assigned_to_trip",
                "loading_in_progress",
                "loaded_to_vehicle",
            ],
            parent_container__source_context_type="shipping",
            parent_container__source_context_id=order_key,
        )
        .order_by("parent_container__container_code", "id")
    )
    if not snapshots:
        return None

    pallets_by_code: dict[str, dict] = {}
    box_keys: set[tuple[str, str]] = set()
    for snapshot in snapshots:
        pallet = snapshot.parent_container
        if pallet is None:
            continue
        pallet_code = str(pallet.container_code or "").strip()
        if not pallet_code:
            continue
        pallet_entry = pallets_by_code.setdefault(
            pallet_code,
            {
                "label": _shipping_pallet_label_from_code(pallet_code),
                "code": pallet_code,
                "source_code": pallet_code,
                "box_count": 0,
                "qty": 0,
                "boxes": [],
            },
        )
        container = snapshot.container
        box_code = ""
        if container is not None and str(container.container_type or "").strip() == WarehouseContainer.TYPE_BOX:
            box_code = str(container.container_code or "").strip()
        elif str(snapshot.container_code or "").strip():
            box_code = str(snapshot.container_code or "").strip()
        item_qty = int(snapshot.shipping_reserved_qty or snapshot.qty or 0)
        pallet_entry["qty"] += item_qty
        if box_code:
            box_key = (pallet_code, box_code.lower())
            if box_key not in box_keys:
                box_keys.add(box_key)
                pallet_entry["boxes"].append(
                    {
                        "row_key": _shipping_box_row_key(
                            {
                                "code": box_code,
                                "receiving_order_id": str(snapshot.source_context_id or "").strip(),
                            },
                            len(pallet_entry["boxes"]) + 1,
                        ),
                        "box_code": box_code,
                        "qty": 0,
                        "barcode_preview": "",
                        "pallet_label": pallet_entry["label"],
                    }
                )
                pallet_entry["box_count"] += 1
            for box in pallet_entry["boxes"]:
                if str(box.get("box_code") or "").strip().lower() == box_code.lower():
                    box["qty"] = int(box.get("qty") or 0) + item_qty
                    break

    pallets = list(pallets_by_code.values())
    if not pallets:
        return None
    return {
        "pallet_count": len(pallets),
        "box_count": sum(int(pallet.get("box_count") or 0) for pallet in pallets),
        "pallets": pallets,
        "entry": None,
    }


def _shipping_packing_summary(order: ShippingOrder) -> dict | None:
    warehouse_summary = _shipping_packing_summary_from_warehouse(order)
    if warehouse_summary:
        return warehouse_summary
    entry = _shipping_packing_entry(order)
    if not entry or not isinstance(entry.payload, dict):
        return None
    payload = dict(entry.payload or {})
    raw_pallets = payload.get("act_pallets") or []
    raw_boxes = payload.get("act_boxes") or []
    if not isinstance(raw_pallets, list) or not isinstance(raw_boxes, list):
        return None
    boxes_by_key: dict[str, dict] = {}
    boxes_by_code: dict[str, list[dict]] = {}
    for index, box in enumerate(raw_boxes, start=1):
        if not isinstance(box, dict):
            continue
        code = str(box.get("code") or "").strip()
        if not code:
            continue
        row_key = _shipping_box_row_key(box, index)
        box_data = {
            "row_key": row_key,
            "box_code": code,
            "qty": _to_int(box.get("qty")) or _shipping_box_qty(box.get("items") or []),
            "barcode_preview": str(box.get("barcode_preview") or _shipping_box_items_preview(box.get("items") or [])),
            "pallet_label": str(box.get("pallet_label") or "").strip(),
        }
        boxes_by_key[row_key] = box_data
        boxes_by_code.setdefault(code, []).append(box_data)
    pallets = []
    for pallet in raw_pallets:
        if not isinstance(pallet, dict):
            continue
        label = str(pallet.get("label") or pallet.get("code") or "").strip()
        code = str(pallet.get("code") or "").strip()
        pallet_boxes = []
        legacy_box_map = {key: list(rows) for key, rows in boxes_by_code.items()}
        for box_ref in pallet.get("boxes") or []:
            ref = str(box_ref or "").strip()
            box_data = boxes_by_key.get(ref)
            if not box_data:
                fallback_rows = legacy_box_map.get(ref) or []
                box_data = fallback_rows.pop(0) if fallback_rows else None
            if box_data:
                pallet_boxes.append(box_data)
        pallets.append(
            {
                "label": label or code or "-",
                "code": code,
                "source_code": str(pallet.get("source_code") or "").strip(),
                "box_count": len(pallet_boxes),
                "qty": sum(int(box.get("qty") or 0) for box in pallet_boxes),
                "boxes": pallet_boxes,
            }
        )
    return {
        "pallet_count": len(pallets),
        "box_count": len(boxes_by_key),
        "pallets": pallets,
        "entry": entry,
    }


def shipping_packing_slip_meta(order: ShippingOrder) -> dict:
    delivery_date = "-"
    if order.slot_date:
        delivery_date = order.slot_date.strftime("%d.%m.%Y")
        if order.slot_time:
            delivery_date = f"{delivery_date} {order.slot_time.strftime('%H:%M')}"
    elif order.planned_ship_date:
        delivery_date = order.planned_ship_date.strftime("%d.%m.%Y")
    elif order.eta_at:
        eta_at = order.eta_at
        if timezone.is_naive(eta_at):
            eta_at = timezone.make_aware(eta_at, timezone.get_current_timezone())
        delivery_date = timezone.localtime(eta_at).strftime("%d.%m.%Y %H:%M")

    marketplace_name = str(getattr(order.marketplace, "name", "") or "").strip() or "-"
    agency_name = str(getattr(order.agency, "agn_name", "") or "").strip() or "-"
    return {
        "order_number": str(order.number or "").strip(),
        "marketplace_name": marketplace_name,
        "supply_type": order.get_supply_type_display() or "-",
        "shipping_barcode": str(order.shipping_barcode or "").strip() or "-",
        "supply_number": str(order.wb_supply_barcode or "").strip() or "-",
        "destination_warehouse": str(order.destination_warehouse or "").strip() or "-",
        "transit_address": str(order.transit_address or "").strip() if order.wb_transit_warehouse else "",
        "supplier_name": agency_name,
        "delivery_date": delivery_date,
    }


def shipping_packing_slips_data(
    order: ShippingOrder,
    packing_summary: dict | None = None,
) -> list[dict]:
    summary = packing_summary if isinstance(packing_summary, dict) else _shipping_packing_summary(order)
    if not summary:
        return []
    meta = shipping_packing_slip_meta(order)
    pallets = list(summary.get("pallets") or [])
    total_pallets = len(pallets)
    slips: list[dict] = []
    for index, pallet in enumerate(pallets, start=1):
        pallet_data = dict(pallet or {})
        pallet_label = str(pallet_data.get("label") or pallet_data.get("code") or index).strip() or str(index)
        pallet_code = str(pallet_data.get("code") or "").strip()
        boxes = list(pallet_data.get("boxes") or [])
        qr_source = str(pallet_data.get("source_code") or pallet_code or pallet_label).strip()
        qr_value = f"{meta['order_number']}::{qr_source}".strip(":")
        slips.append(
            {
                "slip_key": pallet_code or f"PAL-{index}",
                "pallet_index": str(index),
                "pallet_label": pallet_label,
                "pallet_code": pallet_code,
                "box_count": str(len(boxes)),
                "total_pallets": str(total_pallets),
                "marketplace_name": str(meta.get("marketplace_name") or "-").upper(),
                "supply_type": str(meta.get("supply_type") or "-"),
                "supply_number": str(meta.get("supply_number") or "-"),
                "shipping_barcode": str(meta.get("shipping_barcode") or "-"),
                "destination_warehouse": str(meta.get("destination_warehouse") or "-"),
                "transit_address": str(meta.get("transit_address") or "-"),
                "supplier_name": str(meta.get("supplier_name") or "-"),
                "delivery_date": str(meta.get("delivery_date") or "-"),
                "qr_value": qr_value or "-",
            }
        )
    return slips


def _shipping_manageable_packing_boxes(
    order: ShippingOrder,
    packing_summary: dict | None = None,
) -> list[dict]:
    delivered_boxes = _shipping_delivered_boxes(order)
    if delivered_boxes:
        return delivered_boxes
    summary = packing_summary if isinstance(packing_summary, dict) else _shipping_packing_summary(order)
    entry = summary.get("entry") if isinstance(summary, dict) else None
    payload = entry.payload if entry and isinstance(entry.payload, dict) else {}
    return _shipping_boxes_from_packing_payload(payload)


def _shipping_packing_initial_state(
    order: ShippingOrder,
    packing_summary: dict | None = None,
) -> dict:
    summary = packing_summary if isinstance(packing_summary, dict) else _shipping_packing_summary(order)
    delivered_boxes = _shipping_manageable_packing_boxes(order, summary)

    existing_assignments_by_row: dict[str, str] = {}
    existing_assignments_by_code: dict[str, str] = {}
    if summary:
        for pallet in summary.get("pallets") or []:
            label = str((pallet or {}).get("label") or "").strip()
            for box_index, box in enumerate((pallet or {}).get("boxes") or [], start=1):
                box_code = str((box or {}).get("box_code") or "").strip()
                if not box_code or not label:
                    continue
                row_key = _shipping_box_row_key(box, box_index)
                if row_key:
                    existing_assignments_by_row[row_key] = label
                existing_assignments_by_code.setdefault(box_code, label)

    initial_boxes: list[dict] = []
    for index, box in enumerate(delivered_boxes, start=1):
        row_key = _shipping_box_row_key(box, index)
        box_code = str(box.get("box_code") or "").strip()
        assigned_label = existing_assignments_by_row.get(row_key) or existing_assignments_by_code.get(box_code, "")
        initial_boxes.append(
            {
                "row_key": row_key,
                "code": box_code,
                "qty": int(box.get("qty") or 0),
                "barcode_preview": str(box.get("barcode_preview") or "-"),
                "items": list(box.get("items") or []),
                "pallet_code": assigned_label,
                "ui_index": index,
            }
        )

    initial_pallets: list[dict] = []
    seen_labels: set[str] = set()
    for box in initial_boxes:
        label = _normalize_shipping_pallet_label(box.get("pallet_code"))
        if not label or label in seen_labels:
            continue
        seen_labels.add(label)
        initial_pallets.append({"code": label, "label": label})

    return {
        "packing_summary": summary,
        "delivered_boxes": delivered_boxes,
        "initial_boxes": initial_boxes,
        "initial_pallets": initial_pallets,
    }


def save_shipping_packing(
    order: ShippingOrder,
    *,
    boxes_state: list,
    pallets_state: list,
    delivered_boxes: list[dict],
    initial_boxes: list[dict],
    user,
) -> dict:
    errors: list[str] = []
    if not isinstance(boxes_state, list):
        boxes_state = []
    if not isinstance(pallets_state, list):
        pallets_state = []

    delivered_map: dict[str, dict] = {}
    for index, box in enumerate(delivered_boxes, start=1):
        row_key = _shipping_box_row_key(box, index)
        if row_key:
            delivered_map[row_key] = box

    used_pallet_codes: set[str] = set()
    for raw_box in boxes_state:
        if not isinstance(raw_box, dict):
            continue
        pallet_code = str(raw_box.get("pallet_code") or "").strip()
        row_key = _shipping_box_row_key(raw_box)
        if row_key in delivered_map and pallet_code:
            used_pallet_codes.add(pallet_code)

    pallet_labels: dict[str, str] = {}
    for raw_pallet in pallets_state:
        if not isinstance(raw_pallet, dict):
            continue
        code = str(raw_pallet.get("code") or "").strip()
        label = _normalize_shipping_pallet_label(raw_pallet.get("label") or code)
        if not code:
            continue
        if code not in used_pallet_codes:
            continue
        if not label:
            errors.append("Укажите название для каждой новой паллеты.")
            continue
        pallet_labels[code] = label

    assignments: dict[str, str] = {}
    seen_row_keys: set[str] = set()
    for raw_box in boxes_state:
        if not isinstance(raw_box, dict):
            continue
        row_key = _shipping_box_row_key(raw_box)
        box_code = str(raw_box.get("code") or "").strip()
        pallet_code = str(raw_box.get("pallet_code") or "").strip()
        delivered_box = delivered_map.get(row_key)
        if not box_code or delivered_box is None:
            continue
        seen_row_keys.add(row_key)
        if not pallet_code:
            errors.append(f"Укажите паллету для короба {box_code}.")
            continue
        label = pallet_labels.get(pallet_code)
        if not label:
            errors.append(f"Для короба {box_code} выбрана неизвестная паллета.")
            continue
        assignments[row_key] = label

    missing_boxes = [box for box in initial_boxes if _shipping_box_row_key(box) not in seen_row_keys]
    for box in missing_boxes:
        errors.append(f"Короб {box.get('code') or '-'} отсутствует в раскладке.")

    if errors:
        return {"saved": False, "errors": errors}

    source_codes_by_label = {
        str(label or "").strip(): str(code or "").strip()
        for code, label in pallet_labels.items()
        if str(label or "").strip()
    }
    act_data = _shipping_build_packing_act(
        order,
        delivered_boxes,
        assignments,
        source_codes_by_label=source_codes_by_label,
    )
    with transaction.atomic():
        _shipping_sync_warehouse_packing(
            order,
            act_data=act_data,
            user=user,
        )
        order.status = ShippingOrder.STATUS_PACKED
        order.save(update_fields=["status", "updated_at"])
        close_storekeeper_task(order)
        ensure_logistician_task(order, user)
        log_order_action(
            action="status",
            order_id=order.number,
            order_type="shipping",
            user=user if getattr(user, "is_authenticated", False) else None,
            agency=order.agency,
            description="Кладовщик разложил короба по новым паллетам для отгрузки",
            payload=order_payload(order, extra=act_data),
        )
    return {"saved": True, "errors": [], "act_data": act_data}
