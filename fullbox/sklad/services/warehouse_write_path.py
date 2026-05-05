from __future__ import annotations

from dataclasses import dataclass

from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from sku.models import Agency, SKU
from sklad.models import (
    WarehouseContainer,
    WarehouseEvent,
    WarehouseLocation,
    WarehouseOperation,
    WarehouseOperationTask,
    WarehouseReserve,
    WarehouseStockSnapshot,
)

from .warehouse_events import WarehouseEventType
from .warehouse_transitions import WarehouseStateCode, WarehouseTransitionService


ZONE_KIND_BY_CODE = {
    "PR": WarehouseLocation.ZONE_KIND_RECEIVING,
    "OS": WarehouseLocation.ZONE_KIND_STORAGE,
    "MR": WarehouseLocation.ZONE_KIND_STORAGE,
    "OBR": WarehouseLocation.ZONE_KIND_PROCESSING,
    "OTG": WarehouseLocation.ZONE_KIND_SHIPPING,
    "LOAD": WarehouseLocation.ZONE_KIND_LOADING,
    "VEH": WarehouseLocation.ZONE_KIND_VEHICLE,
}
_PUTAWAY_DESTINATION_BLOCKING_STATUSES = (
    WarehouseOperation.STATUS_CREATED,
    WarehouseOperation.STATUS_PLANNED,
    WarehouseOperation.STATUS_IN_PROGRESS,
    WarehouseOperation.STATUS_PARTIAL,
    WarehouseOperation.STATUS_BLOCKED,
)


@dataclass(frozen=True)
class WarehousePlacementResult:
    snapshot_ids: list[int]
    event_ids: list[int]


class WarehouseWritePathService:
    @classmethod
    def ensure_putaway_destination_available(
        cls,
        *,
        destination: WarehouseLocation | None,
        exclude_operation_id: int | None = None,
        exclude_container_code: str = "",
    ) -> None:
        if destination is None:
            raise ValueError("Putaway destination is required")
        if str(destination.zone_code or "").strip().upper() != "OS":
            return

        location_label = cls._location_display_name(
            str(destination.zone_code or "").strip().upper(),
            int(destination.row_no or 0),
            int(destination.section_no or 0),
            int(destination.tier_no or 0),
            int(destination.cell_no or 0),
        )
        normalized_container_code = str(exclude_container_code or "").strip()

        occupied_snapshots = WarehouseStockSnapshot.objects.filter(
            location=destination,
            zone_code__iexact="OS",
            is_archived=False,
        )
        if normalized_container_code:
            occupied_snapshots = occupied_snapshots.exclude(
                models.Q(container_code__iexact=normalized_container_code)
                | models.Q(container__container_code__iexact=normalized_container_code)
                | models.Q(parent_container__container_code__iexact=normalized_container_code)
            )
        if occupied_snapshots.exists():
            raise ValueError(f"Место хранения {location_label} уже занято на складе.")

        reserved_operations = WarehouseOperation.objects.filter(
            operation_type=WarehouseOperation.TYPE_PUTAWAY,
            destination_location=destination,
            status__in=_PUTAWAY_DESTINATION_BLOCKING_STATUSES,
        )
        if exclude_operation_id:
            reserved_operations = reserved_operations.exclude(id=int(exclude_operation_id))
        if normalized_container_code:
            reserved_operations = reserved_operations.exclude(
                tasks__container__container_code__iexact=normalized_container_code
            ).distinct()
        if reserved_operations.exists():
            raise ValueError(
                f"Место хранения {location_label} уже зарезервировано другой заявкой ричтрака."
            )

    @classmethod
    @transaction.atomic
    def clear_receiving_context(
        cls,
        *,
        agency: Agency,
        order_id: str,
    ) -> None:
        order_key = str(order_id or "").strip()
        if not order_key:
            return

        snapshots = list(
            WarehouseStockSnapshot.objects.filter(
                agency=agency,
                source_context_type="receiving",
                source_context_id=order_key,
            ).values_list("id", flat=True)
        )
        operations = list(
            WarehouseOperation.objects.filter(
                agency=agency,
                context_type="receiving",
                context_id=order_key,
            ).values_list("id", flat=True)
        )
        reserves = list(
            WarehouseReserve.objects.filter(
                agency=agency,
                context_type="receiving",
                context_id=order_key,
            ).values_list("id", flat=True)
        )
        containers = list(
            WarehouseContainer.objects.filter(
                agency=agency,
                source_context_type="receiving",
                source_context_id=order_key,
            ).values_list("id", flat=True)
        )

        if snapshots:
            WarehouseStockSnapshot.objects.filter(id__in=snapshots).delete()
        if operations:
            task_ids = list(
                WarehouseOperationTask.objects.filter(operation_id__in=operations).values_list("id", flat=True)
            )
            if task_ids:
                WarehouseEvent.objects.filter(operation_task_id__in=task_ids).delete()
                WarehouseOperationTask.objects.filter(id__in=task_ids).delete()
            WarehouseEvent.objects.filter(operation_id__in=operations).delete()
            WarehouseOperation.objects.filter(id__in=operations).delete()
        if reserves:
            WarehouseEvent.objects.filter(reserve_id__in=reserves).delete()
            WarehouseReserve.objects.filter(id__in=reserves).delete()
        WarehouseEvent.objects.filter(
            agency=agency,
            stock_context_type="receiving",
            stock_context_id=order_key,
        ).delete()
        if containers:
            WarehouseContainer.objects.filter(id__in=containers).delete()

    @staticmethod
    def _reserve_open_qty(reserve: WarehouseReserve) -> int:
        return max(int(reserve.qty_reserved or 0) - int(reserve.qty_satisfied or 0), 0)

    @staticmethod
    def _reserve_matches_snapshot(reserve: WarehouseReserve, snapshot: WarehouseStockSnapshot) -> bool:
        return (
            int(reserve.agency_id or 0) == int(snapshot.agency_id or 0)
            and str(reserve.sku_code or "").strip() == str(snapshot.sku_code or "").strip()
            and str(reserve.size or "").strip() == str(snapshot.size or "").strip()
            and str(reserve.barcode or "").strip() == str(snapshot.barcode or "").strip()
            and str(reserve.goods_type or "").strip() == str(snapshot.goods_type or "").strip()
        )

    @classmethod
    def _reserve_snapshot_id_map(cls, reserves: list[WarehouseReserve]) -> dict[int, int]:
        reserve_ids = [int(reserve.id) for reserve in reserves if int(reserve.id or 0) > 0]
        if not reserve_ids:
            return {}
        snapshot_by_reserve: dict[int, int] = {}
        for row in (
            WarehouseEvent.objects.filter(reserve_id__in=reserve_ids)
            .exclude(payload__isnull=True)
            .values("reserve_id", "payload")
            .order_by("id")
        ):
            reserve_id = int(row.get("reserve_id") or 0)
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            snapshot_id = int(payload.get("snapshot_id") or 0)
            if reserve_id and snapshot_id:
                snapshot_by_reserve.setdefault(reserve_id, snapshot_id)
        return snapshot_by_reserve

    @classmethod
    def _release_reserves_from_snapshots(
        cls,
        *,
        reserves: list[WarehouseReserve],
        reserved_qty_field: str,
        release_event_type: WarehouseEventType,
        performed_by=None,
    ) -> None:
        if not reserves:
            return
        snapshot_id_by_reserve = cls._reserve_snapshot_id_map(reserves)
        snapshot_ids = {snapshot_id for snapshot_id in snapshot_id_by_reserve.values() if snapshot_id}
        snapshots_by_id = {
            int(snapshot.id): snapshot
            for snapshot in WarehouseStockSnapshot.objects.select_for_update().filter(id__in=snapshot_ids)
        }
        for reserve in reserves:
            remaining_to_release = cls._reserve_open_qty(reserve)
            if remaining_to_release <= 0:
                continue
            candidates: list[WarehouseStockSnapshot] = []
            snapshot_id = snapshot_id_by_reserve.get(int(reserve.id or 0))
            if snapshot_id and snapshot_id in snapshots_by_id:
                candidates.append(snapshots_by_id[snapshot_id])
            if not candidates:
                candidates = list(
                    WarehouseStockSnapshot.objects.select_for_update()
                    .filter(
                        agency=reserve.agency,
                        sku_code=reserve.sku_code,
                        size=reserve.size,
                        barcode=reserve.barcode,
                        goods_type=reserve.goods_type,
                        is_archived=False,
                    )
                    .order_by("id")
                )
            for snapshot in candidates:
                if not cls._reserve_matches_snapshot(reserve, snapshot):
                    continue
                snapshot_reserved_qty = int(getattr(snapshot, reserved_qty_field, 0) or 0)
                if snapshot_reserved_qty <= 0:
                    continue
                released_qty = min(snapshot_reserved_qty, remaining_to_release)
                try:
                    transition = WarehouseTransitionService.apply_event(
                        snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                        release_event_type,
                    )
                    next_state = transition.code.value
                except ValueError:
                    next_state = snapshot.warehouse_state_code
                release_event = WarehouseEvent.objects.create(
                    agency=snapshot.agency,
                    event_type=release_event_type.value,
                    stock_context_type=reserve.context_type,
                    stock_context_id=reserve.context_id,
                    container=snapshot.container,
                    reserve=reserve,
                    from_location=snapshot.location,
                    to_location=snapshot.location,
                    from_zone_code=snapshot.zone_code,
                    to_zone_code=snapshot.zone_code,
                    qty=released_qty,
                    performed_by=performed_by,
                    performed_by_role=cls._role_of(performed_by),
                    occurred_at=timezone.now(),
                    payload={"snapshot_id": snapshot.id},
                )
                setattr(snapshot, reserved_qty_field, snapshot_reserved_qty - released_qty)
                snapshot.available_qty = min(int(snapshot.qty or 0), int(snapshot.available_qty or 0) + released_qty)
                if int(getattr(snapshot, reserved_qty_field, 0) or 0) <= 0:
                    if int(snapshot.shipping_reserved_qty or 0) > 0:
                        snapshot.warehouse_state_code = WarehouseStateCode.RESERVED_FOR_SHIPPING.value
                    elif int(snapshot.processing_reserved_qty or 0) > 0:
                        snapshot.warehouse_state_code = WarehouseStateCode.RESERVED_FOR_PROCESSING.value
                    else:
                        snapshot.warehouse_state_code = next_state
                snapshot.last_event = release_event
                snapshot.save(
                    update_fields=[
                        reserved_qty_field,
                        "available_qty",
                        "warehouse_state_code",
                        "last_event",
                        "updated_at",
                    ]
                )
                remaining_to_release -= released_qty
                if remaining_to_release <= 0:
                    break

    @classmethod
    @transaction.atomic
    def replace_processing_reserves(
        cls,
        *,
        agency: Agency,
        order_id: str,
        items: list[dict],
        created_by=None,
        source_document_type: str = "processing_order",
        source_document_id: str = "",
    ) -> list[WarehouseReserve]:
        order_key = str(order_id or "").strip()
        if not order_key:
            return []
        active_reserves = list(
            WarehouseReserve.objects.filter(
                agency=agency,
                reserve_type=WarehouseReserve.TYPE_PROCESSING,
                context_type="processing",
                context_id=order_key,
                status__in=[
                    WarehouseReserve.STATUS_ACTIVE,
                    WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
                    WarehouseReserve.STATUS_ALLOCATED,
                    WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
                    WarehouseReserve.STATUS_SATISFIED,
                ],
            ).order_by("id")
        )
        cls._release_reserves_from_snapshots(
            reserves=active_reserves,
            reserved_qty_field="processing_reserved_qty",
            release_event_type=WarehouseEventType.PROCESSING_RESERVE_RELEASED,
            performed_by=created_by,
        )
        if active_reserves:
            WarehouseReserve.objects.filter(id__in=[reserve.id for reserve in active_reserves]).update(
                status=WarehouseReserve.STATUS_RELEASED,
                released_by=created_by if getattr(created_by, "is_authenticated", False) else None,
                updated_at=timezone.now(),
            )
        if not items:
            return []
        return cls.reserve_for_processing(
            agency=agency,
            order_id=order_key,
            items=items,
            created_by=created_by,
            source_document_type=source_document_type,
            source_document_id=source_document_id,
        )

    @classmethod
    @transaction.atomic
    def sync_receiving_placement(
        cls,
        *,
        agency: Agency,
        order_id: str,
        placement_payload: dict,
        performed_by=None,
        warehouse_code: str = "MSK",
    ) -> WarehousePlacementResult:
        order_key = str(order_id or "").strip()
        if not order_key:
            raise ValueError("order_id is required for receiving placement sync")

        cls.clear_receiving_context(agency=agency, order_id=order_key)
        items = cls._receiving_items_from_placement_payload(
            order_id=order_key,
            placement_payload=placement_payload,
        )
        if not items:
            return WarehousePlacementResult(snapshot_ids=[], event_ids=[])
        return cls.create_receiving_placement(
            agency=agency,
            order_id=order_key,
            items=items,
            performed_by=performed_by,
            warehouse_code=warehouse_code,
            source_document_type="placement_act",
            source_document_id=order_key,
            stock_context_type="receiving",
        )

    @classmethod
    @transaction.atomic
    def create_receiving_placement(
        cls,
        *,
        agency: Agency,
        order_id: str,
        items: list[dict],
        performed_by=None,
        warehouse_code: str = "MSK",
        source_document_type: str = "receiving_order",
        source_document_id: str = "",
        stock_context_type: str = "receiving",
        respect_item_location: bool = False,
    ) -> WarehousePlacementResult:
        if not items:
            return WarehousePlacementResult(snapshot_ids=[], event_ids=[])

        order_key = str(order_id or "").strip()
        if not order_key:
            raise ValueError("order_id is required for receiving placement")

        source_document_id = str(source_document_id or order_key).strip()
        stock_context_type = str(stock_context_type or "receiving").strip()
        receiving_location = cls.ensure_location(
            warehouse_code=warehouse_code,
            zone_code="PR",
            row_no=0,
            section_no=0,
            tier_no=0,
            cell_no=0,
        )

        receiving_event = WarehouseEvent.objects.create(
            agency=agency,
            event_type=WarehouseEventType.RECEIVING_ARRIVED.value,
            stock_context_type=stock_context_type,
            stock_context_id=order_key,
            source_document_type=source_document_type,
            source_document_id=source_document_id,
            to_location=receiving_location,
            to_zone_code=receiving_location.zone_code,
            qty=sum(max(int(item.get("qty") or 0), 0) for item in items),
            performed_by=performed_by,
            performed_by_role=cls._role_of(performed_by),
            occurred_at=timezone.now(),
            payload={"item_count": len(items)},
        )

        snapshot_ids: list[int] = []
        event_ids: list[int] = [receiving_event.id]
        for item in items:
            qty = max(int(item.get("qty") or 0), 0)
            if qty <= 0:
                continue
            target_location = (
                cls._location_from_item(
                    item=item,
                    fallback=receiving_location,
                    warehouse_code=warehouse_code,
                )
                if respect_item_location
                else receiving_location
            )
            sku_ref = cls._resolve_sku_ref(agency=agency, item=item)
            container = cls._resolve_container(
                agency=agency,
                item=item,
                current_location=target_location,
                performed_by=performed_by,
            )
            transition = WarehouseTransitionService.apply_event(
                WarehouseStateCode.RECEIVED_UNPLACED,
                WarehouseEventType.PLACEMENT_COMPLETED,
            )
            placement_event = WarehouseEvent.objects.create(
                agency=agency,
                event_type=WarehouseEventType.PLACEMENT_COMPLETED.value,
                stock_context_type=stock_context_type,
                stock_context_id=order_key,
                container=container,
                source_document_type=source_document_type,
                source_document_id=source_document_id,
                to_location=target_location,
                to_zone_code=target_location.zone_code,
                qty=qty,
                performed_by=performed_by,
                performed_by_role=cls._role_of(performed_by),
                occurred_at=timezone.now(),
                payload={
                    "sku_code": str(item.get("sku_code") or item.get("sku") or "").strip(),
                    "name": str(item.get("name") or "").strip(),
                    "size": str(item.get("size") or "").strip(),
                    "barcode": str(item.get("barcode") or "").strip(),
                    "goods_type": str(item.get("goods_type") or "").strip(),
                    "marking_code": str(item.get("marking_code") or "").strip(),
                },
            )
            snapshot = WarehouseStockSnapshot.objects.create(
                agency=agency,
                stock_unit_type="item",
                source_context_type=stock_context_type,
                source_context_id=order_key,
                sku_ref=sku_ref,
                sku_code=str(item.get("sku_code") or item.get("sku") or "").strip(),
                name=str(item.get("name") or getattr(sku_ref, "name", "") or "").strip(),
                size=str(item.get("size") or "").strip(),
                barcode=str(item.get("barcode") or "").strip(),
                goods_type=str(item.get("goods_type") or "").strip(),
                marking_code=str(item.get("marking_code") or "").strip(),
                qty=qty,
                available_qty=qty,
                container=container,
                container_code=container.container_code if container else "",
                parent_container=container.parent_container if container else None,
                location=target_location,
                zone_code=target_location.zone_code,
                zone_kind=target_location.zone_kind,
                warehouse_state_code=(
                    transition.code.value
                    if target_location.zone_code == "PR"
                    else cls._state_for_location(target_location)
                ),
                last_event=placement_event,
            )
            snapshot_ids.append(snapshot.id)
            event_ids.append(placement_event.id)

        return WarehousePlacementResult(snapshot_ids=snapshot_ids, event_ids=event_ids)

    @classmethod
    @transaction.atomic
    def request_putaway_for_receiving(
        cls,
        *,
        agency: Agency,
        order_id: str,
        container_codes: list[str] | None = None,
        destination_zone_code: str = "OS",
        destination_row_no: int = 0,
        destination_section_no: int = 0,
        destination_tier_no: int = 0,
        destination_cell_no: int = 0,
        requested_by=None,
        requested_by_role: str = "storekeeper",
        warehouse_code: str = "MSK",
        source_document_type: str = "",
        source_document_id: str = "",
    ) -> WarehouseOperation:
        order_key = str(order_id or "").strip()
        snapshot_query = WarehouseStockSnapshot.objects.select_related("location", "container", "parent_container").filter(
            agency=agency,
            source_context_type="receiving",
            source_context_id=order_key,
            warehouse_state_code=WarehouseStateCode.PLACED_IN_RECEIVING.value,
            is_archived=False,
        )
        normalized_container_codes = [
            str(value or "").strip()
            for value in (container_codes or [])
            if str(value or "").strip()
        ]
        if normalized_container_codes:
            snapshot_query = snapshot_query.filter(
                models.Q(container_code__in=normalized_container_codes)
                | models.Q(container__container_code__in=normalized_container_codes)
                | models.Q(parent_container__container_code__in=normalized_container_codes)
            )
        snapshots = list(snapshot_query.order_by("id"))
        if not snapshots:
            raise ValueError("No receiving snapshots ready for putaway")

        source_location = snapshots[0].location or cls.ensure_location(warehouse_code=warehouse_code, zone_code="PR")
        destination = cls.ensure_location(
            warehouse_code=warehouse_code,
            zone_code=destination_zone_code,
            row_no=destination_row_no,
            section_no=destination_section_no,
            tier_no=destination_tier_no,
            cell_no=destination_cell_no,
        )
        cls.ensure_putaway_destination_available(
            destination=destination,
            exclude_container_code=normalized_container_codes[0] if len(normalized_container_codes) == 1 else "",
        )
        operation = WarehouseOperation.objects.create(
            agency=agency,
            operation_type=WarehouseOperation.TYPE_PUTAWAY,
            context_type="receiving",
            context_id=order_key,
            source_location=source_location,
            destination_location=destination,
            source_document_type=str(source_document_type or "").strip(),
            source_document_id=str(source_document_id or "").strip(),
            source_zone_code=source_location.zone_code,
            destination_zone_code=destination.zone_code,
            status=WarehouseOperation.STATUS_PLANNED,
            requested_by=requested_by,
            requested_by_role=requested_by_role,
            assigned_executor_role="reachtruck",
            planned_qty=sum(int(snapshot.qty or 0) for snapshot in snapshots),
        )
        WarehouseEvent.objects.create(
            agency=agency,
            event_type=WarehouseEventType.PUTAWAY_REQUESTED.value,
            stock_context_type="receiving",
            stock_context_id=order_key,
            operation=operation,
            from_location=source_location,
            to_location=destination,
            from_zone_code=source_location.zone_code,
            to_zone_code=destination.zone_code,
            qty=operation.planned_qty,
            performed_by=requested_by,
            performed_by_role=requested_by_role,
            occurred_at=timezone.now(),
        )
        grouped_tasks: dict[tuple[str, int], dict] = {}
        for snapshot in snapshots:
            move_container = snapshot.parent_container or snapshot.container
            if move_container:
                task_key = ("container", int(move_container.id))
            else:
                task_key = ("snapshot", int(snapshot.id))
            task_bucket = grouped_tasks.setdefault(
                task_key,
                {
                    "container": move_container,
                    "from_location": snapshot.location,
                    "from_zone_code": snapshot.zone_code,
                    "qty_planned": 0,
                    "snapshot_ids": [],
                    "container_code": (
                        move_container.container_code
                        if move_container
                        else snapshot.container_code
                    ),
                },
            )
            task_bucket["qty_planned"] += int(snapshot.qty or 0)
            task_bucket["snapshot_ids"].append(int(snapshot.id))
        for task_bucket in grouped_tasks.values():
            task_container = task_bucket["container"]
            WarehouseOperationTask.objects.create(
                operation=operation,
                task_type=(
                    WarehouseOperationTask.TYPE_PALLET_MOVE
                    if task_container
                    and task_container.container_type
                    in {WarehouseContainer.TYPE_PALLET, WarehouseContainer.TYPE_MIXED_PALLET}
                    else WarehouseOperationTask.TYPE_BOX_MOVE
                ),
                container=task_container,
                from_location=task_bucket["from_location"],
                to_location=destination,
                from_zone_code=task_bucket["from_zone_code"],
                to_zone_code=destination.zone_code,
                qty_planned=int(task_bucket["qty_planned"] or 0),
                status=WarehouseOperationTask.STATUS_CREATED,
                executor_role="reachtruck",
                payload={
                    "snapshot_ids": task_bucket["snapshot_ids"],
                    "container_code": task_bucket["container_code"],
                },
            )
        for snapshot in snapshots:
            snapshot.active_operation = operation
            snapshot.active_operation_type = operation.operation_type
            snapshot.save(update_fields=["active_operation", "active_operation_type", "updated_at"])
        return operation

    @classmethod
    @transaction.atomic
    def complete_putaway_operation(
        cls,
        *,
        operation: WarehouseOperation,
        performed_by=None,
    ) -> WarehouseOperation:
        if operation.operation_type != WarehouseOperation.TYPE_PUTAWAY:
            raise ValueError("Only putaway operations can be completed by this write-path")
        destination = operation.destination_location
        if destination is None:
            raise ValueError("Putaway operation must have destination_location")

        snapshots = list(
            WarehouseStockSnapshot.objects.select_related("container", "parent_container")
            .filter(active_operation=operation, is_archived=False)
            .order_by("id")
        )
        total_done = 0
        touched_container_ids: set[int] = set()
        for snapshot in snapshots:
            move_container = snapshot.parent_container or snapshot.container
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.PUTAWAY_COMPLETED,
                operation_type=operation.operation_type,
                zone_to=destination.zone_code,
            )
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.PUTAWAY_COMPLETED.value,
                stock_context_type=snapshot.source_context_type,
                stock_context_id=snapshot.source_context_id,
                container=move_container,
                operation=operation,
                from_location=snapshot.location,
                to_location=destination,
                from_zone_code=snapshot.zone_code,
                to_zone_code=destination.zone_code,
                qty=int(snapshot.qty or 0),
                performed_by=performed_by,
                performed_by_role=cls._role_of(performed_by) or "reachtruck",
                occurred_at=timezone.now(),
            )
            if snapshot.container_id:
                touched_container_ids.add(int(snapshot.container_id))
            if snapshot.parent_container_id:
                touched_container_ids.add(int(snapshot.parent_container_id))
            snapshot.location = destination
            snapshot.zone_code = destination.zone_code
            snapshot.zone_kind = destination.zone_kind
            snapshot.warehouse_state_code = transition.code.value
            snapshot.active_operation = None
            snapshot.active_operation_type = ""
            snapshot.last_event = event
            snapshot.save(
                update_fields=[
                    "location",
                    "zone_code",
                    "zone_kind",
                    "warehouse_state_code",
                    "active_operation",
                    "active_operation_type",
                    "last_event",
                    "updated_at",
                ]
            )
            total_done += int(snapshot.qty or 0)

        if touched_container_ids:
            WarehouseContainer.objects.filter(id__in=touched_container_ids).update(
                current_location=destination,
                updated_at=timezone.now(),
            )

        operation.status = WarehouseOperation.STATUS_DONE
        operation.done_qty = total_done
        operation.completed_at = timezone.now()
        operation.save(update_fields=["status", "done_qty", "completed_at", "updated_at"])

        operation.tasks.update(
            status=WarehouseOperationTask.STATUS_DONE,
            qty_done=models.F("qty_planned"),
            completed_at=timezone.now(),
            updated_at=timezone.now(),
        )
        return operation

    @classmethod
    @transaction.atomic
    def reserve_for_processing(
        cls,
        *,
        agency: Agency,
        order_id: str,
        items: list[dict],
        created_by=None,
        source_document_type: str = "processing_order",
        source_document_id: str = "",
    ) -> list[WarehouseReserve]:
        order_key = str(order_id or "").strip()
        if not order_key:
            raise ValueError("order_id is required for processing reserve")
        source_document_id = str(source_document_id or order_key).strip()
        reserves: list[WarehouseReserve] = []
        for item in items:
            qty = max(int(item.get("qty") or 0), 0)
            if qty <= 0:
                continue
            allocations = cls._match_snapshots_for_processing_reserve(
                agency=agency,
                item=item,
                required_qty=qty,
            )
            for snapshot, reserved_qty in allocations:
                transition = WarehouseTransitionService.apply_event(
                    snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                    WarehouseEventType.PROCESSING_RESERVED,
                )
                reserve = WarehouseReserve.objects.create(
                    agency=agency,
                    reserve_type=WarehouseReserve.TYPE_PROCESSING,
                    context_type="processing",
                    context_id=order_key,
                    sku_ref=snapshot.sku_ref,
                    sku_code=snapshot.sku_code,
                    size=snapshot.size,
                    barcode=snapshot.barcode,
                    goods_type=snapshot.goods_type,
                    marking_code=snapshot.marking_code,
                    qty_reserved=reserved_qty,
                    status=WarehouseReserve.STATUS_ACTIVE,
                    source_document_type=source_document_type,
                    source_document_id=source_document_id,
                    created_by=created_by,
                )
                event = WarehouseEvent.objects.create(
                    agency=agency,
                    event_type=WarehouseEventType.PROCESSING_RESERVED.value,
                    stock_context_type="processing",
                    stock_context_id=order_key,
                    container=snapshot.container,
                    reserve=reserve,
                    source_document_type=source_document_type,
                    source_document_id=source_document_id,
                    from_location=snapshot.location,
                    to_location=snapshot.location,
                    from_zone_code=snapshot.zone_code,
                    to_zone_code=snapshot.zone_code,
                    qty=reserved_qty,
                    performed_by=created_by,
                    performed_by_role=cls._role_of(created_by),
                    occurred_at=timezone.now(),
                    payload={"snapshot_id": snapshot.id},
                )
                snapshot.processing_reserved_qty += reserved_qty
                snapshot.available_qty -= reserved_qty
                snapshot.warehouse_state_code = transition.code.value
                snapshot.last_event = event
                snapshot.save(
                    update_fields=[
                        "processing_reserved_qty",
                        "available_qty",
                        "warehouse_state_code",
                        "last_event",
                        "updated_at",
                    ]
                )
                reserves.append(reserve)
        return reserves

    @classmethod
    @transaction.atomic
    def request_move_to_processing(
        cls,
        *,
        agency: Agency,
        order_id: str,
        container_codes: list[str] | None = None,
        destination_row_no: int = 0,
        destination_section_no: int = 0,
        destination_tier_no: int = 0,
        destination_cell_no: int = 0,
        requested_by=None,
        requested_by_role: str = "processing_lead",
        warehouse_code: str = "MSK",
        source_document_type: str = "",
        source_document_id: str = "",
    ) -> WarehouseOperation:
        order_key = str(order_id or "").strip()
        snapshot_query = WarehouseStockSnapshot.objects.select_related("location", "container").filter(
            agency=agency,
            warehouse_state_code=WarehouseStateCode.RESERVED_FOR_PROCESSING.value,
            processing_reserved_qty__gt=0,
            is_archived=False,
        )
        normalized_container_codes = [
            str(value or "").strip()
            for value in (container_codes or [])
            if str(value or "").strip()
        ]
        if normalized_container_codes:
            snapshot_query = snapshot_query.filter(
                Q(container_code__in=normalized_container_codes)
                | Q(parent_container__container_code__in=normalized_container_codes)
            )
        snapshots = list(snapshot_query.order_by("id"))
        snapshots = [snapshot for snapshot in snapshots if cls._snapshot_matches_processing_context(snapshot, order_key)]
        if not snapshots:
            raise ValueError("No processing-reserved snapshots ready for move to OBR")

        source_location = snapshots[0].location or cls.ensure_location(warehouse_code=warehouse_code, zone_code="OS")
        destination = cls.ensure_location(
            warehouse_code=warehouse_code,
            zone_code="OBR",
            row_no=destination_row_no,
            section_no=destination_section_no,
            tier_no=destination_tier_no,
            cell_no=destination_cell_no,
        )
        operation = WarehouseOperation.objects.create(
            agency=agency,
            operation_type=WarehouseOperation.TYPE_MOVE_TO_PROCESSING,
            context_type="processing",
            context_id=order_key,
            source_location=source_location,
            destination_location=destination,
            source_document_type=str(source_document_type or "").strip(),
            source_document_id=str(source_document_id or "").strip(),
            source_zone_code=source_location.zone_code,
            destination_zone_code=destination.zone_code,
            status=WarehouseOperation.STATUS_PLANNED,
            requested_by=requested_by,
            requested_by_role=requested_by_role,
            assigned_executor_role="reachtruck",
            planned_qty=sum(int(snapshot.processing_reserved_qty or 0) for snapshot in snapshots),
        )
        WarehouseEvent.objects.create(
            agency=agency,
            event_type=WarehouseEventType.MOVEMENT_REQUESTED.value,
            stock_context_type="processing",
            stock_context_id=order_key,
            operation=operation,
            from_location=source_location,
            to_location=destination,
            from_zone_code=source_location.zone_code,
            to_zone_code=destination.zone_code,
            qty=operation.planned_qty,
            performed_by=requested_by,
            performed_by_role=requested_by_role,
            occurred_at=timezone.now(),
        )
        grouped_tasks: dict[tuple[str, int], dict] = {}
        selected_container_codes = {code.lower() for code in normalized_container_codes}
        for snapshot in snapshots:
            move_container = snapshot.container
            if (
                snapshot.parent_container
                and str(snapshot.parent_container.container_code or "").strip().lower() in selected_container_codes
            ):
                move_container = snapshot.parent_container
            elif (
                snapshot.container
                and str(snapshot.container.container_code or "").strip().lower() in selected_container_codes
            ):
                move_container = snapshot.container
            elif snapshot.parent_container and not selected_container_codes:
                move_container = snapshot.parent_container

            if move_container:
                task_key = ("container", int(move_container.id))
            else:
                task_key = ("snapshot", int(snapshot.id))
            task_bucket = grouped_tasks.setdefault(
                task_key,
                {
                    "container": move_container,
                    "from_location": snapshot.location,
                    "from_zone_code": snapshot.zone_code,
                    "qty_planned": 0,
                    "snapshot_ids": [],
                    "container_code": (
                        move_container.container_code
                        if move_container
                        else snapshot.container_code
                    ),
                },
            )
            task_bucket["qty_planned"] += int(snapshot.processing_reserved_qty or snapshot.qty or 0)
            task_bucket["snapshot_ids"].append(int(snapshot.id))
        for task_bucket in grouped_tasks.values():
            task_container = task_bucket["container"]
            WarehouseOperationTask.objects.create(
                operation=operation,
                task_type=(
                    WarehouseOperationTask.TYPE_PALLET_MOVE
                    if task_container
                    and task_container.container_type
                    in {WarehouseContainer.TYPE_PALLET, WarehouseContainer.TYPE_MIXED_PALLET}
                    else WarehouseOperationTask.TYPE_BOX_MOVE
                ),
                container=task_container,
                from_location=task_bucket["from_location"],
                to_location=destination,
                from_zone_code=task_bucket["from_zone_code"],
                to_zone_code=destination.zone_code,
                qty_planned=int(task_bucket["qty_planned"] or 0),
                status=WarehouseOperationTask.STATUS_CREATED,
                executor_role="reachtruck",
                payload={
                    "snapshot_ids": task_bucket["snapshot_ids"],
                    "container_code": task_bucket["container_code"],
                },
            )
        for snapshot in snapshots:
            snapshot.active_operation = operation
            snapshot.active_operation_type = operation.operation_type
            snapshot.save(update_fields=["active_operation", "active_operation_type", "updated_at"])
        return operation

    @classmethod
    @transaction.atomic
    def start_move_to_processing(
        cls,
        *,
        operation: WarehouseOperation,
        performed_by=None,
    ) -> WarehouseOperation:
        if operation.operation_type != WarehouseOperation.TYPE_MOVE_TO_PROCESSING:
            raise ValueError("Only move_to_processing operations can be started by this write-path")
        now = timezone.now()
        operation.status = WarehouseOperation.STATUS_IN_PROGRESS
        operation.started_at = operation.started_at or now
        operation.save(update_fields=["status", "started_at", "updated_at"])
        for snapshot in WarehouseStockSnapshot.objects.filter(active_operation=operation, is_archived=False).order_by("id"):
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.MOVEMENT_STARTED,
                operation_type=operation.operation_type,
            )
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.MOVEMENT_STARTED.value,
                stock_context_type="processing",
                stock_context_id=operation.context_id,
                container=snapshot.container,
                operation=operation,
                from_location=snapshot.location,
                to_location=operation.destination_location,
                from_zone_code=snapshot.zone_code,
                to_zone_code=operation.destination_zone_code,
                qty=int(snapshot.processing_reserved_qty or snapshot.qty or 0),
                performed_by=performed_by,
                performed_by_role=cls._role_of(performed_by) or "reachtruck",
                occurred_at=now,
            )
            snapshot.warehouse_state_code = transition.code.value
            snapshot.last_event = event
            snapshot.save(update_fields=["warehouse_state_code", "last_event", "updated_at"])
        operation.tasks.update(
            status=WarehouseOperationTask.STATUS_IN_PROGRESS,
            started_at=now,
            updated_at=now,
        )
        return operation

    @classmethod
    @transaction.atomic
    def complete_move_to_processing(
        cls,
        *,
        operation: WarehouseOperation,
        performed_by=None,
    ) -> WarehouseOperation:
        if operation.operation_type != WarehouseOperation.TYPE_MOVE_TO_PROCESSING:
            raise ValueError("Only move_to_processing operations can be completed by this write-path")
        destination = operation.destination_location
        if destination is None:
            raise ValueError("Processing move operation must have destination_location")

        total_done = 0
        for snapshot in WarehouseStockSnapshot.objects.filter(active_operation=operation, is_archived=False).order_by("id"):
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.PROCESSING_ZONE_ARRIVED,
                zone_to=destination.zone_code,
            )
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.PROCESSING_ZONE_ARRIVED.value,
                stock_context_type="processing",
                stock_context_id=operation.context_id,
                container=snapshot.container,
                operation=operation,
                from_location=snapshot.location,
                to_location=destination,
                from_zone_code=snapshot.zone_code,
                to_zone_code=destination.zone_code,
                qty=int(snapshot.processing_reserved_qty or snapshot.qty or 0),
                performed_by=performed_by,
                performed_by_role=cls._role_of(performed_by) or "reachtruck",
                occurred_at=timezone.now(),
            )
            snapshot.location = destination
            snapshot.zone_code = destination.zone_code
            snapshot.zone_kind = destination.zone_kind
            snapshot.warehouse_state_code = transition.code.value
            snapshot.last_event = event
            snapshot.save(
                update_fields=[
                    "location",
                    "zone_code",
                    "zone_kind",
                    "warehouse_state_code",
                    "last_event",
                    "updated_at",
                ]
            )
            total_done += int(snapshot.processing_reserved_qty or snapshot.qty or 0)
        operation.status = WarehouseOperation.STATUS_DONE
        operation.done_qty = total_done
        operation.completed_at = timezone.now()
        operation.save(update_fields=["status", "done_qty", "completed_at", "updated_at"])
        operation.tasks.update(
            status=WarehouseOperationTask.STATUS_DONE,
            qty_done=models.F("qty_planned"),
            completed_at=timezone.now(),
            updated_at=timezone.now(),
        )
        return operation

    @classmethod
    @transaction.atomic
    def start_processing(
        cls,
        *,
        agency: Agency,
        order_id: str,
        started_by=None,
        started_by_role: str = "processor",
    ) -> WarehouseOperation:
        order_key = str(order_id or "").strip()
        snapshots = list(
            WarehouseStockSnapshot.objects.filter(
                agency=agency,
                warehouse_state_code=WarehouseStateCode.IN_PROCESSING_ZONE.value,
                processing_reserved_qty__gt=0,
                is_archived=False,
            ).order_by("id")
        )
        snapshots = [snapshot for snapshot in snapshots if cls._snapshot_matches_processing_context(snapshot, order_key)]
        if not snapshots:
            raise ValueError("No snapshots in processing zone ready to start processing")

        operation = WarehouseOperation.objects.create(
            agency=agency,
            operation_type=WarehouseOperation.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_key,
            source_location=snapshots[0].location,
            destination_location=snapshots[0].location,
            source_zone_code=snapshots[0].zone_code,
            destination_zone_code=snapshots[0].zone_code,
            status=WarehouseOperation.STATUS_IN_PROGRESS,
            requested_by=started_by,
            requested_by_role=started_by_role,
            assigned_executor_role="processor",
            planned_qty=sum(int(snapshot.processing_reserved_qty or snapshot.qty or 0) for snapshot in snapshots),
            started_at=timezone.now(),
        )
        now = timezone.now()
        for snapshot in snapshots:
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.PROCESSING_STARTED,
            )
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.PROCESSING_STARTED.value,
                stock_context_type="processing",
                stock_context_id=order_key,
                container=snapshot.container,
                operation=operation,
                from_location=snapshot.location,
                to_location=snapshot.location,
                from_zone_code=snapshot.zone_code,
                to_zone_code=snapshot.zone_code,
                qty=int(snapshot.processing_reserved_qty or snapshot.qty or 0),
                performed_by=started_by,
                performed_by_role=started_by_role or cls._role_of(started_by),
                occurred_at=now,
            )
            snapshot.warehouse_state_code = transition.code.value
            snapshot.active_operation = operation
            snapshot.active_operation_type = operation.operation_type
            snapshot.last_event = event
            snapshot.save(
                update_fields=[
                    "warehouse_state_code",
                    "active_operation",
                    "active_operation_type",
                    "last_event",
                    "updated_at",
                ]
            )
        return operation

    @classmethod
    @transaction.atomic
    def complete_processing(
        cls,
        *,
        operation: WarehouseOperation,
        performed_by=None,
        performed_by_role: str = "processor",
    ) -> WarehouseOperation:
        if operation.operation_type != WarehouseOperation.TYPE_PROCESSING:
            raise ValueError("Only processing operations can be completed by this write-path")
        total_done = 0
        snapshots = list(WarehouseStockSnapshot.objects.filter(active_operation=operation, is_archived=False).order_by("id"))
        for snapshot in snapshots:
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.PROCESSING_COMPLETED,
            )
            reserve = WarehouseReserve.objects.filter(
                agency=snapshot.agency,
                reserve_type=WarehouseReserve.TYPE_PROCESSING,
                context_type="processing",
                context_id=operation.context_id,
                sku_code=snapshot.sku_code,
                size=snapshot.size,
                barcode=snapshot.barcode,
                goods_type=snapshot.goods_type,
                status__in=[
                    WarehouseReserve.STATUS_ACTIVE,
                    WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
                    WarehouseReserve.STATUS_ALLOCATED,
                    WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
                ],
            ).order_by("id").first()
            qty_done = int(snapshot.processing_reserved_qty or snapshot.qty or 0)
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.PROCESSING_COMPLETED.value,
                stock_context_type="processing",
                stock_context_id=operation.context_id,
                container=snapshot.container,
                operation=operation,
                reserve=reserve,
                from_location=snapshot.location,
                to_location=snapshot.location,
                from_zone_code=snapshot.zone_code,
                to_zone_code=snapshot.zone_code,
                qty=qty_done,
                performed_by=performed_by,
                performed_by_role=performed_by_role or cls._role_of(performed_by),
                occurred_at=timezone.now(),
            )
            if reserve:
                reserve.qty_allocated = max(int(reserve.qty_allocated or 0), qty_done)
                reserve.qty_satisfied = min(int(reserve.qty_reserved or 0), max(int(reserve.qty_satisfied or 0), qty_done))
                reserve.status = WarehouseReserve.STATUS_SATISFIED
                reserve.save(update_fields=["qty_allocated", "qty_satisfied", "status", "updated_at"])
            snapshot.processing_reserved_qty = 0
            snapshot.available_qty = int(snapshot.qty or 0)
            snapshot.warehouse_state_code = transition.code.value
            snapshot.active_operation = None
            snapshot.active_operation_type = ""
            snapshot.last_event = event
            snapshot.save(
                update_fields=[
                    "processing_reserved_qty",
                    "available_qty",
                    "warehouse_state_code",
                    "active_operation",
                    "active_operation_type",
                    "last_event",
                    "updated_at",
                ]
            )
            total_done += qty_done
        operation.status = WarehouseOperation.STATUS_DONE
        operation.done_qty = total_done
        operation.completed_at = timezone.now()
        operation.save(update_fields=["status", "done_qty", "completed_at", "updated_at"])
        return operation

    @classmethod
    @transaction.atomic
    def start_processing_if_ready(
        cls,
        *,
        agency: Agency,
        order_id: str,
        started_by=None,
        started_by_role: str = "processor",
    ) -> WarehouseOperation | None:
        order_key = str(order_id or "").strip()
        if not order_key:
            return None
        existing = (
            WarehouseOperation.objects.filter(
                agency=agency,
                operation_type=WarehouseOperation.TYPE_PROCESSING,
                context_type="processing",
                context_id=order_key,
                status__in=[
                    WarehouseOperation.STATUS_CREATED,
                    WarehouseOperation.STATUS_PLANNED,
                    WarehouseOperation.STATUS_IN_PROGRESS,
                    WarehouseOperation.STATUS_PARTIAL,
                    WarehouseOperation.STATUS_DONE,
                ],
            )
            .order_by("-id")
            .first()
        )
        if existing:
            return existing
        try:
            return cls.start_processing(
                agency=agency,
                order_id=order_key,
                started_by=started_by,
                started_by_role=started_by_role,
            )
        except ValueError:
            return None

    @classmethod
    @transaction.atomic
    def complete_processing_if_started(
        cls,
        *,
        agency: Agency,
        order_id: str,
        performed_by=None,
        performed_by_role: str = "processor",
    ) -> WarehouseOperation | None:
        order_key = str(order_id or "").strip()
        if not order_key:
            return None
        operation = (
            WarehouseOperation.objects.filter(
                agency=agency,
                operation_type=WarehouseOperation.TYPE_PROCESSING,
                context_type="processing",
                context_id=order_key,
                status__in=[
                    WarehouseOperation.STATUS_IN_PROGRESS,
                    WarehouseOperation.STATUS_PARTIAL,
                ],
            )
            .order_by("-id")
            .first()
        )
        if operation is None:
            operation = cls.start_processing_if_ready(
                agency=agency,
                order_id=order_key,
                started_by=performed_by,
                started_by_role=performed_by_role,
            )
        if operation is None or operation.status == WarehouseOperation.STATUS_DONE:
            return operation
        try:
            return cls.complete_processing(
                operation=operation,
                performed_by=performed_by,
                performed_by_role=performed_by_role,
            )
        except ValueError:
            return None

    @classmethod
    @transaction.atomic
    def replace_shipping_reserves(
        cls,
        *,
        agency: Agency,
        order_id: str,
        items: list[dict],
        created_by=None,
        source_document_type: str = "shipping_order",
        source_document_id: str = "",
    ) -> list[WarehouseReserve]:
        order_key = str(order_id or "").strip()
        if not order_key:
            return []
        active_reserves = list(
            WarehouseReserve.objects.filter(
                agency=agency,
                reserve_type=WarehouseReserve.TYPE_SHIPPING,
                context_type="shipping",
                context_id=order_key,
                status__in=[
                    WarehouseReserve.STATUS_ACTIVE,
                    WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
                    WarehouseReserve.STATUS_ALLOCATED,
                ],
            ).order_by("id")
        )
        cls._release_reserves_from_snapshots(
            reserves=active_reserves,
            reserved_qty_field="shipping_reserved_qty",
            release_event_type=WarehouseEventType.SHIPPING_RESERVE_RELEASED,
            performed_by=created_by,
        )
        if active_reserves:
            WarehouseReserve.objects.filter(id__in=[reserve.id for reserve in active_reserves]).update(
                status=WarehouseReserve.STATUS_RELEASED,
                released_by=created_by if getattr(created_by, "is_authenticated", False) else None,
                updated_at=timezone.now(),
            )
        if not items:
            return []
        return cls.reserve_for_shipping(
            agency=agency,
            order_id=order_key,
            items=items,
            created_by=created_by,
            source_document_type=source_document_type,
            source_document_id=source_document_id,
        )

    @classmethod
    @transaction.atomic
    def reserve_for_shipping(
        cls,
        *,
        agency: Agency,
        order_id: str,
        items: list[dict],
        created_by=None,
        source_document_type: str = "shipping_order",
        source_document_id: str = "",
    ) -> list[WarehouseReserve]:
        order_key = str(order_id or "").strip()
        if not order_key:
            raise ValueError("order_id is required for shipping reserve")
        source_document_id = str(source_document_id or order_key).strip()
        reserves: list[WarehouseReserve] = []
        for item in items:
            qty = max(int(item.get("qty") or 0), 0)
            if qty <= 0:
                continue
            allocations = cls._match_snapshots_for_shipping_reserve(
                agency=agency,
                item=item,
                required_qty=qty,
            )
            for snapshot, reserved_qty in allocations:
                transition = WarehouseTransitionService.apply_event(
                    snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                    WarehouseEventType.SHIPPING_RESERVED,
                )
                reserve = WarehouseReserve.objects.create(
                    agency=agency,
                    reserve_type=WarehouseReserve.TYPE_SHIPPING,
                    context_type="shipping",
                    context_id=order_key,
                    sku_ref=snapshot.sku_ref,
                    sku_code=snapshot.sku_code,
                    size=snapshot.size,
                    barcode=snapshot.barcode,
                    goods_type=snapshot.goods_type,
                    marking_code=snapshot.marking_code,
                    qty_reserved=reserved_qty,
                    status=WarehouseReserve.STATUS_ACTIVE,
                    source_document_type=source_document_type,
                    source_document_id=source_document_id,
                    created_by=created_by,
                )
                event = WarehouseEvent.objects.create(
                    agency=agency,
                    event_type=WarehouseEventType.SHIPPING_RESERVED.value,
                    stock_context_type="shipping",
                    stock_context_id=order_key,
                    container=snapshot.container,
                    reserve=reserve,
                    source_document_type=source_document_type,
                    source_document_id=source_document_id,
                    from_location=snapshot.location,
                    to_location=snapshot.location,
                    from_zone_code=snapshot.zone_code,
                    to_zone_code=snapshot.zone_code,
                    qty=reserved_qty,
                    performed_by=created_by,
                    performed_by_role=cls._role_of(created_by),
                    occurred_at=timezone.now(),
                    payload={"snapshot_id": snapshot.id},
                )
                snapshot.shipping_reserved_qty += reserved_qty
                snapshot.available_qty -= reserved_qty
                snapshot.warehouse_state_code = transition.code.value
                snapshot.last_event = event
                snapshot.save(
                    update_fields=[
                        "shipping_reserved_qty",
                        "available_qty",
                        "warehouse_state_code",
                        "last_event",
                        "updated_at",
                    ]
                )
                reserves.append(reserve)
        return reserves

    @classmethod
    @transaction.atomic
    def request_move_to_otg(
        cls,
        *,
        agency: Agency,
        order_id: str,
        destination_row_no: int = 0,
        destination_section_no: int = 0,
        destination_tier_no: int = 0,
        destination_cell_no: int = 0,
        requested_by=None,
        requested_by_role: str = "storekeeper",
        warehouse_code: str = "MSK",
    ) -> WarehouseOperation:
        order_key = str(order_id or "").strip()
        snapshots = list(
            WarehouseStockSnapshot.objects.select_related("location", "container")
            .filter(
                agency=agency,
                warehouse_state_code=WarehouseStateCode.RESERVED_FOR_SHIPPING.value,
                shipping_reserved_qty__gt=0,
                is_archived=False,
            )
            .order_by("id")
        )
        snapshots = [snapshot for snapshot in snapshots if cls._snapshot_matches_shipping_context(snapshot, order_key)]
        if not snapshots:
            raise ValueError("No shipping-reserved snapshots ready for move to OTG")

        source_location = snapshots[0].location or cls.ensure_location(warehouse_code=warehouse_code, zone_code="OS")
        destination = cls.ensure_location(
            warehouse_code=warehouse_code,
            zone_code="OTG",
            row_no=destination_row_no,
            section_no=destination_section_no,
            tier_no=destination_tier_no,
            cell_no=destination_cell_no,
        )
        operation = WarehouseOperation.objects.create(
            agency=agency,
            operation_type=WarehouseOperation.TYPE_MOVE_TO_OTG,
            context_type="shipping",
            context_id=order_key,
            source_location=source_location,
            destination_location=destination,
            source_zone_code=source_location.zone_code,
            destination_zone_code=destination.zone_code,
            status=WarehouseOperation.STATUS_PLANNED,
            requested_by=requested_by,
            requested_by_role=requested_by_role,
            assigned_executor_role="reachtruck",
            planned_qty=sum(int(snapshot.shipping_reserved_qty or 0) for snapshot in snapshots),
        )
        WarehouseEvent.objects.create(
            agency=agency,
            event_type=WarehouseEventType.OTG_REQUESTED.value,
            stock_context_type="shipping",
            stock_context_id=order_key,
            operation=operation,
            from_location=source_location,
            to_location=destination,
            from_zone_code=source_location.zone_code,
            to_zone_code=destination.zone_code,
            qty=operation.planned_qty,
            performed_by=requested_by,
            performed_by_role=requested_by_role,
            occurred_at=timezone.now(),
        )
        for snapshot in snapshots:
            WarehouseOperationTask.objects.create(
                operation=operation,
                task_type=WarehouseOperationTask.TYPE_PALLET_MOVE if snapshot.container_id else WarehouseOperationTask.TYPE_BOX_MOVE,
                container=snapshot.container,
                from_location=snapshot.location,
                to_location=destination,
                from_zone_code=snapshot.zone_code,
                to_zone_code=destination.zone_code,
                qty_planned=int(snapshot.shipping_reserved_qty or 0),
                status=WarehouseOperationTask.STATUS_CREATED,
                executor_role="reachtruck",
            )
            snapshot.active_operation = operation
            snapshot.active_operation_type = operation.operation_type
            snapshot.save(update_fields=["active_operation", "active_operation_type", "updated_at"])
        return operation

    @classmethod
    @transaction.atomic
    def start_move_to_otg(
        cls,
        *,
        operation: WarehouseOperation,
        performed_by=None,
    ) -> WarehouseOperation:
        if operation.operation_type != WarehouseOperation.TYPE_MOVE_TO_OTG:
            raise ValueError("Only move_to_otg operations can be started by this write-path")
        now = timezone.now()
        operation.status = WarehouseOperation.STATUS_IN_PROGRESS
        operation.started_at = operation.started_at or now
        operation.save(update_fields=["status", "started_at", "updated_at"])
        for snapshot in WarehouseStockSnapshot.objects.filter(active_operation=operation, is_archived=False).order_by("id"):
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.MOVEMENT_STARTED,
                operation_type=operation.operation_type,
            )
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.MOVEMENT_STARTED.value,
                stock_context_type="shipping",
                stock_context_id=operation.context_id,
                container=snapshot.container,
                operation=operation,
                from_location=snapshot.location,
                to_location=operation.destination_location,
                from_zone_code=snapshot.zone_code,
                to_zone_code=operation.destination_zone_code,
                qty=int(snapshot.shipping_reserved_qty or snapshot.qty or 0),
                performed_by=performed_by,
                performed_by_role=cls._role_of(performed_by) or "reachtruck",
                occurred_at=now,
            )
            snapshot.warehouse_state_code = transition.code.value
            snapshot.last_event = event
            snapshot.save(update_fields=["warehouse_state_code", "last_event", "updated_at"])
        operation.tasks.update(
            status=WarehouseOperationTask.STATUS_IN_PROGRESS,
            started_at=now,
            updated_at=now,
        )
        return operation

    @classmethod
    @transaction.atomic
    def complete_move_to_otg(
        cls,
        *,
        operation: WarehouseOperation,
        performed_by=None,
    ) -> WarehouseOperation:
        if operation.operation_type != WarehouseOperation.TYPE_MOVE_TO_OTG:
            raise ValueError("Only move_to_otg operations can be completed by this write-path")
        destination = operation.destination_location
        if destination is None:
            raise ValueError("OTG move operation must have destination_location")
        total_done = 0
        for snapshot in WarehouseStockSnapshot.objects.filter(active_operation=operation, is_archived=False).order_by("id"):
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.OTG_ARRIVED,
                zone_to=destination.zone_code,
            )
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.OTG_ARRIVED.value,
                stock_context_type="shipping",
                stock_context_id=operation.context_id,
                container=snapshot.container,
                operation=operation,
                from_location=snapshot.location,
                to_location=destination,
                from_zone_code=snapshot.zone_code,
                to_zone_code=destination.zone_code,
                qty=int(snapshot.shipping_reserved_qty or snapshot.qty or 0),
                performed_by=performed_by,
                performed_by_role=cls._role_of(performed_by) or "reachtruck",
                occurred_at=timezone.now(),
            )
            snapshot.location = destination
            snapshot.zone_code = destination.zone_code
            snapshot.zone_kind = destination.zone_kind
            snapshot.warehouse_state_code = transition.code.value
            snapshot.active_operation = None
            snapshot.active_operation_type = ""
            snapshot.last_event = event
            snapshot.save(
                update_fields=[
                    "location",
                    "zone_code",
                    "zone_kind",
                    "warehouse_state_code",
                    "active_operation",
                    "active_operation_type",
                    "last_event",
                    "updated_at",
                ]
            )
            total_done += int(snapshot.shipping_reserved_qty or snapshot.qty or 0)
        operation.status = WarehouseOperation.STATUS_DONE
        operation.done_qty = total_done
        operation.completed_at = timezone.now()
        operation.save(update_fields=["status", "done_qty", "completed_at", "updated_at"])
        operation.tasks.update(
            status=WarehouseOperationTask.STATUS_DONE,
            qty_done=models.F("qty_planned"),
            completed_at=timezone.now(),
            updated_at=timezone.now(),
        )
        return operation

    @classmethod
    @transaction.atomic
    def start_palletization(
        cls,
        *,
        agency: Agency,
        order_id: str,
        started_by=None,
        started_by_role: str = "storekeeper",
    ) -> WarehouseOperation:
        order_key = str(order_id or "").strip()
        snapshots = list(
            WarehouseStockSnapshot.objects.filter(
                agency=agency,
                warehouse_state_code=WarehouseStateCode.IN_OTG.value,
                shipping_reserved_qty__gt=0,
                is_archived=False,
            ).order_by("id")
        )
        snapshots = [snapshot for snapshot in snapshots if cls._snapshot_matches_shipping_context(snapshot, order_key)]
        if not snapshots:
            raise ValueError("No snapshots in OTG ready for palletization")
        operation = WarehouseOperation.objects.create(
            agency=agency,
            operation_type=WarehouseOperation.TYPE_PALLETIZATION,
            context_type="shipping",
            context_id=order_key,
            source_location=snapshots[0].location,
            destination_location=snapshots[0].location,
            source_zone_code=snapshots[0].zone_code,
            destination_zone_code=snapshots[0].zone_code,
            status=WarehouseOperation.STATUS_IN_PROGRESS,
            requested_by=started_by,
            requested_by_role=started_by_role,
            assigned_executor_role="storekeeper",
            planned_qty=sum(int(snapshot.shipping_reserved_qty or snapshot.qty or 0) for snapshot in snapshots),
            started_at=timezone.now(),
        )
        now = timezone.now()
        for snapshot in snapshots:
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.PALLETIZATION_STARTED,
            )
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.PALLETIZATION_STARTED.value,
                stock_context_type="shipping",
                stock_context_id=order_key,
                container=snapshot.container,
                operation=operation,
                from_location=snapshot.location,
                to_location=snapshot.location,
                from_zone_code=snapshot.zone_code,
                to_zone_code=snapshot.zone_code,
                qty=int(snapshot.shipping_reserved_qty or snapshot.qty or 0),
                performed_by=started_by,
                performed_by_role=started_by_role or cls._role_of(started_by),
                occurred_at=now,
            )
            snapshot.warehouse_state_code = transition.code.value
            snapshot.active_operation = operation
            snapshot.active_operation_type = operation.operation_type
            snapshot.last_event = event
            snapshot.save(
                update_fields=[
                    "warehouse_state_code",
                    "active_operation",
                    "active_operation_type",
                    "last_event",
                    "updated_at",
                ]
            )
        return operation

    @classmethod
    @transaction.atomic
    def complete_palletization(
        cls,
        *,
        operation: WarehouseOperation,
        performed_by=None,
        performed_by_role: str = "storekeeper",
    ) -> WarehouseOperation:
        if operation.operation_type != WarehouseOperation.TYPE_PALLETIZATION:
            raise ValueError("Only palletization operations can be completed by this write-path")
        total_done = 0
        for snapshot in WarehouseStockSnapshot.objects.filter(active_operation=operation, is_archived=False).order_by("id"):
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.PALLETIZATION_COMPLETED,
            )
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.PALLETIZATION_COMPLETED.value,
                stock_context_type="shipping",
                stock_context_id=operation.context_id,
                container=snapshot.container,
                operation=operation,
                from_location=snapshot.location,
                to_location=snapshot.location,
                from_zone_code=snapshot.zone_code,
                to_zone_code=snapshot.zone_code,
                qty=int(snapshot.shipping_reserved_qty or snapshot.qty or 0),
                performed_by=performed_by,
                performed_by_role=performed_by_role or cls._role_of(performed_by),
                occurred_at=timezone.now(),
            )
            snapshot.warehouse_state_code = transition.code.value
            snapshot.active_operation = None
            snapshot.active_operation_type = ""
            snapshot.last_event = event
            snapshot.save(
                update_fields=[
                    "warehouse_state_code",
                    "active_operation",
                    "active_operation_type",
                    "last_event",
                    "updated_at",
                ]
            )
            total_done += int(snapshot.shipping_reserved_qty or snapshot.qty or 0)
        operation.status = WarehouseOperation.STATUS_DONE
        operation.done_qty = total_done
        operation.completed_at = timezone.now()
        operation.save(update_fields=["status", "done_qty", "completed_at", "updated_at"])
        return operation

    @classmethod
    @transaction.atomic
    def assign_to_trip(
        cls,
        *,
        agency: Agency,
        order_id: str,
        trip_id: str,
        assigned_by=None,
        assigned_by_role: str = "logistician",
    ) -> list[int]:
        order_key = str(order_id or "").strip()
        trip_key = str(trip_id or "").strip()
        snapshots = list(
            WarehouseStockSnapshot.objects.filter(
                agency=agency,
                warehouse_state_code=WarehouseStateCode.READY_FOR_LOADING.value,
                shipping_reserved_qty__gt=0,
                is_archived=False,
            ).order_by("id")
        )
        snapshots = [snapshot for snapshot in snapshots if cls._snapshot_matches_shipping_context(snapshot, order_key)]
        if not snapshots:
            raise ValueError("No snapshots ready for trip assignment")
        event_ids: list[int] = []
        for snapshot in snapshots:
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.ASSIGNED_TO_TRIP,
            )
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.ASSIGNED_TO_TRIP.value,
                stock_context_type="shipping",
                stock_context_id=order_key,
                container=snapshot.container,
                from_location=snapshot.location,
                to_location=snapshot.location,
                from_zone_code=snapshot.zone_code,
                to_zone_code=snapshot.zone_code,
                qty=int(snapshot.shipping_reserved_qty or snapshot.qty or 0),
                performed_by=assigned_by,
                performed_by_role=assigned_by_role or cls._role_of(assigned_by),
                occurred_at=timezone.now(),
                payload={"trip_id": trip_key},
            )
            snapshot.warehouse_state_code = transition.code.value
            snapshot.current_trip_id = trip_key
            snapshot.last_event = event
            snapshot.save(update_fields=["warehouse_state_code", "current_trip_id", "last_event", "updated_at"])
            event_ids.append(event.id)
        return event_ids

    @classmethod
    @transaction.atomic
    def start_loading(
        cls,
        *,
        agency: Agency,
        order_id: str,
        trip_id: str,
        started_by=None,
        started_by_role: str = "logistician",
    ) -> WarehouseOperation:
        order_key = str(order_id or "").strip()
        trip_key = str(trip_id or "").strip()
        snapshots = list(
            WarehouseStockSnapshot.objects.filter(
                agency=agency,
                warehouse_state_code=WarehouseStateCode.ASSIGNED_TO_TRIP.value,
                current_trip_id=trip_key,
                is_archived=False,
            ).order_by("id")
        )
        snapshots = [snapshot for snapshot in snapshots if cls._snapshot_matches_shipping_context(snapshot, order_key)]
        if not snapshots:
            raise ValueError("No assigned snapshots ready for loading")
        operation = WarehouseOperation.objects.create(
            agency=agency,
            operation_type=WarehouseOperation.TYPE_LOAD_TO_VEHICLE,
            context_type="shipping",
            context_id=order_key,
            source_location=snapshots[0].location,
            destination_location=snapshots[0].location,
            source_zone_code=snapshots[0].zone_code,
            destination_zone_code="VEH",
            status=WarehouseOperation.STATUS_IN_PROGRESS,
            requested_by=started_by,
            requested_by_role=started_by_role,
            assigned_executor_role="logistician",
            planned_qty=sum(int(snapshot.shipping_reserved_qty or snapshot.qty or 0) for snapshot in snapshots),
            started_at=timezone.now(),
            comment=f"trip:{trip_key}",
        )
        now = timezone.now()
        for snapshot in snapshots:
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.LOADING_STARTED,
            )
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.LOADING_STARTED.value,
                stock_context_type="shipping",
                stock_context_id=order_key,
                container=snapshot.container,
                operation=operation,
                from_location=snapshot.location,
                to_location=snapshot.location,
                from_zone_code=snapshot.zone_code,
                to_zone_code="VEH",
                qty=int(snapshot.shipping_reserved_qty or snapshot.qty or 0),
                performed_by=started_by,
                performed_by_role=started_by_role or cls._role_of(started_by),
                occurred_at=now,
                payload={"trip_id": trip_key},
            )
            snapshot.warehouse_state_code = transition.code.value
            snapshot.active_operation = operation
            snapshot.active_operation_type = operation.operation_type
            snapshot.last_event = event
            snapshot.save(
                update_fields=[
                    "warehouse_state_code",
                    "active_operation",
                    "active_operation_type",
                    "last_event",
                    "updated_at",
                ]
            )
        return operation

    @classmethod
    @transaction.atomic
    def complete_loading(
        cls,
        *,
        operation: WarehouseOperation,
        performed_by=None,
        performed_by_role: str = "logistician",
    ) -> WarehouseOperation:
        if operation.operation_type != WarehouseOperation.TYPE_LOAD_TO_VEHICLE:
            raise ValueError("Only load_to_vehicle operations can be completed by this write-path")
        total_done = 0
        for snapshot in WarehouseStockSnapshot.objects.filter(active_operation=operation, is_archived=False).order_by("id"):
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.LOADED_TO_VEHICLE,
            )
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.LOADED_TO_VEHICLE.value,
                stock_context_type="shipping",
                stock_context_id=operation.context_id,
                container=snapshot.container,
                operation=operation,
                from_location=snapshot.location,
                to_location=snapshot.location,
                from_zone_code=snapshot.zone_code,
                to_zone_code="VEH",
                qty=int(snapshot.shipping_reserved_qty or snapshot.qty or 0),
                performed_by=performed_by,
                performed_by_role=performed_by_role or cls._role_of(performed_by),
                occurred_at=timezone.now(),
                payload={"trip_id": snapshot.current_trip_id},
            )
            snapshot.warehouse_state_code = transition.code.value
            snapshot.is_in_vehicle = True
            snapshot.active_operation = None
            snapshot.active_operation_type = ""
            snapshot.last_event = event
            snapshot.save(
                update_fields=[
                    "warehouse_state_code",
                    "is_in_vehicle",
                    "active_operation",
                    "active_operation_type",
                    "last_event",
                    "updated_at",
                ]
            )
            total_done += int(snapshot.shipping_reserved_qty or snapshot.qty or 0)
        operation.status = WarehouseOperation.STATUS_DONE
        operation.done_qty = total_done
        operation.completed_at = timezone.now()
        operation.save(update_fields=["status", "done_qty", "completed_at", "updated_at"])
        return operation

    @classmethod
    @transaction.atomic
    def ship_order(
        cls,
        *,
        agency: Agency,
        order_id: str,
        trip_id: str,
        performed_by=None,
        performed_by_role: str = "logistician",
    ) -> list[int]:
        order_key = str(order_id or "").strip()
        trip_key = str(trip_id or "").strip()
        snapshots = list(
            WarehouseStockSnapshot.objects.filter(
                agency=agency,
                warehouse_state_code=WarehouseStateCode.LOADED_TO_VEHICLE.value,
                current_trip_id=trip_key,
                is_archived=False,
            ).order_by("id")
        )
        snapshots = [snapshot for snapshot in snapshots if cls._snapshot_matches_shipping_context(snapshot, order_key)]
        if not snapshots:
            raise ValueError("No loaded snapshots ready for shipping")
        event_ids: list[int] = []
        for snapshot in snapshots:
            reserve = WarehouseReserve.objects.filter(
                agency=snapshot.agency,
                reserve_type=WarehouseReserve.TYPE_SHIPPING,
                context_type="shipping",
                context_id=order_key,
                sku_code=snapshot.sku_code,
                size=snapshot.size,
                barcode=snapshot.barcode,
                goods_type=snapshot.goods_type,
                status__in=[
                    WarehouseReserve.STATUS_ACTIVE,
                    WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
                    WarehouseReserve.STATUS_ALLOCATED,
                    WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
                    WarehouseReserve.STATUS_SATISFIED,
                ],
            ).order_by("id").first()
            transition = WarehouseTransitionService.apply_event(
                snapshot.warehouse_state_code or WarehouseStateCode.UNKNOWN.value,
                WarehouseEventType.SHIPPED,
            )
            event = WarehouseEvent.objects.create(
                agency=snapshot.agency,
                event_type=WarehouseEventType.SHIPPED.value,
                stock_context_type="shipping",
                stock_context_id=order_key,
                container=snapshot.container,
                reserve=reserve,
                from_location=snapshot.location,
                to_location=snapshot.location,
                from_zone_code=snapshot.zone_code,
                to_zone_code="VEH",
                qty=int(snapshot.shipping_reserved_qty or snapshot.qty or 0),
                performed_by=performed_by,
                performed_by_role=performed_by_role or cls._role_of(performed_by),
                occurred_at=timezone.now(),
                payload={"trip_id": trip_key},
            )
            if reserve:
                reserve.qty_allocated = max(int(reserve.qty_allocated or 0), int(snapshot.shipping_reserved_qty or 0))
                reserve.qty_satisfied = min(int(reserve.qty_reserved or 0), int(snapshot.shipping_reserved_qty or 0))
                reserve.status = WarehouseReserve.STATUS_SATISFIED
                reserve.save(update_fields=["qty_allocated", "qty_satisfied", "status", "updated_at"])
            snapshot.shipping_reserved_qty = 0
            snapshot.available_qty = 0
            snapshot.warehouse_state_code = transition.code.value
            snapshot.is_archived = True
            snapshot.last_event = event
            snapshot.save(
                update_fields=[
                    "shipping_reserved_qty",
                    "available_qty",
                    "warehouse_state_code",
                    "is_archived",
                    "last_event",
                    "updated_at",
                ]
            )
            event_ids.append(event.id)
        return event_ids

    @classmethod
    def ensure_location(
        cls,
        *,
        warehouse_code: str = "MSK",
        zone_code: str,
        row_no: int = 0,
        section_no: int = 0,
        tier_no: int = 0,
        cell_no: int = 0,
    ) -> WarehouseLocation:
        zone = str(zone_code or "").strip().upper()
        kind = ZONE_KIND_BY_CODE.get(zone, WarehouseLocation.ZONE_KIND_VIRTUAL)
        defaults = {
            "zone_kind": kind,
            "location_code": cls._location_code(zone, row_no, section_no, tier_no, cell_no),
            "display_name": cls._location_display_name(zone, row_no, section_no, tier_no, cell_no),
            "is_active": True,
            "is_pickable": kind in {WarehouseLocation.ZONE_KIND_STORAGE, WarehouseLocation.ZONE_KIND_SHIPPING},
            "is_storage": kind == WarehouseLocation.ZONE_KIND_STORAGE,
            "is_processing": kind == WarehouseLocation.ZONE_KIND_PROCESSING,
            "is_shipping": kind == WarehouseLocation.ZONE_KIND_SHIPPING,
            "is_loading": kind == WarehouseLocation.ZONE_KIND_LOADING,
        }
        location, _ = WarehouseLocation.objects.get_or_create(
            warehouse_code=warehouse_code,
            zone_code=zone,
            row_no=max(int(row_no or 0), 0),
            section_no=max(int(section_no or 0), 0),
            tier_no=max(int(tier_no or 0), 0),
            cell_no=max(int(cell_no or 0), 0),
            defaults=defaults,
        )
        return location

    @classmethod
    def _resolve_container(cls, *, agency: Agency, item: dict, current_location: WarehouseLocation, performed_by=None):
        pallet_code = str(item.get("pallet_code") or "").strip()
        box_code = str(item.get("box_code") or "").strip()
        order_key = str(item.get("order_id") or "").strip()
        if not pallet_code and not box_code:
            return None

        def ensure_container(
            *,
            container_code: str,
            container_type: str,
            parent_container: WarehouseContainer | None = None,
        ) -> WarehouseContainer:
            container, _ = WarehouseContainer.objects.get_or_create(
                agency=agency,
                container_code=container_code,
                defaults={
                    "container_type": container_type,
                    "parent_container": parent_container,
                    "current_location": current_location,
                    "created_by": performed_by,
                    "source_context_type": "receiving",
                    "source_context_id": order_key,
                },
            )
            update_fields: list[str] = []
            if container.current_location_id != current_location.id:
                container.current_location = current_location
                update_fields.append("current_location")
            if parent_container is not None and container.parent_container_id != parent_container.id:
                container.parent_container = parent_container
                update_fields.append("parent_container")
            if not container.source_context_type:
                container.source_context_type = "receiving"
                update_fields.append("source_context_type")
            if order_key and not container.source_context_id:
                container.source_context_id = order_key
                update_fields.append("source_context_id")
            if update_fields:
                container.save(update_fields=[*update_fields, "updated_at"])
            return container

        pallet_container = None
        if pallet_code:
            pallet_container = ensure_container(
                container_code=pallet_code,
                container_type=WarehouseContainer.TYPE_PALLET,
            )
        if box_code:
            return ensure_container(
                container_code=box_code,
                container_type=WarehouseContainer.TYPE_BOX,
                parent_container=pallet_container,
            )
        return pallet_container

    @staticmethod
    def _resolve_sku_ref(*, agency: Agency, item: dict):
        sku_ref = item.get("sku_ref")
        if isinstance(sku_ref, SKU):
            return sku_ref
        sku_code = str(item.get("sku_code") or item.get("sku") or "").strip()
        if not sku_code:
            return None
        return SKU.objects.filter(agency=agency, sku_code=sku_code, deleted=False).first()

    @staticmethod
    def _snapshot_matches_processing_context(snapshot: WarehouseStockSnapshot, order_id: str) -> bool:
        return WarehouseReserve.objects.filter(
            agency=snapshot.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
            sku_code=snapshot.sku_code,
            size=snapshot.size,
            barcode=snapshot.barcode,
            goods_type=snapshot.goods_type,
            status__in=[
                WarehouseReserve.STATUS_ACTIVE,
                WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
                WarehouseReserve.STATUS_ALLOCATED,
                WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
                WarehouseReserve.STATUS_SATISFIED,
            ],
        ).exists()

    @staticmethod
    def _normalize_reserve_lookup_text(value) -> str:
        text = str(value or "").strip()
        if text in {"-", "–", "—"}:
            return ""
        return text

    @staticmethod
    def _match_snapshot_for_processing_reserve(*, agency: Agency, item: dict, required_qty: int) -> WarehouseStockSnapshot:
        normalize = WarehouseWritePathService._normalize_reserve_lookup_text
        sku_code = str(item.get("sku_code") or item.get("sku") or "").strip()
        size = normalize(item.get("size"))
        barcode = normalize(item.get("barcode"))
        goods_type = normalize(item.get("goods_type"))
        qs = WarehouseStockSnapshot.objects.filter(
            agency=agency,
            sku_code=sku_code,
            size=size,
            barcode=barcode,
            goods_type=goods_type,
            warehouse_state_code=WarehouseStateCode.STORED.value,
            is_archived=False,
        ).order_by("id")
        for snapshot in qs:
            if int(snapshot.available_qty or 0) >= required_qty:
                return snapshot
        raise ValueError(f"No stored snapshot with enough available qty for {sku_code}")

    @staticmethod
    def _match_snapshots_for_processing_reserve(
        *,
        agency: Agency,
        item: dict,
        required_qty: int,
    ) -> list[tuple[WarehouseStockSnapshot, int]]:
        normalize = WarehouseWritePathService._normalize_reserve_lookup_text
        sku_code = str(item.get("sku_code") or item.get("sku") or "").strip()
        size = normalize(item.get("size"))
        barcode = normalize(item.get("barcode"))
        goods_type = normalize(item.get("goods_type"))
        remaining_qty = max(int(required_qty or 0), 0)
        allocations: list[tuple[WarehouseStockSnapshot, int]] = []
        qs = (
            WarehouseStockSnapshot.objects.select_for_update()
            .filter(
                agency=agency,
                sku_code=sku_code,
                size=size,
                barcode=barcode,
                goods_type=goods_type,
                warehouse_state_code__in=[
                    WarehouseStateCode.STORED.value,
                    WarehouseStateCode.RESERVED_FOR_PROCESSING.value,
                ],
                is_archived=False,
            )
            .order_by("id")
        )
        for snapshot in qs:
            available_qty = int(snapshot.available_qty or 0)
            if available_qty <= 0:
                continue
            reserved_qty = min(available_qty, remaining_qty)
            allocations.append((snapshot, reserved_qty))
            remaining_qty -= reserved_qty
            if remaining_qty <= 0:
                return allocations
        raise ValueError(f"No stored snapshots with enough available qty for {sku_code}")

    @staticmethod
    def _snapshot_matches_shipping_context(snapshot: WarehouseStockSnapshot, order_id: str) -> bool:
        return WarehouseReserve.objects.filter(
            agency=snapshot.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id=order_id,
            sku_code=snapshot.sku_code,
            size=snapshot.size,
            barcode=snapshot.barcode,
            goods_type=snapshot.goods_type,
            status__in=[
                WarehouseReserve.STATUS_ACTIVE,
                WarehouseReserve.STATUS_PARTIALLY_ALLOCATED,
                WarehouseReserve.STATUS_ALLOCATED,
                WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
                WarehouseReserve.STATUS_SATISFIED,
            ],
        ).exists()

    @staticmethod
    def _match_snapshot_for_shipping_reserve(*, agency: Agency, item: dict, required_qty: int) -> WarehouseStockSnapshot:
        normalize = WarehouseWritePathService._normalize_reserve_lookup_text
        sku_code = str(item.get("sku_code") or item.get("sku") or "").strip()
        size = normalize(item.get("size"))
        barcode = normalize(item.get("barcode"))
        goods_type = normalize(item.get("goods_type"))
        qs = WarehouseStockSnapshot.objects.filter(
            agency=agency,
            sku_code=sku_code,
            size=size,
            barcode=barcode,
            goods_type=goods_type,
            warehouse_state_code__in=[
                WarehouseStateCode.STORED.value,
                WarehouseStateCode.PLACED_AFTER_PROCESSING.value,
            ],
            is_archived=False,
        ).order_by("id")
        for snapshot in qs:
            if int(snapshot.available_qty or 0) >= required_qty:
                return snapshot
        raise ValueError(f"No snapshot with enough available qty for shipping reserve: {sku_code}")

    @staticmethod
    def _match_snapshots_for_shipping_reserve(
        *,
        agency: Agency,
        item: dict,
        required_qty: int,
    ) -> list[tuple[WarehouseStockSnapshot, int]]:
        normalize = WarehouseWritePathService._normalize_reserve_lookup_text
        sku_code = str(item.get("sku_code") or item.get("sku") or "").strip()
        size = normalize(item.get("size"))
        barcode = normalize(item.get("barcode"))
        goods_type = normalize(item.get("goods_type"))
        remaining_qty = max(int(required_qty or 0), 0)
        allocations: list[tuple[WarehouseStockSnapshot, int]] = []
        qs = (
            WarehouseStockSnapshot.objects.select_for_update()
            .filter(
                agency=agency,
                sku_code=sku_code,
                size=size,
                barcode=barcode,
                goods_type=goods_type,
                warehouse_state_code__in=[
                    WarehouseStateCode.STORED.value,
                    WarehouseStateCode.PLACED_AFTER_PROCESSING.value,
                    WarehouseStateCode.RESERVED_FOR_SHIPPING.value,
                ],
                is_archived=False,
            )
            .order_by("id")
        )
        for snapshot in qs:
            available_qty = int(snapshot.available_qty or 0)
            if available_qty <= 0:
                continue
            reserved_qty = min(available_qty, remaining_qty)
            allocations.append((snapshot, reserved_qty))
            remaining_qty -= reserved_qty
            if remaining_qty <= 0:
                return allocations
        raise ValueError(f"No snapshots with enough available qty for shipping reserve: {sku_code}")

    @staticmethod
    def _state_for_location(location: WarehouseLocation) -> str:
        zone = str(getattr(location, "zone_code", "") or "").strip().upper()
        if zone == "PR":
            return WarehouseStateCode.PLACED_IN_RECEIVING.value
        if zone in {"OS", "MR"}:
            return WarehouseStateCode.STORED.value
        if zone == "OBR":
            return WarehouseStateCode.IN_PROCESSING_ZONE.value
        if zone == "OTG":
            return WarehouseStateCode.IN_OTG.value
        return WarehouseStateCode.UNKNOWN.value

    @classmethod
    def _location_from_item(
        cls,
        *,
        item: dict,
        fallback: WarehouseLocation,
        warehouse_code: str,
    ) -> WarehouseLocation:
        location = item.get("location") if isinstance(item.get("location"), dict) else {}
        zone = str(location.get("zone") or location.get("zone_code") or "").strip().upper()
        if not zone:
            return fallback
        return cls.ensure_location(
            warehouse_code=warehouse_code,
            zone_code=zone,
            row_no=int(location.get("row") or location.get("row_no") or 0),
            section_no=int(location.get("section") or location.get("section_no") or 0),
            tier_no=int(location.get("tier") or location.get("tier_no") or 0),
            cell_no=int(location.get("cell") or location.get("cell_no") or 0),
        )

    @staticmethod
    def _role_of(user) -> str:
        if not user:
            return ""
        employee = getattr(user, "employee_profile", None)
        if employee and getattr(employee, "role", ""):
            return str(employee.role).strip().lower()
        return ""

    @staticmethod
    def _location_code(zone: str, row_no: int, section_no: int, tier_no: int, cell_no: int) -> str:
        values = [str(max(int(value or 0), 0)) for value in (row_no, section_no, tier_no, cell_no)]
        if any(int(value) > 0 for value in values):
            return f"{zone}-{'-'.join(values)}"
        return zone

    @staticmethod
    def _location_display_name(zone: str, row_no: int, section_no: int, tier_no: int, cell_no: int) -> str:
        if zone == "PR":
            return "PR · Зона приемки"
        if zone == "OS":
            if all(int(value or 0) > 0 for value in (row_no, section_no, tier_no, cell_no)):
                return f"OS · Ряд {row_no} · Секция {section_no} · Ярус {tier_no} · Ячейка {cell_no}"
            return "OS · Основной склад"
        if zone == "MR":
            if int(row_no or 0) > 0:
                return f"MR · Ряд {row_no}"
            return "MR · Мезонин"
        if zone == "OBR":
            return "OBR · Зона обработки"
        if zone == "OTG":
            return "OTG · Зона отгрузки"
        return zone

    @classmethod
    def _receiving_items_from_placement_payload(
        cls,
        *,
        order_id: str,
        placement_payload: dict,
    ) -> list[dict]:
        payload = placement_payload if isinstance(placement_payload, dict) else {}
        boxes = payload.get("act_boxes") or []
        pallets = payload.get("act_pallets") or []
        if not isinstance(boxes, list):
            boxes = []
        if not isinstance(pallets, list):
            pallets = []
        default_goods_type = str(payload.get("goods_type") or "").strip()

        box_to_pallet: dict[str, str] = {}
        box_to_location: dict[str, dict] = {}
        items: list[dict] = []
        for pallet in pallets:
            if not isinstance(pallet, dict):
                continue
            pallet_code = str(pallet.get("code") or "").strip()
            if not pallet_code:
                continue
            pallet_goods_type = str(pallet.get("goods_type") or default_goods_type).strip()
            pallet_location = pallet.get("location") if isinstance(pallet.get("location"), dict) else {}
            for box_code in pallet.get("boxes") or []:
                normalized_box_code = str(box_code or "").strip()
                if normalized_box_code:
                    box_to_pallet[normalized_box_code] = pallet_code
                    box_to_location[normalized_box_code] = dict(pallet_location or {})
            for item in pallet.get("items") or []:
                normalized = cls._normalize_receiving_item(
                    item=item,
                    order_id=order_id,
                    pallet_code=pallet_code,
                    box_code="",
                    default_goods_type=pallet_goods_type,
                    location=pallet_location,
                )
                if normalized:
                    items.append(normalized)
        for box in boxes:
            if not isinstance(box, dict):
                continue
            box_code = str(box.get("code") or "").strip()
            pallet_code = box_to_pallet.get(box_code, "")
            box_goods_type = str(box.get("goods_type") or default_goods_type).strip()
            box_location = (
                box.get("location")
                if isinstance(box.get("location"), dict)
                else box_to_location.get(box_code, {})
            )
            for item in box.get("items") or []:
                normalized = cls._normalize_receiving_item(
                    item=item,
                    order_id=order_id,
                    pallet_code=pallet_code,
                    box_code=box_code,
                    default_goods_type=box_goods_type,
                    location=box_location,
                )
                if normalized:
                    items.append(normalized)
        return items

    @staticmethod
    def _normalize_receiving_item(
        *,
        item: dict,
        order_id: str,
        pallet_code: str,
        box_code: str,
        default_goods_type: str = "",
        location: dict | None = None,
    ) -> dict | None:
        if not isinstance(item, dict):
            return None
        qty = max(int(item.get("qty") or 0), 0)
        if qty <= 0:
            return None
        return {
            "order_id": str(order_id or "").strip(),
            "sku_code": str(item.get("sku_code") or item.get("sku") or "").strip(),
            "sku": str(item.get("sku") or item.get("sku_code") or "").strip(),
            "name": str(item.get("name") or "").strip(),
            "size": str(item.get("size") or "").strip(),
            "barcode": str(item.get("barcode") or "").strip(),
            "goods_type": str(item.get("goods_type") or default_goods_type or "").strip(),
            "marking_code": str(item.get("marking_code") or "").strip(),
            "qty": qty,
            "pallet_code": str(pallet_code or "").strip(),
            "box_code": str(box_code or "").strip(),
            "location": dict(location or {}),
        }
