from datetime import datetime, timedelta
from io import BytesIO
import json
import shutil
import tempfile
from unittest.mock import patch
import zipfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory
from django.test import TestCase
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from audit.models import OrderAuditEntry
from employees.models import Employee
from head_manager.models import Carrier, OwnCompany
from logistics.models import LogisticsTrip, LogisticsTripOrder
from reachtruck.models import MoveRequest, MoveTask
from sklad.models import (
    WarehouseContainer,
    WarehouseLocation,
    WarehouseReserve,
    WarehouseStockSnapshot,
)
from sklad.services import WarehouseGoodsStateResolver, WarehouseStateCode
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sklad.test_utils import create_warehouse_snapshot_row
from sku.models import Agency, Market
from todo.models import Task

from .forms import ShippingOrderForm
from .models import ShippingOrder, ShippingOrderAttachment, ShippingOrderItem, ShippingTransportNote
from .services import (
    build_shipping_detail_page_context,
    build_shipping_list_page_context,
    create_pick_tasks,
    release_order_reserves,
    reserve_order,
    ship_order,
)
from .views import (
    _can_cancel,
    _can_edit_items,
    _display_shipping_number,
    _order_box_count,
    _parse_selected_stock_items,
    _shipping_box_row_key,
    _shipping_boxes_from_packing_payload,
    _shipping_packing_summary,
    _shipping_packing_initial_state,
    _shipping_delivered_boxes,
    _shipping_reachtruck_metrics,
    _shipping_stock_picker_rows,
    save_shipping_packing,
)


class ShippingFlowTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="shipping_manager", password="pwd")
        Employee.objects.create(
            full_name="Менеджер отгрузки",
            role="manager",
            user=self.user,
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Тест Клиент")
        self.order = ShippingOrder.objects.create(
            number="SO-000001",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_SUBMITTED,
        )
        self.item = ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-001",
            name="Товар 1",
            size="42",
            barcode="200000000001",
            goods_type="Готовый",
            qty_requested=20,
        )
        self._create_snapshot_box()

    def _create_snapshot_box(
        self,
        *,
        pallet_code: str = "PL-1",
        box_code: str = "BX-1",
        order_id: str = "R-SNAP",
        sku: str = "SKU-001",
        name: str = "Товар 1",
        size: str = "42",
        barcode: str = "200000000001",
        goods_type: str = "Готовый",
        qty: int = 100,
        zone: str = "OS",
        row: int = 1,
        section: int = 1,
        tier: int = 1,
        cell: int = 1,
        location_code: str = "OS-1-1-1-1",
    ) -> WarehouseStockSnapshot:
        location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code=zone,
            row_no=row,
            section_no=section,
            tier_no=tier,
            cell_no=cell,
        )
        if location.location_code != location_code:
            location.location_code = location_code
            location.save(update_fields=["location_code", "updated_at"])
        pallet, _ = WarehouseContainer.objects.get_or_create(
            agency=self.agency,
            container_code=pallet_code,
            defaults={
                "container_type": WarehouseContainer.TYPE_PALLET,
                "current_location": location,
            },
        )
        if pallet.current_location_id != location.id:
            pallet.current_location = location
            pallet.save(update_fields=["current_location", "updated_at"])
        box, _ = WarehouseContainer.objects.get_or_create(
            agency=self.agency,
            container_code=box_code,
            defaults={
                "container_type": WarehouseContainer.TYPE_BOX,
                "parent_container": pallet,
                "current_location": location,
            },
        )
        changed_fields = []
        if box.parent_container_id != pallet.id:
            box.parent_container = pallet
            changed_fields.append("parent_container")
        if box.current_location_id != location.id:
            box.current_location = location
            changed_fields.append("current_location")
        if changed_fields:
            box.save(update_fields=[*changed_fields, "updated_at"])
        return WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code=sku,
            name=name,
            size=size,
            barcode=barcode,
            goods_type=goods_type,
            qty=qty,
            available_qty=qty,
            container=box,
            container_code=box.container_code,
            parent_container=pallet,
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="stored",
        )

    def _load_snapshot_order_to_vehicle(self, *, trip_number: str = "TRIP-1", qty: int = 20) -> WarehouseStockSnapshot:
        snapshot = self._create_snapshot_box(qty=qty)
        WarehouseWritePathService.reserve_for_shipping(
            agency=self.agency,
            order_id=self.order.number,
            items=[
                {
                    "sku_code": self.item.sku_code,
                    "size": self.item.size,
                    "barcode": self.item.barcode,
                    "goods_type": self.item.goods_type,
                    "qty": qty,
                }
            ],
            created_by=self.user,
        )
        move_op = WarehouseWritePathService.request_move_to_otg(
            agency=self.agency,
            order_id=self.order.number,
            requested_by=self.user,
        )
        WarehouseWritePathService.start_move_to_otg(operation=move_op, performed_by=self.user)
        WarehouseWritePathService.complete_move_to_otg(operation=move_op, performed_by=self.user)
        palletization = WarehouseWritePathService.start_palletization(
            agency=self.agency,
            order_id=self.order.number,
            started_by=self.user,
        )
        WarehouseWritePathService.complete_palletization(operation=palletization, performed_by=self.user)
        trip = LogisticsTrip.objects.create(
            number=trip_number,
            status=LogisticsTrip.STATUS_LOADING,
            vehicle_type=LogisticsTrip.VEHICLE_FULFILLMENT,
            created_by=self.user,
        )
        LogisticsTripOrder.objects.create(trip=trip, shipping_order=self.order)
        WarehouseWritePathService.assign_to_trip(
            agency=self.agency,
            order_id=self.order.number,
            trip_id=trip.number,
            assigned_by=self.user,
        )
        loading = WarehouseWritePathService.start_loading(
            agency=self.agency,
            order_id=self.order.number,
            trip_id=trip.number,
            started_by=self.user,
        )
        WarehouseWritePathService.complete_loading(operation=loading, performed_by=self.user)
        snapshot.refresh_from_db()
        return snapshot

    def _load_snapshot_order_ready_for_loading(self, *, qty: int = 20) -> WarehouseStockSnapshot:
        snapshot = self._create_snapshot_box(qty=qty)
        WarehouseWritePathService.reserve_for_shipping(
            agency=self.agency,
            order_id=self.order.number,
            items=[
                {
                    "sku_code": self.item.sku_code,
                    "size": self.item.size,
                    "barcode": self.item.barcode,
                    "goods_type": self.item.goods_type,
                    "qty": qty,
                }
            ],
            created_by=self.user,
        )
        move_op = WarehouseWritePathService.request_move_to_otg(
            agency=self.agency,
            order_id=self.order.number,
            requested_by=self.user,
        )
        WarehouseWritePathService.start_move_to_otg(operation=move_op, performed_by=self.user)
        WarehouseWritePathService.complete_move_to_otg(operation=move_op, performed_by=self.user)
        palletization = WarehouseWritePathService.start_palletization(
            agency=self.agency,
            order_id=self.order.number,
            started_by=self.user,
        )
        WarehouseWritePathService.complete_palletization(operation=palletization, performed_by=self.user)
        snapshot.refresh_from_db()
        return snapshot

    def _assign_ready_snapshot_to_trip(
        self,
        *,
        trip_number: str = "TRIP-1",
        qty: int = 20,
        start_loading: bool = False,
    ) -> tuple[WarehouseStockSnapshot, LogisticsTrip]:
        snapshot = self._load_snapshot_order_ready_for_loading(qty=qty)
        trip = LogisticsTrip.objects.create(
            number=trip_number,
            status=LogisticsTrip.STATUS_LOADING if start_loading else LogisticsTrip.STATUS_PLANNED,
            vehicle_type=LogisticsTrip.VEHICLE_FULFILLMENT,
            created_by=self.user,
        )
        LogisticsTripOrder.objects.create(trip=trip, shipping_order=self.order)
        WarehouseWritePathService.assign_to_trip(
            agency=self.agency,
            order_id=self.order.number,
            trip_id=trip.number,
            assigned_by=self.user,
        )
        if start_loading:
            WarehouseWritePathService.start_loading(
                agency=self.agency,
                order_id=self.order.number,
                trip_id=trip.number,
                started_by=self.user,
            )
        snapshot.refresh_from_db()
        return snapshot, trip

    def test_reserve_order_creates_reserve_rows(self):
        reserve_order(self.order, self.user)
        self.item.refresh_from_db()
        self.order.refresh_from_db()
        snapshot = WarehouseStockSnapshot.objects.get(agency=self.agency, sku_code="SKU-001", size="42")
        self.assertEqual(self.order.status, ShippingOrder.STATUS_RESERVED)
        self.assertEqual(self.item.qty_reserved, 20)
        self.assertEqual(
            WarehouseReserve.objects.filter(
                agency=self.agency,
                reserve_type=WarehouseReserve.TYPE_SHIPPING,
                context_id=self.order.number,
            ).count(),
            1,
        )
        self.assertEqual(snapshot.processing_reserved_qty, 0)
        self.assertEqual(snapshot.shipping_reserved_qty, 20)
        self.assertEqual(snapshot.available_qty, 80)

    def test_reserve_order_can_keep_submitted_status_for_auto_reserve(self):
        reserve_order(
            self.order,
            self.user,
            target_status=ShippingOrder.STATUS_SUBMITTED,
            log_description="Авто-резерв при отправке клиента",
        )
        self.item.refresh_from_db()
        self.order.refresh_from_db()
        snapshot = WarehouseStockSnapshot.objects.get(agency=self.agency, sku_code="SKU-001", size="42")
        self.assertEqual(self.order.status, ShippingOrder.STATUS_SUBMITTED)
        self.assertEqual(self.item.qty_reserved, 20)
        self.assertEqual(
            WarehouseReserve.objects.filter(
                agency=self.agency,
                reserve_type=WarehouseReserve.TYPE_SHIPPING,
                context_id=self.order.number,
            ).count(),
            1,
        )
        self.assertEqual(snapshot.shipping_reserved_qty, 20)
        self.assertEqual(snapshot.available_qty, 80)

    def test_release_order_reserves_restores_available_qty_without_global_refresh(self):
        reserve_order(self.order, self.user)

        release_order_reserves(self.order, self.user)

        self.order.refresh_from_db()
        self.item.refresh_from_db()
        snapshot = WarehouseStockSnapshot.objects.get(agency=self.agency, sku_code="SKU-001", size="42")
        self.assertEqual(self.order.status, ShippingOrder.STATUS_SUBMITTED)
        self.assertEqual(self.item.qty_reserved, 0)
        self.assertFalse(
            WarehouseReserve.objects.filter(
                agency=self.agency,
                reserve_type=WarehouseReserve.TYPE_SHIPPING,
                context_id=self.order.number,
            )
            .exclude(status__in=[WarehouseReserve.STATUS_RELEASED, WarehouseReserve.STATUS_CANCELED])
            .exists()
        )
        self.assertEqual(snapshot.shipping_reserved_qty, 0)
        self.assertEqual(snapshot.available_qty, 100)

    def test_create_pick_tasks_creates_reachtruck_task(self):
        reserve_order(self.order, self.user)
        move_ids = create_pick_tasks(
            self.order,
            self.user,
            requested_by_name="Тест Менеджер",
            requested_by_role="manager",
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, ShippingOrder.STATUS_PICKING)
        self.assertTrue(move_ids)
        self.assertEqual(MoveRequest.objects.count(), 1)
        self.assertEqual(MoveTask.objects.count(), 1)
        task = MoveTask.objects.first()
        self.assertEqual(task.to_zone, "OTG")
        self.assertEqual(task.request.items.count(), 1)

    def test_create_pick_tasks_groups_multiple_items_from_same_pallet_into_one_task(self):
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-002",
            name="Товар 2",
            size="44",
            barcode="200000000002",
            goods_type="Готовый",
            qty_requested=10,
        )
        self._create_snapshot_box(
            order_id="R-2",
            sku="SKU-002",
            name="Товар 2",
            size="44",
            barcode="200000000002",
            qty=30,
            box_code="BX-2",
            pallet_code="PL-1",
        )

        reserve_order(self.order, self.user)
        move_ids = create_pick_tasks(
            self.order,
            self.user,
            requested_by_name="Тест Менеджер",
            requested_by_role="manager",
        )

        self.assertEqual(len(move_ids), 1)
        self.assertEqual(MoveRequest.objects.count(), 1)
        self.assertEqual(MoveTask.objects.count(), 1)
        task = MoveTask.objects.first()
        self.assertEqual(task.pallet_code, "PL-1")
        self.assertEqual(task.request.items.count(), 2)
        self.assertEqual(task.qty_planned, 30)
        self.assertEqual(task.payload.get("task_kind_label"), "Частичный отбор с палеты для отгрузки")
        self.assertEqual(
            task.payload.get("requested_barcode_qty"),
            {"200000000001": 20, "200000000002": 10},
        )
        self.assertIn("Частичный отбор для отгрузки", task.payload.get("instruction") or "")
        self.assertIn("верни палету обратно", task.payload.get("instruction") or "")

    def test_create_pick_tasks_uses_full_pallet_when_request_covers_entire_pallet(self):
        self.item.qty_requested = 100
        self.item.save(update_fields=["qty_requested", "updated_at"])
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-002",
            name="Товар 2",
            size="44",
            barcode="200000000002",
            goods_type="Готовый",
            qty_requested=30,
        )
        self._create_snapshot_box(
            order_id="R-2",
            sku="SKU-002",
            name="Товар 2",
            size="44",
            barcode="200000000002",
            qty=30,
            box_code="BX-2",
            pallet_code="PL-1",
        )

        reserve_order(self.order, self.user)
        move_ids = create_pick_tasks(
            self.order,
            self.user,
            requested_by_name="Тест Менеджер",
            requested_by_role="manager",
        )

        self.assertEqual(len(move_ids), 1)
        task = MoveTask.objects.first()
        self.assertEqual(task.move_mode, MoveTask.MODE_PALLET_FULL)
        self.assertEqual(task.payload.get("task_kind_label"), "Паллета целиком")
        self.assertEqual(task.payload.get("pick_mode"), "full")
        self.assertEqual(task.payload.get("requested_qty"), "")
        self.assertEqual(task.payload.get("requested_barcode_qty"), {})
        self.assertIn("Возьми палету PL-1 целиком", task.payload.get("instruction") or "")
        self.assertNotIn("верни палету обратно", task.payload.get("instruction") or "")

    def test_warehouse_state_resolver_marks_picking_order_as_in_otg_after_delivery(self):
        self.order.status = ShippingOrder.STATUS_PICKING
        self.order.save(update_fields=["status", "updated_at"])
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="receiving",
            order_id="R-RES-1",
            action="placement",
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {
                        "code": "BX-RES-1",
                        "items": [
                            {
                                "sku_code": "SKU-001",
                                "name": "Товар 1",
                                "size": "42",
                                "barcode": "200000000001",
                                "goods_type": "Готовый",
                                "qty": 20,
                            }
                        ],
                    }
                ],
            },
        )
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id="",
            agency=self.agency,
            destination_zone="OTG",
            status=MoveRequest.STATUS_DONE,
        )
        MoveTask.objects.create(
            request=move_request,
            pallet_code="PL-RES-1",
            to_zone="OTG",
            status=MoveTask.STATUS_DONE,
            payload={
                "shipping_order_id": self.order.number,
                "shipping_order_pk": self.order.pk,
                "receiving_order_id": "R-RES-1",
                "picked_boxes": ["BX-RES-1"],
                "picked_rows": [{"box_code": "BX-RES-1", "qty": 20}],
            },
        )

        result = WarehouseGoodsStateResolver.resolve_for_shipping_order(self.order)

        self.assertEqual(result.code, WarehouseStateCode.IN_OTG)
        self.assertEqual(result.label_for("default"), "Товар доставлен в OTG, ожидает паллетизации")

    def test_shipping_delivered_boxes_reads_boxes_from_all_placement_acts_of_same_receiving_order(self):
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id="",
            agency=self.agency,
            destination_zone="OTG",
            status=MoveRequest.STATUS_DONE,
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="receiving",
            order_id="R-MULTI-1",
            action="placement",
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {
                        "code": "BX-MULTI-1",
                        "items": [
                            {
                                "sku_code": "SKU-001",
                                "name": "Товар 1",
                                "size": "42",
                                "barcode": "200000000001",
                                "goods_type": "Готовый",
                                "qty": 10,
                            }
                        ],
                    }
                ],
            },
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="receiving",
            order_id="R-MULTI-1",
            action="placement",
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {
                        "code": "BX-MULTI-2",
                        "items": [
                            {
                                "sku_code": "SKU-001",
                                "name": "Товар 1",
                                "size": "42",
                                "barcode": "200000000001",
                                "goods_type": "Готовый",
                                "qty": 10,
                            }
                        ],
                    }
                ],
            },
        )
        MoveTask.objects.create(
            request=move_request,
            pallet_code="PL-MULTI-1",
            to_zone="OTG",
            status=MoveTask.STATUS_DONE,
            payload={
                "shipping_order_id": self.order.number,
                "shipping_order_pk": self.order.pk,
                "receiving_order_id": "R-MULTI-1",
                "picked_boxes": ["BX-MULTI-1", "BX-MULTI-2"],
            },
        )

        rows = _shipping_delivered_boxes(self.order)

        self.assertEqual(
            [(row["box_code"], row["qty"]) for row in rows],
            [("BX-MULTI-1", 10), ("BX-MULTI-2", 10)],
        )

    def test_shipping_delivered_boxes_prefers_warehouse_snapshots_in_otg(self):
        WarehouseStockSnapshot.objects.filter(agency=self.agency).delete()
        self._create_snapshot_box(box_code="BX-WH-1", qty=20)
        WarehouseWritePathService.reserve_for_shipping(
            agency=self.agency,
            order_id=self.order.number,
            items=[
                {
                    "sku_code": self.item.sku_code,
                    "size": self.item.size,
                    "barcode": self.item.barcode,
                    "goods_type": self.item.goods_type,
                    "qty": 20,
                }
            ],
            created_by=self.user,
        )
        move_op = WarehouseWritePathService.request_move_to_otg(
            agency=self.agency,
            order_id=self.order.number,
            requested_by=self.user,
        )
        WarehouseWritePathService.start_move_to_otg(operation=move_op, performed_by=self.user)
        WarehouseWritePathService.complete_move_to_otg(operation=move_op, performed_by=self.user)

        rows = _shipping_delivered_boxes(self.order)

        self.assertEqual(
            rows,
            [
                {
                    "row_key": _shipping_box_row_key({"box_code": "BX-WH-1", "receiving_order_id": "R-SNAP"}, 1),
                    "box_code": "BX-WH-1",
                    "qty": 20,
                    "items": [
                        {
                            "sku_code": "SKU-001",
                            "name": "Товар 1",
                            "size": "42",
                            "barcode": "200000000001",
                            "goods_type": "Готовый",
                            "qty": 20,
                        }
                    ],
                    "barcode_preview": "200000000001 - 20 шт.",
                    "receiving_order_id": "R-SNAP",
                }
            ],
        )

    def test_save_shipping_packing_persists_shipping_pallets_in_warehouse(self):
        WarehouseStockSnapshot.objects.filter(agency=self.agency).delete()
        snapshot = self._create_snapshot_box(box_code="BX-PACK-1", qty=20)
        self.order.status = ShippingOrder.STATUS_PICKING
        self.order.save(update_fields=["status", "updated_at"])
        WarehouseWritePathService.reserve_for_shipping(
            agency=self.agency,
            order_id=self.order.number,
            items=[
                {
                    "sku_code": self.item.sku_code,
                    "size": self.item.size,
                    "barcode": self.item.barcode,
                    "goods_type": self.item.goods_type,
                    "qty": 20,
                }
            ],
            created_by=self.user,
        )
        move_op = WarehouseWritePathService.request_move_to_otg(
            agency=self.agency,
            order_id=self.order.number,
            requested_by=self.user,
        )
        WarehouseWritePathService.start_move_to_otg(operation=move_op, performed_by=self.user)
        WarehouseWritePathService.complete_move_to_otg(operation=move_op, performed_by=self.user)

        packing_state = _shipping_packing_initial_state(self.order)
        row = packing_state["initial_boxes"][0]
        result = save_shipping_packing(
            self.order,
            boxes_state=[
                {
                    "row_key": row["row_key"],
                    "code": row["code"],
                    "qty": row["qty"],
                    "barcode_preview": row["barcode_preview"],
                    "pallet_code": "PAL-1",
                }
            ],
            pallets_state=[{"code": "PAL-1", "label": "1"}],
            delivered_boxes=packing_state["delivered_boxes"],
            initial_boxes=packing_state["initial_boxes"],
            user=self.user,
        )

        self.assertTrue(result["saved"])
        self.order.refresh_from_db()
        snapshot.refresh_from_db()
        snapshot.container.refresh_from_db()
        self.assertEqual(self.order.status, ShippingOrder.STATUS_PACKED)
        self.assertEqual(snapshot.warehouse_state_code, "ready_for_loading")
        self.assertIsNone(snapshot.active_operation)
        self.assertIsNotNone(snapshot.parent_container)
        self.assertEqual(snapshot.parent_container.container_type, WarehouseContainer.TYPE_MIXED_PALLET)
        self.assertEqual(snapshot.parent_container.source_context_type, "shipping")
        self.assertEqual(snapshot.parent_container.source_context_id, self.order.number)
        self.assertTrue(str(snapshot.parent_container.container_code).startswith("SHIP-"))
        self.assertEqual(snapshot.container.parent_container_id, snapshot.parent_container_id)

    def test_shipping_packing_summary_falls_back_to_warehouse_when_audit_missing(self):
        WarehouseStockSnapshot.objects.filter(agency=self.agency).delete()
        self._create_snapshot_box(box_code="BX-PACK-2", qty=20)
        self.order.status = ShippingOrder.STATUS_PICKING
        self.order.save(update_fields=["status", "updated_at"])
        WarehouseWritePathService.reserve_for_shipping(
            agency=self.agency,
            order_id=self.order.number,
            items=[
                {
                    "sku_code": self.item.sku_code,
                    "size": self.item.size,
                    "barcode": self.item.barcode,
                    "goods_type": self.item.goods_type,
                    "qty": 20,
                }
            ],
            created_by=self.user,
        )
        move_op = WarehouseWritePathService.request_move_to_otg(
            agency=self.agency,
            order_id=self.order.number,
            requested_by=self.user,
        )
        WarehouseWritePathService.start_move_to_otg(operation=move_op, performed_by=self.user)
        WarehouseWritePathService.complete_move_to_otg(operation=move_op, performed_by=self.user)
        packing_state = _shipping_packing_initial_state(self.order)
        row = packing_state["initial_boxes"][0]
        save_shipping_packing(
            self.order,
            boxes_state=[
                {
                    "row_key": row["row_key"],
                    "code": row["code"],
                    "qty": row["qty"],
                    "barcode_preview": row["barcode_preview"],
                    "pallet_code": "PAL-1",
                }
            ],
            pallets_state=[{"code": "PAL-1", "label": "1"}],
            delivered_boxes=packing_state["delivered_boxes"],
            initial_boxes=packing_state["initial_boxes"],
            user=self.user,
        )
        OrderAuditEntry.objects.filter(
            agency=self.agency,
            order_type="shipping",
            order_id=self.order.number,
            payload__act="shipping_packing",
        ).delete()

        summary = _shipping_packing_summary(self.order)

        self.assertIsNotNone(summary)
        self.assertEqual(summary["pallet_count"], 1)
        self.assertEqual(summary["box_count"], 1)
        self.assertEqual(summary["pallets"][0]["code"].startswith("SHIP-"), True)
        self.assertEqual(summary["pallets"][0]["boxes"][0]["box_code"], "BX-PACK-2")

    def test_warehouse_state_resolver_marks_packed_order_as_assigned_to_trip(self):
        self.order.status = ShippingOrder.STATUS_PACKED
        self.order.save(update_fields=["status", "updated_at"])

        result = WarehouseGoodsStateResolver.resolve_for_shipping_order(
            self.order,
            trip_status=LogisticsTrip.STATUS_PLANNED,
        )

        self.assertEqual(result.code, WarehouseStateCode.ASSIGNED_TO_TRIP)
        self.assertEqual(result.label_for("logistician"), "Подготовка к рейсу")

    def test_warehouse_state_resolver_marks_departed_packed_order_as_loaded(self):
        self.order.status = ShippingOrder.STATUS_PACKED
        self.order.save(update_fields=["status", "updated_at"])

        result = WarehouseGoodsStateResolver.resolve_for_shipping_order(
            self.order,
            trip_status=LogisticsTrip.STATUS_DEPARTED,
        )

        self.assertEqual(result.code, WarehouseStateCode.LOADED_TO_VEHICLE)
        self.assertEqual(result.label_for("storekeeper"), "Загружено в машину")

    def test_warehouse_state_resolver_prefers_ready_for_loading_snapshot_over_order_status(self):
        WarehouseStockSnapshot.objects.filter(agency=self.agency).delete()
        self._load_snapshot_order_ready_for_loading(qty=20)
        self.order.status = ShippingOrder.STATUS_SUBMITTED
        self.order.save(update_fields=["status", "updated_at"])

        result = WarehouseGoodsStateResolver.resolve_for_shipping_order(self.order)

        self.assertEqual(result.code, WarehouseStateCode.READY_FOR_LOADING)
        self.assertEqual(result.label_for("default"), "Подготовлена складом, ожидает логиста")

    def test_warehouse_state_resolver_prefers_loaded_snapshot_over_trip_status_fallback(self):
        WarehouseStockSnapshot.objects.filter(agency=self.agency).delete()
        self._load_snapshot_order_to_vehicle(qty=20)
        self.order.status = ShippingOrder.STATUS_SUBMITTED
        self.order.save(update_fields=["status", "updated_at"])

        result = WarehouseGoodsStateResolver.resolve_for_shipping_order(
            self.order,
            trip_status=LogisticsTrip.STATUS_PLANNED,
        )

        self.assertEqual(result.code, WarehouseStateCode.LOADED_TO_VEHICLE)
        self.assertEqual(result.label_for("default"), "Загружено в машину")

    def test_create_pick_tasks_prefers_single_pallet_when_it_covers_request(self):
        self.item.qty_requested = 110
        self.item.save(update_fields=["qty_requested", "updated_at"])
        self._create_snapshot_box(
            order_id="R-2",
            sku="SKU-001",
            name="Товар 1",
            size="42",
            barcode="200000000001",
            qty=110,
            box_code="BX-2",
            pallet_code="PL-2",
            zone="MR",
            row=2,
            section=1,
            tier=1,
            cell=1,
            location_code="MR-2-1-1-1",
        )

        reserve_order(self.order, self.user)
        move_ids = create_pick_tasks(
            self.order,
            self.user,
            requested_by_name="Тест Менеджер",
            requested_by_role="manager",
        )

        self.assertEqual(len(move_ids), 1)
        self.assertEqual(MoveTask.objects.count(), 1)
        task = MoveTask.objects.first()
        self.assertEqual(task.pallet_code, "PL-2")
        self.assertEqual(task.move_mode, MoveTask.MODE_PALLET_FULL)
        self.assertEqual(task.payload.get("pick_mode"), "full")
        self.assertEqual(task.payload.get("requested_qty"), "")

    def test_ship_order_deducts_stock(self):
        reserve_order(self.order, self.user)
        ship_order(self.order, self.user, shipped_qty_by_item={self.item.id: 20})
        self.order.refresh_from_db()
        self.item.refresh_from_db()
        snapshot = WarehouseStockSnapshot.objects.get(
            agency=self.agency,
            sku_code="SKU-001",
            size="42",
        )
        self.assertEqual(snapshot.qty, 80)
        self.assertEqual(snapshot.shipping_reserved_qty, 0)
        self.assertEqual(snapshot.available_qty, 80)
        self.assertEqual(self.order.status, ShippingOrder.STATUS_SHIPPED)
        self.assertEqual(self.item.qty_shipped, 20)
        self.assertFalse(
            WarehouseReserve.objects.filter(
                agency=self.agency,
                reserve_type=WarehouseReserve.TYPE_SHIPPING,
                context_id=self.order.number,
            )
            .exclude(status__in=[WarehouseReserve.STATUS_RELEASED, WarehouseReserve.STATUS_CANCELED])
            .exists()
        )

    def test_ship_order_uses_warehouse_write_path_for_loaded_trip(self):
        WarehouseStockSnapshot.objects.filter(agency=self.agency).delete()
        snapshot = self._load_snapshot_order_to_vehicle(qty=20)
        self.item.qty_reserved = 20
        self.item.save(update_fields=["qty_reserved", "updated_at"])

        ship_order(self.order, self.user, shipped_qty_by_item={self.item.id: 20})

        self.order.refresh_from_db()
        self.item.refresh_from_db()
        snapshot.refresh_from_db()
        self.assertEqual(self.order.status, ShippingOrder.STATUS_SHIPPED)
        self.assertEqual(self.item.qty_shipped, 20)
        self.assertEqual(self.item.qty_reserved, 0)
        self.assertEqual(snapshot.warehouse_state_code, "shipped")
        self.assertEqual(snapshot.shipping_reserved_qty, 0)
        self.assertTrue(snapshot.is_archived)

    def test_ship_order_auto_promotes_ready_for_loading_snapshot_to_loaded_vehicle(self):
        WarehouseStockSnapshot.objects.filter(agency=self.agency).delete()
        snapshot, trip = self._assign_ready_snapshot_to_trip(qty=20, start_loading=False)
        self.item.qty_reserved = 20
        self.item.save(update_fields=["qty_reserved", "updated_at"])

        ship_order(self.order, self.user, shipped_qty_by_item={self.item.id: 20})

        self.order.refresh_from_db()
        self.item.refresh_from_db()
        snapshot.refresh_from_db()
        self.assertEqual(self.order.status, ShippingOrder.STATUS_SHIPPED)
        self.assertEqual(self.item.qty_shipped, 20)
        self.assertEqual(snapshot.current_trip_id, trip.number)
        self.assertEqual(snapshot.warehouse_state_code, "shipped")
        self.assertTrue(snapshot.is_in_vehicle)
        self.assertTrue(snapshot.is_archived)

    def test_ship_order_completes_loading_in_progress_snapshot_via_warehouse_path(self):
        WarehouseStockSnapshot.objects.filter(agency=self.agency).delete()
        snapshot, trip = self._assign_ready_snapshot_to_trip(qty=20, start_loading=True)
        self.item.qty_reserved = 20
        self.item.save(update_fields=["qty_reserved", "updated_at"])
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "loading_in_progress")

        ship_order(self.order, self.user, shipped_qty_by_item={self.item.id: 20})

        self.order.refresh_from_db()
        self.item.refresh_from_db()
        snapshot.refresh_from_db()
        self.assertEqual(self.order.status, ShippingOrder.STATUS_SHIPPED)
        self.assertEqual(self.item.qty_shipped, 20)
        self.assertEqual(snapshot.current_trip_id, trip.number)
        self.assertEqual(snapshot.warehouse_state_code, "shipped")
        self.assertTrue(snapshot.is_in_vehicle)
        self.assertTrue(snapshot.is_archived)

    def test_shipping_stock_picker_uses_warehouse_snapshot_when_legacy_rows_missing(self):
        WarehouseStockSnapshot.objects.filter(agency=self.agency).delete()
        self._create_snapshot_box()

        stock_rows = _shipping_stock_picker_rows(self.agency)

        self.assertEqual(len(stock_rows), 1)
        self.assertEqual(stock_rows[0]["sku_code"], "SKU-001")
        self.assertEqual(stock_rows[0]["available_boxes"], 1)
        self.assertEqual(stock_rows[0]["available_qty"], 100)

    def test_create_pick_tasks_uses_warehouse_snapshot_when_legacy_rows_missing(self):
        WarehouseStockSnapshot.objects.filter(agency=self.agency).delete()
        self._create_snapshot_box()
        self.item.qty_reserved = 20
        self.item.save(update_fields=["qty_reserved", "updated_at"])

        move_ids = create_pick_tasks(
            self.order,
            self.user,
            requested_by_name="Тест Менеджер",
            requested_by_role="manager",
        )

        self.order.refresh_from_db()
        self.assertTrue(move_ids)
        self.assertEqual(self.order.status, ShippingOrder.STATUS_PICKING)
        task = MoveTask.objects.get()
        self.assertEqual(task.pallet_code, "PL-1")
        self.assertEqual(task.to_zone, "OTG")

    def test_shipping_stock_picker_marks_mixed_boxes_by_barcodes(self):
        self._create_snapshot_box(
            order_id="R-1",
            sku="SKU-002",
            name="Товар 2",
            size="43",
            barcode="200000000002",
            qty=100,
            box_code="BX-1",
            pallet_code="PL-1",
        )
        stock_rows = _shipping_stock_picker_rows(self.agency)
        row_1 = next(row for row in stock_rows if row["sku_code"] == "SKU-001")
        row_2 = next(row for row in stock_rows if row["sku_code"] == "SKU-002")
        self.assertTrue(row_1["is_mixed_box"])
        self.assertTrue(row_2["is_mixed_box"])
        self.assertTrue(row_1["mixed_group"])
        self.assertEqual(row_1["mixed_group"], row_2["mixed_group"])

    def test_shipping_stock_picker_marks_mixed_boxes_without_barcodes_by_item_signature(self):
        self._create_snapshot_box(
            order_id="R-2",
            sku="SKU-010",
            name="Товар без ШК",
            size="40",
            barcode="",
            qty=20,
            box_code="BX-NO-BC",
            pallet_code="PL-2",
            cell=2,
            location_code="OS-1-1-1-2",
        )
        self._create_snapshot_box(
            order_id="R-2",
            sku="SKU-010",
            name="Товар без ШК",
            size="42",
            barcode="",
            qty=20,
            box_code="BX-NO-BC",
            pallet_code="PL-2",
            cell=2,
            location_code="OS-1-1-1-2",
        )
        stock_rows = _shipping_stock_picker_rows(self.agency)
        rows = [row for row in stock_rows if row["sku_code"] == "SKU-010"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["is_mixed_box"] for row in rows))
        groups = {row["mixed_group"] for row in rows}
        self.assertEqual(len(groups), 1)

    def test_shipping_stock_picker_hides_box_with_partial_processing_reserve(self):
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="P-1",
            sku_code="SKU-001",
            size="42",
            goods_type="Готовый",
            qty_reserved=20,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        stock_rows = _shipping_stock_picker_rows(self.agency)

        self.assertEqual(stock_rows, [])

    def test_shipping_stock_picker_keeps_only_fully_free_boxes(self):
        self._create_snapshot_box(
            order_id="R-2",
            sku="SKU-001",
            name="Товар 1",
            size="42",
            barcode="200000000001",
            qty=100,
            box_code="BX-2",
            pallet_code="PL-2",
            cell=2,
            location_code="OS-1-1-1-2",
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="P-2",
            sku_code="SKU-001",
            size="42",
            goods_type="Готовый",
            qty_reserved=20,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        stock_rows = _shipping_stock_picker_rows(self.agency)

        self.assertEqual(len(stock_rows), 1)
        self.assertEqual(stock_rows[0]["sku_code"], "SKU-001")
        self.assertEqual(stock_rows[0]["available_boxes"], 1)
        self.assertEqual(stock_rows[0]["available_qty"], 100)

    def test_shipping_stock_picker_exclude_order_restores_current_order_box(self):
        order = ShippingOrder.objects.create(
            number="SO-EXCLUDE-1",
            agency=self.agency,
            status=ShippingOrder.STATUS_RESERVED,
        )
        item = ShippingOrderItem.objects.create(
            order=order,
            sku_code="SKU-001",
            name="Товар 1",
            size="42",
            barcode="200000000001",
            goods_type="Готовый",
            qty_requested=20,
            qty_reserved=20,
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id=order.number,
            sku_code="SKU-001",
            size="42",
            goods_type="Готовый",
            qty_reserved=20,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        self.assertEqual(_shipping_stock_picker_rows(self.agency), [])

        stock_rows = _shipping_stock_picker_rows(self.agency, exclude_order=order)

        self.assertEqual(len(stock_rows), 1)
        self.assertEqual(stock_rows[0]["available_boxes"], 1)
        self.assertEqual(stock_rows[0]["available_qty"], 100)

    def test_shipping_reachtruck_metrics_counts_done_otg_tasks(self):
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id="",
            agency=self.agency,
            destination_zone="OTG",
            status=MoveRequest.STATUS_DONE,
        )
        MoveTask.objects.create(
            request=move_request,
            pallet_code="PL-A",
            to_zone="OTG",
            status=MoveTask.STATUS_DONE,
            payload={"shipping_order_id": self.order.number, "requested_boxes": [{"box_code": "B1"}, {"box_code": "B2"}]},
        )
        MoveTask.objects.create(
            request=move_request,
            pallet_code="PL-B",
            to_zone="OTG",
            status=MoveTask.STATUS_DONE,
            payload={"shipping_order_id": self.order.number, "requested_box": "B3"},
        )
        MoveTask.objects.create(
            request=move_request,
            pallet_code="PL-C",
            to_zone="OBR",
            status=MoveTask.STATUS_DONE,
            payload={"shipping_order_id": self.order.number, "requested_box": "B4"},
        )
        metrics = _shipping_reachtruck_metrics(self.order)
        self.assertEqual(metrics["pallet_count"], 2)
        self.assertEqual(metrics["box_count"], 3)

    def test_shipping_reachtruck_metrics_falls_back_for_done_task_with_stale_payload(self):
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id="",
            agency=self.agency,
            destination_zone="OTG",
            status=MoveRequest.STATUS_DONE,
        )
        MoveTask.objects.create(
            request=move_request,
            pallet_code="PL-STUCK",
            to_zone="OTG",
            status=MoveTask.STATUS_DONE,
            qty_done=1090,
            payload={
                "shipping_order_id": self.order.number,
                "shipping_order_pk": self.order.pk,
                "status": "created",
                "requested_boxes": [],
            },
        )

        metrics = _shipping_reachtruck_metrics(self.order)

        self.assertEqual(metrics["pallet_count"], 1)
        self.assertGreater(metrics["box_count"], 0)

    def test_shipping_detail_hides_otg_packing_label_without_delivered_boxes(self):
        self.order.status = ShippingOrder.STATUS_PICKING
        self.order.save(update_fields=["status", "updated_at"])
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id="",
            agency=self.agency,
            destination_zone="OTG",
            status=MoveRequest.STATUS_DONE,
        )
        MoveTask.objects.create(
            request=move_request,
            pallet_code="PL-A",
            to_zone="OTG",
            status=MoveTask.STATUS_DONE,
            payload={
                "shipping_order_id": self.order.number,
                "shipping_order_pk": self.order.pk,
            },
        )
        staff_user = get_user_model().objects.create_user(username="storekeeper_sync_shipping", password="pwd")
        Employee.objects.create(
            full_name="Кладовщик Синхронизатор",
            role="storekeeper",
            user=staff_user,
            is_active=True,
        )
        self.client.force_login(staff_user)

        response = self.client.get(f"/shipping/{self.order.pk}/")

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, ShippingOrder.STATUS_PICKING)
        self.assertNotContains(response, "Товар доставлен в OTG, ожидает паллетизации")
        self.assertNotContains(response, "Разложить короба по новым паллетам")

    def test_shipping_detail_shows_otg_packing_label_when_delivered_boxes_resolve(self):
        self.order.status = ShippingOrder.STATUS_PICKING
        self.order.save(update_fields=["status", "updated_at"])
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="receiving",
            order_id="R-1",
            action="placement",
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {
                        "code": "BX-1",
                        "qty": 20,
                        "items": [
                            {
                                "sku_code": "SKU-001",
                                "name": "Товар 1",
                                "size": "42",
                                "barcode": "200000000001",
                                "goods_type": "Готовый",
                                "qty": 20,
                            }
                        ],
                    }
                ],
            },
        )
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id="",
            agency=self.agency,
            destination_zone="OTG",
            status=MoveRequest.STATUS_DONE,
        )
        MoveTask.objects.create(
            request=move_request,
            pallet_code="PL-A",
            to_zone="OTG",
            status=MoveTask.STATUS_DONE,
            payload={
                "shipping_order_id": self.order.number,
                "shipping_order_pk": self.order.pk,
                "receiving_order_id": "R-1",
                "picked_boxes": ["BX-1"],
                "picked_rows": [{"box_code": "BX-1", "qty": 20}],
            },
        )
        staff_user = get_user_model().objects.create_user(username="storekeeper_ready_shipping", password="pwd")
        Employee.objects.create(
            full_name="Кладовщик Готовый",
            role="storekeeper",
            user=staff_user,
            is_active=True,
        )
        self.client.force_login(staff_user)

        response = self.client.get(f"/shipping/{self.order.pk}/")

        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, ShippingOrder.STATUS_PICKING)
        self.assertContains(response, "Товар доставлен в OTG, ожидает паллетизации")
        self.assertContains(response, "Разложить короба по новым паллетам")


class ShippingReadModelServiceTests(TestCase):
    def setUp(self):
        self.request_factory = RequestFactory()
        user_model = get_user_model()
        self.manager_user = user_model.objects.create_user(username="shipping_read_manager", password="pwd")
        Employee.objects.create(
            full_name="Shipping Read Manager",
            role="manager",
            user=self.manager_user,
            is_active=True,
        )
        self.storekeeper_user = user_model.objects.create_user(username="shipping_read_storekeeper", password="pwd")
        Employee.objects.create(
            full_name="Shipping Read Storekeeper",
            role="storekeeper",
            user=self.storekeeper_user,
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Shipping Read Agency")
        self.order = ShippingOrder.objects.create(
            number="SO-000777",
            agency=self.agency,
            created_by=self.manager_user,
            status=ShippingOrder.STATUS_SUBMITTED,
        )
        ShippingOrderItem.objects.create(
            order=self.order,
            sku_code="SKU-LIST",
            name="List item",
            size="42",
            barcode="200000007777",
            goods_type="Готовый",
            qty_requested=10,
        )

    def test_build_shipping_list_page_context_applies_filters_and_display_fields(self):
        request = self.request_factory.get(
            reverse("shipping:list"),
            data={"client": str(self.agency.id), "status": ShippingOrder.STATUS_SUBMITTED},
        )
        request.user = self.manager_user

        context = build_shipping_list_page_context(
            request=request,
            scope="staff",
            role="manager",
            client_agency=None,
        )

        orders = list(context["orders"])
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].pk, self.order.pk)
        self.assertEqual(context["selected_client"], self.agency)
        self.assertEqual(context["client_filter"], str(self.agency.id))
        self.assertEqual(orders[0].display_number, _display_shipping_number(self.order.number))

    def test_build_shipping_detail_page_context_adds_transport_note_and_pick_reason(self):
        self.order.status = ShippingOrder.STATUS_STOREKEEPER_ACCEPTED
        self.order.save(update_fields=["status", "updated_at"])
        request = self.request_factory.get(reverse("shipping:detail", args=[self.order.pk]))
        request.user = self.storekeeper_user

        context = build_shipping_detail_page_context(
            request=request,
            order=self.order,
            scope="staff",
            role="storekeeper",
        )

        self.assertEqual(context["order"].pk, self.order.pk)
        self.assertEqual(context["transport_note_url"], reverse("shipping:documents", args=[self.order.pk]))
        self.assertIn("can_storekeeper_pick", context)
        self.assertIn("storekeeper_pick_unavailable_reason", context)


class ShippingManagerTaskFlowTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.client_user = user_model.objects.create_user(username="client_shipping", password="pwd")
        self.manager_user = user_model.objects.create_user(username="manager_shipping", password="pwd")
        self.storekeeper_user = user_model.objects.create_user(username="storekeeper_shipping", password="pwd")
        self.logistician_user = user_model.objects.create_user(username="logistician_shipping", password="pwd")
        self.manager = Employee.objects.create(
            full_name="Менеджер Проверяющий",
            role="manager",
            user=self.manager_user,
            is_active=True,
        )
        self.storekeeper = Employee.objects.create(
            full_name="Кладовщиков Алексей",
            role="storekeeper",
            user=self.storekeeper_user,
            is_active=True,
        )
        self.logistician = Employee.objects.create(
            full_name="Логист Петров",
            role="logistician",
            user=self.logistician_user,
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Клиент отгрузки", portal_user=self.client_user)
        self.market = Market.objects.create(id=301, name="Ozon")
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="R-SH-1",
            sku="SKU-SHIP-1",
            name="Товар отгрузки",
            size="44",
            barcode="300000000001",
            goods_type="Готовый",
            qty=24,
            box_code="BX-SH-1",
            pallet_code="PL-SH-1",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=1,
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="R-SH-2",
            sku="SKU-SHIP-1",
            name="Товар отгрузки",
            size="44",
            barcode="300000000001",
            goods_type="Готовый",
            qty=24,
            box_code="BX-SH-2",
            pallet_code="PL-SH-2",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=2,
        )

    def _submit_payload(self):
        picker_key = _shipping_stock_picker_rows(self.agency)[0]["key"]
        eta = (timezone.localtime() + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
        return {
            "slot_date": eta.strftime("%Y-%m-%d"),
            "slot_time": "12:30",
            "eta_at": eta.strftime("%Y-%m-%dT%H:%M"),
            "shipping_barcode": "SHIP-CLIENT-1",
            "marketplace": str(self.market.id),
            "wb_supply_barcode": "SUPPLY-1",
            "destination_warehouse": "Склад маркетплейса",
            "supply_type": ShippingOrder.SUPPLY_BOX,
            "vehicle_type": ShippingOrder.VEHICLE_FULFILLMENT,
            "vehicle_number": "A123BC77",
            "driver_phone": "+7 900 000-00-00",
            "comment": "Клиентская отправка",
            "action": "submit",
            "stock_key_all[]": [picker_key],
            "stock_boxes[]": ["1"],
        }

    def _create_order_ready_for_packing(self):
        order = ShippingOrder.objects.create(
            number="SO-000010",
            agency=self.agency,
            created_by=self.manager_user,
            status=ShippingOrder.STATUS_PICKING,
            expected_boxes=2,
            shipping_barcode="SHIP-PACK-1",
        )
        ShippingOrderItem.objects.create(
            order=order,
            sku_code="SKU-SHIP-1",
            name="Товар отгрузки",
            size="44",
            barcode="300000000001",
            goods_type="Готовый",
            qty_requested=48,
            qty_reserved=48,
        )
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="receiving",
            order_id="R-SH-1",
            action="placement",
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {
                        "code": "BX-SH-1",
                        "qty": 24,
                        "items": [
                            {"sku_code": "SKU-SHIP-1", "name": "Товар отгрузки", "size": "44", "barcode": "300000000001", "goods_type": "Готовый", "qty": 24}
                        ],
                    },
                    {
                        "code": "BX-SH-2",
                        "qty": 24,
                        "items": [
                            {"sku_code": "SKU-SHIP-1", "name": "Товар отгрузки", "size": "44", "barcode": "300000000001", "goods_type": "Готовый", "qty": 24}
                        ],
                    },
                ],
            },
        )
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id=f"shipping:{order.pk}",
            agency=self.agency,
            destination_zone="OTG",
            status=MoveRequest.STATUS_DONE,
        )
        MoveTask.objects.create(
            request=move_request,
            pallet_code="PL-OTG-1",
            to_zone="OTG",
            status=MoveTask.STATUS_DONE,
            qty_done=48,
            payload={
                "shipping_order_id": order.number,
                "shipping_order_pk": order.pk,
                "receiving_order_id": "R-SH-1",
                "picked_boxes": ["BX-SH-1", "BX-SH-2"],
                "picked_rows": [
                    {"box_code": "BX-SH-1", "qty": 24},
                    {"box_code": "BX-SH-2", "qty": 24},
                ],
            },
        )
        return order

    def _mark_transport_note_ready(
        self,
        order: ShippingOrder,
        *,
        vehicle_type: str = ShippingOrder.VEHICLE_CLIENT,
    ) -> ShippingOrder:
        order.status = ShippingOrder.STATUS_PACKED
        order.vehicle_type = vehicle_type
        order.save(update_fields=["status", "vehicle_type", "updated_at"])
        if vehicle_type == ShippingOrder.VEHICLE_FULFILLMENT:
            trip = LogisticsTrip.objects.create(
                number=f"TRIP-TN-{order.pk}",
                status=LogisticsTrip.STATUS_DEPARTED,
                vehicle_type=vehicle_type,
                created_by=self.logistician_user,
                assigned_logistician=self.logistician,
            )
            LogisticsTripOrder.objects.create(
                trip=trip,
                shipping_order=order,
                loading_sequence=1,
                delivery_sequence=1,
            )
        return order

    def _packing_rows_payload(self):
        return [
            {
                "row_key": _shipping_box_row_key({"code": "BX-SH-1", "receiving_order_id": "R-SH-1"}, 1),
                "code": "BX-SH-1",
                "qty": 24,
                "barcode_preview": "300000000001 - 24 шт.",
                "pallet_code": "PAL-1",
            },
            {
                "row_key": _shipping_box_row_key({"code": "BX-SH-2", "receiving_order_id": "R-SH-1"}, 2),
                "code": "BX-SH-2",
                "qty": 24,
                "barcode_preview": "300000000001 - 24 шт.",
                "pallet_code": "PAL-2",
            },
        ]

    def test_client_submit_creates_manager_review_task(self):
        self.client.force_login(self.client_user)

        response = self.client.post("/shipping/new/", data=self._submit_payload())

        self.assertEqual(response.status_code, 302)
        order = ShippingOrder.objects.get()
        task = Task.objects.get(route=f"/shipping/{order.pk}/")
        self.assertEqual(order.status, ShippingOrder.STATUS_SUBMITTED)
        self.assertEqual(order.expected_boxes, 1)
        self.assertEqual(str(order.slot_date), (timezone.localtime() + timedelta(days=2)).date().isoformat())
        self.assertEqual(task.assigned_to, self.manager)
        self.assertEqual(task.status, "backlog")
        self.assertIn(order.number, task.title)

    def test_manager_approval_closes_review_task_and_creates_storekeeper_task(self):
        self.client.force_login(self.client_user)
        self.client.post("/shipping/new/", data=self._submit_payload())
        order = ShippingOrder.objects.get()
        manager_task = Task.objects.get(route=f"/shipping/{order.pk}/", assigned_to=self.manager)

        self.client.force_login(self.manager_user)
        response = self.client.post(f"/shipping/{order.pk}/", data={"action": "reserve"})

        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        manager_task.refresh_from_db()
        storekeeper_task = Task.objects.get(route=f"/shipping/{order.pk}/", assigned_to=self.storekeeper)
        self.assertEqual(order.status, ShippingOrder.STATUS_RESERVED)
        self.assertEqual(manager_task.status, "done")
        self.assertEqual(storekeeper_task.status, "backlog")
        self.assertEqual(storekeeper_task.title, f"Заявка на отгрузку №{order.number}")

    def test_storekeeper_can_accept_order_into_work(self):
        self.client.force_login(self.client_user)
        self.client.post("/shipping/new/", data=self._submit_payload())
        order = ShippingOrder.objects.get()

        self.client.force_login(self.manager_user)
        self.client.post(f"/shipping/{order.pk}/", data={"action": "reserve"})

        self.client.force_login(self.storekeeper_user)
        response = self.client.post(f"/shipping/{order.pk}/", data={"action": "accept_storekeeper"})

        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        storekeeper_task = Task.objects.get(route=f"/shipping/{order.pk}/", assigned_to=self.storekeeper)
        self.assertEqual(order.status, ShippingOrder.STATUS_STOREKEEPER_ACCEPTED)
        self.assertEqual(storekeeper_task.status, "in_progress")

    def test_storekeeper_detail_shows_accept_button_before_pick_tasks(self):
        self.client.force_login(self.client_user)
        self.client.post("/shipping/new/", data=self._submit_payload())
        order = ShippingOrder.objects.get()

        self.client.force_login(self.manager_user)
        self.client.post(f"/shipping/{order.pk}/", data={"action": "reserve"})

        self.client.force_login(self.storekeeper_user)
        response = self.client.get(f"/shipping/{order.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Принять в работу")
        self.assertNotContains(response, "Дать ричтраку доставку в OTG")

    def test_storekeeper_detail_hides_transport_note_link_before_loading(self):
        self.client.force_login(self.client_user)
        self.client.post("/shipping/new/", data=self._submit_payload())
        order = ShippingOrder.objects.get()

        self.client.force_login(self.manager_user)
        self.client.post(f"/shipping/{order.pk}/", data={"action": "reserve"})

        self.client.force_login(self.storekeeper_user)
        response = self.client.get(f"/shipping/{order.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, f"/shipping/{order.pk}/documents/")
        self.assertNotContains(response, "Сопроводительные документы")

    def test_storekeeper_detail_hides_pick_button_when_reserved_stock_has_no_pallets(self):
        self.client.force_login(self.client_user)
        self.client.post("/shipping/new/", data=self._submit_payload())
        order = ShippingOrder.objects.get()

        self.client.force_login(self.manager_user)
        self.client.post(f"/shipping/{order.pk}/", data={"action": "reserve"})
        self.client.force_login(self.storekeeper_user)
        self.client.post(f"/shipping/{order.pk}/", data={"action": "accept_storekeeper"})
        WarehouseContainer.objects.filter(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
        ).update(parent_container=None)
        WarehouseStockSnapshot.objects.filter(agency=self.agency).update(parent_container=None)

        response = self.client.get(f"/shipping/{order.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Дать ричтраку доставку в OTG")
        self.assertContains(response, "зарезервированный товар не размещен на паллетах")

    def test_storekeeper_pick_returns_specific_reason_when_stock_has_no_pallets(self):
        self.client.force_login(self.client_user)
        self.client.post("/shipping/new/", data=self._submit_payload())
        order = ShippingOrder.objects.get()

        self.client.force_login(self.manager_user)
        self.client.post(f"/shipping/{order.pk}/", data={"action": "reserve"})
        self.client.force_login(self.storekeeper_user)
        self.client.post(f"/shipping/{order.pk}/", data={"action": "accept_storekeeper"})
        WarehouseContainer.objects.filter(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
        ).update(parent_container=None)
        WarehouseStockSnapshot.objects.filter(agency=self.agency).update(parent_container=None)

        response = self.client.post(f"/shipping/{order.pk}/", data={"action": "create_pick_tasks"}, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "зарезервированный товар не размещен на паллетах")
        self.assertEqual(MoveRequest.objects.count(), 0)

    def test_storekeeper_detail_shows_transport_note_link_after_loading_completed(self):
        order = self._create_order_ready_for_packing()
        order.status = ShippingOrder.STATUS_PACKED
        order.vehicle_type = ShippingOrder.VEHICLE_FULFILLMENT
        order.save(update_fields=["status", "vehicle_type", "updated_at"])
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="shipping",
            order_id=order.number,
            action="status",
            user=self.storekeeper_user,
            description="Паллетизация завершена",
            payload={
                "act": "shipping_packing",
                "act_state": "closed",
                "pallet_count": 2,
                "delivered_box_count": 2,
                "act_pallets": [
                    {"label": "1", "code": "PAL-1", "boxes": ["BX-SH-1"]},
                    {"label": "2", "code": "PAL-2", "boxes": ["BX-SH-2"]},
                ],
                "act_boxes": [
                    {"code": "BX-SH-1", "qty": 24, "pallet_label": "1", "items": []},
                    {"code": "BX-SH-2", "qty": 24, "pallet_label": "2", "items": []},
                ],
            },
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-DOCS-0001",
            status=LogisticsTrip.STATUS_LOADING,
            vehicle_type=ShippingOrder.VEHICLE_FULFILLMENT,
            created_by=self.logistician_user,
            assigned_logistician=self.logistician,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        OrderAuditEntry.objects.create(
            order_id=str(trip.pk),
            order_type="logistics_trip",
            action="update",
            agency=self.agency,
            user=self.storekeeper_user,
            description="Все паллеты погружены",
            payload={
                "act": "trip_loading_progress",
                "trip_pk": trip.pk,
                "loaded_pallet_keys": ["PAL-1", "PAL-2"],
                "last_loaded_key": "PAL-2",
            },
        )

        self.client.force_login(self.storekeeper_user)
        response = self.client.get(f"/shipping/{order.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"/shipping/{order.pk}/documents/")
        self.assertContains(response, "Сопроводительные документы")

    def test_manager_detail_shows_edit_button_for_submitted_order(self):
        self.client.force_login(self.client_user)
        self.client.post("/shipping/new/", data=self._submit_payload())
        order = ShippingOrder.objects.get()

        self.client.force_login(self.manager_user)
        response = self.client.get(f"/shipping/{order.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"/shipping/new/?order={order.pk}&amp;edit=1")
        self.assertContains(response, "Отредактировать заявку")
        self.assertNotContains(response, "Добавить позицию")
        self.assertContains(response, "Главная роли")
        self.assertNotContains(response, "Главная клиента")
        self.assertContains(response, "Текущий пользователь: Менеджер Проверяющий")
        self.assertNotContains(response, "Текущий пользователь: Клиент")

    def test_manager_can_edit_submitted_order_in_create_like_form(self):
        self.client.force_login(self.client_user)
        self.client.post("/shipping/new/", data=self._submit_payload())
        order = ShippingOrder.objects.get()
        row_key = _shipping_stock_picker_rows(self.agency, exclude_order=order)[0]["key"]
        edit_eta = (timezone.localtime() + timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)

        self.client.force_login(self.manager_user)
        response = self.client.post(
            f"/shipping/new/?order={order.pk}&edit=1",
            data={
                "edit_order_id": str(order.pk),
                "edit": "1",
                "agency": str(self.agency.id),
                "slot_date": edit_eta.strftime("%Y-%m-%d"),
                "slot_time": "15:00",
                "eta_at": edit_eta.strftime("%Y-%m-%dT%H:%M"),
                "shipping_barcode": "SHIP-CLIENT-EDIT-1",
                "marketplace": str(self.market.id),
                "wb_supply_barcode": "SUPPLY-EDIT-1",
                "destination_warehouse": "Склад после редактирования",
                "supply_type": ShippingOrder.SUPPLY_BOX,
                "vehicle_type": ShippingOrder.VEHICLE_FULFILLMENT,
                "vehicle_number": "E555EE77",
                "driver_phone": "+7 911 000-00-00",
                "comment": "Менеджер отредактировал заявку",
                "action": "update",
                "stock_key_all[]": [row_key],
                "stock_boxes[]": ["2"],
            },
        )

        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        item = order.items.get()
        task = Task.objects.get(route=f"/shipping/{order.pk}/")
        self.assertEqual(order.status, ShippingOrder.STATUS_SUBMITTED)
        self.assertEqual(order.shipping_barcode, "SHIP-CLIENT-EDIT-1")
        self.assertEqual(order.wb_supply_barcode, "SUPPLY-EDIT-1")
        self.assertEqual(str(order.slot_date), edit_eta.date().isoformat())
        self.assertEqual(order.slot_time.strftime("%H:%M"), "15:00")
        self.assertEqual(order.destination_warehouse, "Склад после редактирования")
        self.assertEqual(order.vehicle_number, "E555EE77")
        self.assertEqual(order.expected_boxes, 2)
        self.assertEqual(item.qty_requested, 48)
        self.assertEqual(item.qty_reserved, 48)
        self.assertEqual(task.status, "backlog")

    def test_client_submit_persists_ozon_slot_time(self):
        self.client.force_login(self.client_user)

        response = self.client.post("/shipping/new/", data=self._submit_payload())

        self.assertEqual(response.status_code, 302)
        order = ShippingOrder.objects.get()
        self.assertEqual(order.marketplace, self.market)
        self.assertEqual(order.slot_time.strftime("%H:%M"), "12:30")

    def test_storekeeper_can_save_shipping_packing_act(self):
        order = self._create_order_ready_for_packing()

        self.client.force_login(self.storekeeper_user)
        response = self.client.get(f"/shipping/{order.pk}/packing/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Паллетизация отгрузки · заявка 10_OTG")
        self.assertContains(response, "BX-SH-1")
        self.assertContains(response, "BX-SH-2")

        response = self.client.post(
            f"/shipping/{order.pk}/packing/",
            data={
                "boxes_json": json.dumps(
                    self._packing_rows_payload()
                ),
                "pallets_json": json.dumps(
                    [
                        {"code": "PAL-1", "label": "1"},
                        {"code": "PAL-2", "label": "2"},
                    ]
                ),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"/shipping/{order.pk}/packing-slips/")
        order.refresh_from_db()
        self.assertEqual(order.status, ShippingOrder.STATUS_PACKED)
        packing_entry = OrderAuditEntry.objects.filter(
            agency=self.agency,
            order_type="shipping",
            order_id=order.number,
            payload__act="shipping_packing",
        ).latest("created_at")
        self.assertEqual(packing_entry.payload["pallet_count"], 2)
        self.assertEqual(packing_entry.payload["delivered_box_count"], 2)
        self.assertEqual(
            [pallet["label"] for pallet in packing_entry.payload["act_pallets"]],
            ["1", "2"],
        )
        self.assertEqual(
            [box["code"] for box in packing_entry.payload["act_boxes"]],
            ["BX-SH-1", "BX-SH-2"],
        )

        detail_response = self.client.get(f"/shipping/{order.pk}/")
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "Новые паллеты отгрузки")
        self.assertContains(detail_response, "SHIP-")
        self.assertContains(detail_response, "BX-SH-1")

    def test_packing_slips_page_contains_preview_print_controls_and_qr(self):
        order = self._create_order_ready_for_packing()
        order.marketplace = self.market
        order.wb_supply_barcode = "SUPPLY-PACK-1"
        order.destination_warehouse = "Новосемейкино"
        order.wb_transit_warehouse = True
        order.transit_address = "Транзитный терминал Голёво"
        order.supply_type = ShippingOrder.SUPPLY_BOX
        order.slot_date = datetime(2026, 4, 7).date()
        order.slot_time = datetime.strptime("08:30", "%H:%M").time()
        order.save(
            update_fields=[
                "marketplace",
                "wb_supply_barcode",
                "destination_warehouse",
                "wb_transit_warehouse",
                "transit_address",
                "supply_type",
                "slot_date",
                "slot_time",
                "updated_at",
            ]
        )

        self.client.force_login(self.storekeeper_user)
        self.client.post(
            f"/shipping/{order.pk}/packing/",
            data={
                "boxes_json": json.dumps(self._packing_rows_payload()),
                "pallets_json": json.dumps(
                    [
                        {"code": "PAL-1", "label": "1"},
                        {"code": "PAL-2", "label": "2"},
                    ]
                ),
            },
        )

        packing_response = self.client.get(f"/shipping/{order.pk}/packing/")
        self.assertEqual(packing_response.status_code, 403)

        response = self.client.get(f"/shipping/{order.pk}/packing-slips/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Упаковочные листы 75x120")
        self.assertContains(response, "Предпросмотр")
        self.assertContains(response, "Печать всех")
        self.assertContains(response, 'data-label-printer', html=False)
        self.assertContains(response, "Модуль печати")
        self.assertContains(response, "Скачать Fullbox Agent")
        self.assertContains(response, "/static/vendor/qrcode.min.js")
        self.assertContains(response, "/static/vendor/html2canvas.min.js")
        self.assertContains(response, "На каждом листе есть QR-код паллеты.")
        self.assertContains(response, "Статус печати:")
        self.assertNotContains(response, "Автозапуск агента")
        self.assertNotContains(response, "Скрипт агента")
        self.assertNotContains(response, "Скачать агента")
        self.assertNotContains(response, "Синхронизировать")
        self.assertNotContains(response, "Остановить печать")
        self.assertNotContains(response, "Сбросить печать")
        self.assertNotContains(response, "Очистить очередь")
        self.assertEqual(len(response.context["packing_slips_data"]), 2)
        self.assertIn("print_status_line", response.context)
        self.assertIn("available_printers_meta", response.context)
        self.assertEqual(response.context["packing_slips_data"][0]["shipping_barcode"], "SHIP-PACK-1")
        self.assertEqual(response.context["packing_slips_data"][0]["supply_number"], "SUPPLY-PACK-1")
        self.assertEqual(response.context["packing_slips_data"][0]["destination_warehouse"], "Новосемейкино")
        self.assertEqual(response.context["packing_slips_data"][0]["transit_address"], "Транзитный терминал Голёво")
        self.assertEqual(response.context["packing_slips_data"][0]["qr_value"], "SO-000010::PAL-1")

    def test_client_detail_hides_pallet_and_box_breakdown_after_packing(self):
        order = self._create_order_ready_for_packing()

        self.client.force_login(self.storekeeper_user)
        self.client.post(
            f"/shipping/{order.pk}/packing/",
            data={
                "boxes_json": json.dumps(self._packing_rows_payload()),
                "pallets_json": json.dumps(
                    [
                        {"code": "PAL-1", "label": "1"},
                        {"code": "PAL-2", "label": "2"},
                    ]
                ),
            },
        )

        self.client.force_login(self.client_user)
        response = self.client.get(f"/shipping/{order.pk}/?client={self.agency.id}")

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Новые паллеты отгрузки")
        self.assertNotContains(response, "BX-SH-1")
        self.assertNotContains(response, "PAL-1")

    def test_client_detail_uses_departed_trip_status_and_vehicle_data(self):
        order = self._create_order_ready_for_packing()
        order.status = ShippingOrder.STATUS_PACKED
        order.vehicle_type = ShippingOrder.VEHICLE_FULFILLMENT
        order.vehicle_number = ""
        order.driver_phone = ""
        order.save(update_fields=["status", "vehicle_type", "vehicle_number", "driver_phone", "updated_at"])

        trip = LogisticsTrip.objects.create(
            number="4_RS",
            status=LogisticsTrip.STATUS_DEPARTED,
            vehicle_type=LogisticsTrip.VEHICLE_FULFILLMENT,
            vehicle_number="Е777ЕЕ77",
            driver_name="Иван Петров",
            driver_phone="+7 911 222-33-44",
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )

        self.client.force_login(self.client_user)
        response = self.client.get(f"/shipping/{order.pk}/?client={self.agency.id}")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Загружено в машину")
        self.assertContains(response, "Е777ЕЕ77")
        self.assertContains(response, "Иван Петров")
        self.assertContains(response, "+7 911 222-33-44")
        self.assertNotContains(response, "Логист сформировал рейс, ожидается погрузка")
        self.assertNotContains(response, "Открыть рейс 4_RS")
        self.assertContains(response, "Открыть акт отгрузки")
        self.assertContains(response, 'class="summary-table"', html=False)
        self.assertNotContains(response, 'class="chip', html=False)

    def test_storekeeper_can_open_and_save_transport_note(self):
        self.client.force_login(self.client_user)
        self.client.post("/shipping/new/", data=self._submit_payload())
        order = ShippingOrder.objects.get()

        self.client.force_login(self.manager_user)
        self.client.post(f"/shipping/{order.pk}/", data={"action": "reserve"})
        order.refresh_from_db()
        order.status = ShippingOrder.STATUS_PACKED
        order.vehicle_type = ShippingOrder.VEHICLE_CLIENT
        order.save(update_fields=["status", "vehicle_type", "updated_at"])

        self.client.force_login(self.storekeeper_user)
        response = self.client.get(f"/shipping/{order.pk}/documents/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Сопроводительные документы")
        self.assertNotContains(response, "/transport-note/pdf/")
        self.assertContains(response, "/transport-note/docx/")
        self.assertContains(response, "/transport-note/docx/?inline=1")
        self.assertContains(response, "/return-act/doc/")
        self.assertContains(response, "/return-act/doc/?inline=1")
        self.assertNotContains(response, "/transport-note/docx-preview/")
        self.assertContains(response, "Открыть DOCX")
        self.assertContains(response, "Скачать DOCX")
        self.assertContains(response, "Акт возврата")
        self.assertContains(response, "Открыть DOCX")
        self.assertContains(response, "Скачать DOCX")

        response = self.client.post(
            f"/shipping/{order.pk}/transport-note/",
            data={
                "document_number": "ТН-2026-001",
                "document_date": "2026-04-12",
                "shipper_name": "ООО Кейз",
                "shipper_inn": "7701234567",
                "shipper_address": "Москва, ул. Тестовая, 1",
                "shipper_phone": "+7 900 111-22-33",
                "consignee_name": "WB / Склад маркетплейса",
                "consignee_inn": "",
                "consignee_address": "МО, склад назначения",
                "consignee_phone": "",
                "carrier_name": "FullBox",
                "carrier_inn": "",
                "carrier_address": "Старая Купавна",
                "carrier_phone": "+7 499 450-35-55",
                "loading_address": "Склад FullBox",
                "unloading_address": "Склад WB",
                "cargo_name": "Тестовый груз",
                "cargo_package_count": "3",
                "cargo_package_type": "Короб",
                "cargo_weight_kg": "25.500",
                "cargo_declared_value": "150000.00",
                "accompanying_documents": "Заявка и УПД",
                "special_instructions": "Без повреждений",
                "transportation_conditions": "Доставка до склада",
                "delivery_notes": "Без замечаний",
                "driver_name": "Иванов И.И.",
                "driver_phone": "+7 900 123-45-67",
                "vehicle_number": "A123BC77",
                "trailer_number": "TR-01",
                "service_cost": "5000.00",
            },
        )

        self.assertEqual(response.status_code, 302)
        note = ShippingTransportNote.objects.get(order=order)
        self.assertEqual(note.document_number, "ТН-2026-001")
        self.assertEqual(note.cargo_package_count, 3)
        self.assertEqual(note.driver_name, "Иванов И.И.")

        pdf_response = self.client.get(f"/shipping/{order.pk}/transport-note/pdf/")
        self.assertEqual(pdf_response.status_code, 200)
        self.assertEqual(pdf_response["Content-Type"], "application/pdf")
        self.assertIn(b"%PDF", pdf_response.content[:8])

        docx_response = self.client.get(f"/shipping/{order.pk}/transport-note/docx/")
        self.assertEqual(docx_response.status_code, 200)
        self.assertEqual(
            docx_response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertIn(b"PK", docx_response.content[:4])
        self.assertIn("attachment", docx_response["Content-Disposition"].lower())

        inline_response = self.client.get(f"/shipping/{order.pk}/transport-note/docx/?inline=1")
        self.assertEqual(inline_response.status_code, 200)
        self.assertEqual(
            inline_response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertIn("inline", inline_response["Content-Disposition"].lower())
        self.assertIn(b"PK", inline_response.content[:4])

        return_act_response = self.client.get(f"/shipping/{order.pk}/return-act/doc/")
        self.assertEqual(return_act_response.status_code, 200)
        self.assertEqual(
            return_act_response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertIn("attachment", return_act_response["Content-Disposition"].lower())
        self.assertIn(b"PK", return_act_response.content[:4])
        with zipfile.ZipFile(BytesIO(return_act_response.content)) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8")
        self.assertIn(order.number, document_xml)
        self.assertIn(order.agency.agn_name, document_xml)
        self.assertIn("Товар отгрузки", document_xml)

        return_act_inline = self.client.get(f"/shipping/{order.pk}/return-act/doc/?inline=1")
        self.assertEqual(return_act_inline.status_code, 200)
        self.assertEqual(
            return_act_inline["Content-Type"],
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertIn("inline", return_act_inline["Content-Disposition"].lower())
        self.assertIn(b"PK", return_act_inline.content[:4])

    def test_transport_note_is_forbidden_before_shipping_ready(self):
        self.client.force_login(self.client_user)
        self.client.post("/shipping/new/", data=self._submit_payload())
        order = ShippingOrder.objects.get()

        self.client.force_login(self.manager_user)
        self.client.post(f"/shipping/{order.pk}/", data={"action": "reserve"})

        self.client.force_login(self.storekeeper_user)
        detail_response = self.client.get(f"/shipping/{order.pk}/")
        documents_response = self.client.get(f"/shipping/{order.pk}/documents/")
        transport_note_response = self.client.get(f"/shipping/{order.pk}/transport-note/")
        docx_response = self.client.get(f"/shipping/{order.pk}/transport-note/docx/")
        return_act_response = self.client.get(f"/shipping/{order.pk}/return-act/doc/")

        self.assertEqual(detail_response.status_code, 200)
        self.assertNotContains(detail_response, "Сопроводительные документы")
        self.assertEqual(documents_response.status_code, 403)
        self.assertEqual(transport_note_response.status_code, 403)
        self.assertEqual(docx_response.status_code, 403)
        self.assertEqual(return_act_response.status_code, 403)

    def test_transport_note_uses_directories_for_shipper_carrier_customer_and_consignee(self):
        OwnCompany.objects.create(
            name='Общество с ограниченной ответственностью "ФуллБокс"',
            inn="5001149130",
            address="Московская область, Старая Купавна, Магистральная, 59",
            postal_address="Склад FullBox, Московская область, Старая Купавна, Магистральная, 59",
            phone="+7 977 808-83-86",
            is_default=True,
            is_active=True,
        )
        Carrier.objects.create(
            name="Индивидуальный предприниматель Касаев Абдурашид Метханович",
            inn="052903630023",
            address="Дагестан Респ, Сулейман-Стальский м.р-н, село Орта-Стал с.п.",
            postal_address="Дагестан Респ, Сулейман-Стальский м.р-н, село Орта-Стал с.п.",
            phone="+7 (927) 157-22-42",
            is_active=True,
        )
        self.agency.agn_name = 'Общество с ограниченной ответственностью "Кейзи"'
        self.agency.save(update_fields=["agn_name", "short_name"])
        self.client.force_login(self.client_user)
        payload = self._submit_payload()
        payload["destination_warehouse"] = "Санкт_Петербург_РФЦ_1"
        with patch("shipping.transport_note.load_marketplace_warehouse_catalog") as catalog_mock:
            catalog_mock.return_value = {
                "wb": [],
                "ozon": [
                    {
                        "type": "Фулфилмент",
                        "name": "Санкт_Петербург_РФЦ_1",
                        "address": "Санкт-Петербург, склад ОЗОН",
                    }
                ],
                "yandex": [],
                "sber": [],
            }
            self.client.post("/shipping/new/", data=payload)
        order = ShippingOrder.objects.get()

        self.client.force_login(self.manager_user)
        self.client.post(f"/shipping/{order.pk}/", data={"action": "reserve"})
        self._mark_transport_note_ready(order, vehicle_type=ShippingOrder.VEHICLE_FULFILLMENT)

        self.client.force_login(self.storekeeper_user)
        with patch("shipping.transport_note.load_marketplace_warehouse_catalog") as catalog_mock:
            catalog_mock.return_value = {
                "wb": [],
                "ozon": [
                    {
                        "type": "Фулфилмент",
                        "name": "Санкт_Петербург_РФЦ_1",
                        "address": "Санкт-Петербург, склад ОЗОН",
                    }
                ],
                "yandex": [],
                "sber": [],
            }
            response = self.client.get(f"/shipping/{order.pk}/transport-note/")

        note = ShippingTransportNote.objects.get(order=order)
        self.assertEqual(note.shipper_name, 'ООО "ФуллБокс"')
        self.assertEqual(note.shipper_inn, "5001149130")
        self.assertEqual(note.shipper_address, "Склад FullBox, Московская область, Старая Купавна, Магистральная, 59")
        self.assertEqual(note.shipper_phone, "+7 977 808-83-86")
        self.assertEqual(note.carrier_name, "ИП Касаев Абдурашид Метханович")
        self.assertEqual(note.carrier_inn, "052903630023")
        self.assertEqual(note.carrier_address, "Дагестан Респ, Сулейман-Стальский м.р-н, село Орта-Стал с.п.")
        self.assertEqual(note.carrier_phone, "+7 (927) 157-22-42")
        self.assertEqual(note.loading_address, "Склад FullBox, Московская область, Старая Купавна, Магистральная, 59")
        self.assertEqual(note.consignee_name, "Ozon / Санкт_Петербург_РФЦ_1")
        self.assertContains(response, "ООО &quot;Кейзи&quot;")
        self.assertContains(response, "ООО &quot;ФуллБокс&quot;")
        self.assertContains(response, "ИП Касаев Абдурашид Метханович")
        self.assertContains(response, "Ozon / Санкт_Петербург_РФЦ_1")

    def test_transport_note_refreshes_legacy_carrier_requisites_from_directory(self):
        OwnCompany.objects.create(
            name='Общество с ограниченной ответственностью "ФуллБокс"',
            inn="5001149130",
            postal_address="Склад FullBox, Московская область, Старая Купавна, Магистральная, 59",
            phone="+7 977 808-83-86",
            is_default=True,
            is_active=True,
        )
        Carrier.objects.create(
            name="Индивидуальный предприниматель Касаев Абдурашид Метханович",
            inn="052903630023",
            address="Дагестан Респ, Сулейман-Стальский м.р-н, село Орта-Стал с.п.",
            postal_address="Дагестан Респ, Сулейман-Стальский м.р-н, село Орта-Стал с.п.",
            phone="+7 (927) 157-22-42",
            is_active=True,
        )
        self.client.force_login(self.client_user)
        self.client.post("/shipping/new/", data=self._submit_payload())
        order = ShippingOrder.objects.get()
        self._mark_transport_note_ready(order, vehicle_type=ShippingOrder.VEHICLE_FULFILLMENT)
        ShippingTransportNote.objects.update_or_create(
            order=order,
            defaults={
                "document_number": order.number,
                "shipper_name": 'ООО "ФуллБокс"',
                "shipper_inn": "5001149130",
                "shipper_address": "Склад FullBox, Московская область, Старая Купавна, Магистральная, 59",
                "shipper_phone": "+7 977 808-83-86",
                "carrier_name": "FullBox",
                "carrier_inn": "5001149130",
                "carrier_address": "Склад FullBox, Московская область, Старая Купавна, Магистральная, 59",
                "carrier_phone": "+7 977 808-83-86",
            },
        )

        self.client.force_login(self.storekeeper_user)
        self.client.get(f"/shipping/{order.pk}/transport-note/")

        note = ShippingTransportNote.objects.get(order=order)
        self.assertEqual(note.carrier_name, "ИП Касаев Абдурашид Метханович")
        self.assertEqual(note.carrier_inn, "052903630023")
        self.assertEqual(note.carrier_address, "Дагестан Респ, Сулейман-Стальский м.р-н, село Орта-Стал с.п.")
        self.assertEqual(note.carrier_phone, "+7 (927) 157-22-42")

    def test_transport_note_switches_to_transit_address_when_order_gets_transit_route(self):
        self.client.force_login(self.client_user)
        payload = self._submit_payload()
        payload["transit_address"] = ""
        payload["wb_transit_warehouse"] = ""
        self.client.post("/shipping/new/", data=payload)
        order = ShippingOrder.objects.get()
        self._mark_transport_note_ready(order)

        self.client.force_login(self.storekeeper_user)
        self.client.get(f"/shipping/{order.pk}/transport-note/")

        note = ShippingTransportNote.objects.get(order=order)
        initial_address = note.consignee_address
        self.assertTrue(initial_address)
        self.assertEqual(note.unloading_address, initial_address)

        order.wb_transit_warehouse = True
        order.transit_address = "Москва, Транзитный терминал 1"
        order.save(update_fields=["wb_transit_warehouse", "transit_address", "updated_at"])

        self.client.get(f"/shipping/{order.pk}/transport-note/")

        note.refresh_from_db()
        self.assertEqual(note.consignee_address, "Москва, Транзитный терминал 1")
        self.assertEqual(note.unloading_address, "Москва, Транзитный терминал 1")

    def test_client_cannot_access_transport_note(self):
        self.client.force_login(self.client_user)
        self.client.post("/shipping/new/", data=self._submit_payload())
        order = ShippingOrder.objects.get()

        response = self.client.get(f"/shipping/{order.pk}/transport-note/")

        self.assertEqual(response.status_code, 403)

        pdf_response = self.client.get(f"/shipping/{order.pk}/transport-note/pdf/")
        self.assertEqual(pdf_response.status_code, 403)

        docx_response = self.client.get(f"/shipping/{order.pk}/transport-note/docx/")
        self.assertEqual(docx_response.status_code, 403)
        inline_response = self.client.get(f"/shipping/{order.pk}/transport-note/docx/?inline=1")
        self.assertEqual(inline_response.status_code, 403)

    def test_shipping_packing_transfers_order_from_storekeeper_to_logistician(self):
        order = self._create_order_ready_for_packing()
        storekeeper_task = Task.objects.create(
            title=f"Заявка на отгрузку №{order.number}",
            route=f"/shipping/{order.pk}/",
            assigned_to=self.storekeeper,
            status="in_progress",
        )

        self.client.force_login(self.storekeeper_user)
        response = self.client.post(
            f"/shipping/{order.pk}/packing/",
            data={
                "boxes_json": json.dumps(
                    self._packing_rows_payload()
                ),
                "pallets_json": json.dumps(
                    [
                        {"code": "PAL-1", "label": "1"},
                        {"code": "PAL-2", "label": "2"},
                    ]
                ),
            },
        )

        self.assertEqual(response.status_code, 302)
        storekeeper_task.refresh_from_db()
        logistician_task = Task.objects.get(route=f"/shipping/{order.pk}/", assigned_to=self.logistician)
        self.assertEqual(storekeeper_task.status, "done")
        self.assertEqual(logistician_task.status, "backlog")
        self.assertIn("требуется погрузка и акт отгрузки", logistician_task.description.lower())

        detail_response = self.client.get(f"/shipping/{order.pk}/")
        self.assertNotContains(detail_response, "Фиксация отгрузки")
        self.assertContains(detail_response, "Логистика и закрытие заявки")

    def test_fulfillment_transport_dispatch_act_requires_trip(self):
        order = self._create_order_ready_for_packing()
        order.vehicle_type = ShippingOrder.VEHICLE_FULFILLMENT
        order.status = ShippingOrder.STATUS_PACKED
        order.save(update_fields=["vehicle_type", "status", "updated_at"])
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="shipping",
            order_id=order.number,
            action="status",
            user=self.storekeeper_user,
            description="Паллетизация завершена",
            payload={
                "act": "shipping_packing",
                "act_state": "closed",
                "pallet_count": 2,
                "delivered_box_count": 2,
                "act_pallets": [
                    {"label": "1", "code": "1", "boxes": ["BX-SH-1"]},
                    {"label": "2", "code": "2", "boxes": ["BX-SH-2"]},
                ],
                "act_boxes": [
                    {"code": "BX-SH-1", "qty": 24, "pallet_label": "1", "items": []},
                    {"code": "BX-SH-2", "qty": 24, "pallet_label": "2", "items": []},
                ],
            },
        )

        self.client.force_login(self.logistician_user)
        response = self.client.get(f"/shipping/{order.pk}/act/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "без рейса логист не может подписать акт")

    def test_shipping_draft_trip_does_not_count_as_formed_trip(self):
        order = self._create_order_ready_for_packing()
        order.vehicle_type = ShippingOrder.VEHICLE_FULFILLMENT
        order.status = ShippingOrder.STATUS_PACKED
        order.save(update_fields=["vehicle_type", "status", "updated_at"])
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="shipping",
            order_id=order.number,
            action="status",
            user=self.storekeeper_user,
            description="Паллетизация завершена",
            payload={
                "act": "shipping_packing",
                "act_state": "closed",
                "pallet_count": 1,
                "delivered_box_count": 1,
            },
        )
        trip = LogisticsTrip.objects.create(
            number="DRAFT-TEST-0001",
            status=LogisticsTrip.STATUS_DRAFT,
            created_by=self.manager_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )

        self.client.force_login(self.logistician_user)
        response = self.client.get(f"/shipping/{order.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Нужен рейс логиста")
        self.assertNotContains(response, "Логист сформировал рейс, ожидается погрузка")
        self.assertNotContains(response, "Логист подписывает акт")

    def test_dispatch_act_closes_order_only_after_logistician_and_manager_signatures(self):
        order = self._create_order_ready_for_packing()
        order.vehicle_type = ShippingOrder.VEHICLE_CLIENT
        order.status = ShippingOrder.STATUS_PACKED
        order.save(update_fields=["vehicle_type", "status", "updated_at"])
        OrderAuditEntry.objects.create(
            agency=self.agency,
            order_type="shipping",
            order_id=order.number,
            action="status",
            user=self.storekeeper_user,
            description="Паллетизация завершена",
            payload={
                "act": "shipping_packing",
                "act_state": "closed",
                "pallet_count": 2,
                "delivered_box_count": 2,
                "act_pallets": [
                    {"label": "1", "code": "1", "boxes": ["BX-SH-1"]},
                    {"label": "2", "code": "2", "boxes": ["BX-SH-2"]},
                ],
                "act_boxes": [
                    {"code": "BX-SH-1", "qty": 24, "pallet_label": "1", "items": []},
                    {"code": "BX-SH-2", "qty": 24, "pallet_label": "2", "items": []},
                ],
            },
        )
        Task.objects.create(
            title=f"Заявка на отгрузку №{order.number}",
            route=f"/shipping/{order.pk}/",
            assigned_to=self.logistician,
            status="backlog",
        )

        self.client.force_login(self.logistician_user)
        response = self.client.post(f"/shipping/{order.pk}/act/sign-logistician/")

        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, ShippingOrder.STATUS_PACKED)
        manager_sign_task = Task.objects.get(route=f"/shipping/{order.pk}/act/", assigned_to=self.manager)
        self.assertEqual(manager_sign_task.status, "backlog")
        dispatch_entry = OrderAuditEntry.objects.filter(
            order_type="shipping",
            order_id=order.number,
            payload__act="shipping_dispatch_act",
        ).latest("created_at")
        self.assertTrue(dispatch_entry.payload["act_logistician_signed"])
        self.assertFalse(dispatch_entry.payload.get("act_manager_signed"))

        self.client.force_login(self.manager_user)
        response = self.client.post(f"/shipping/{order.pk}/act/sign-manager/")

        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        item = order.items.get()
        manager_sign_task.refresh_from_db()
        logistician_task = Task.objects.get(route=f"/shipping/{order.pk}/", assigned_to=self.logistician)
        self.assertEqual(order.status, ShippingOrder.STATUS_SHIPPED)
        self.assertEqual(item.qty_shipped, 48)
        self.assertEqual(manager_sign_task.status, "done")
        self.assertEqual(logistician_task.status, "done")
        dispatch_entry = OrderAuditEntry.objects.filter(
            order_type="shipping",
            order_id=order.number,
            payload__act="shipping_dispatch_act",
        ).latest("created_at")
        self.assertTrue(dispatch_entry.payload["act_manager_signed"])
        self.assertTrue(dispatch_entry.payload["act_sent"])

    def test_storekeeper_can_reopen_saved_packing_when_otg_payload_lost(self):
        order = self._create_order_ready_for_packing()

        self.client.force_login(self.storekeeper_user)
        self.client.post(
            f"/shipping/{order.pk}/packing/",
            data={
                "boxes_json": json.dumps(
                    self._packing_rows_payload()
                ),
                "pallets_json": json.dumps(
                    [
                        {"code": "PAL-1", "label": "1"},
                        {"code": "PAL-2", "label": "2"},
                    ]
                ),
            },
        )
        move_task = MoveTask.objects.filter(
            payload__shipping_order_pk=order.pk,
            to_zone="OTG",
        ).first()
        self.assertIsNotNone(move_task)
        move_task.payload = {
            "shipping_order_id": order.number,
            "shipping_order_pk": order.pk,
            "receiving_order_id": "R-SH-1",
        }
        move_task.save(update_fields=["payload"])

        response = self.client.get(f"/shipping/{order.pk}/packing/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "BX-SH-1")
        self.assertContains(response, "BX-SH-2")

    def test_shipping_boxes_from_packing_payload_rebuilds_box_rows(self):
        rows = _shipping_boxes_from_packing_payload(
            {
                "act_boxes": [
                    {
                        "code": "BX-1",
                        "qty": 12,
                        "barcode_preview": "300000000001 - 12 шт.",
                        "items": [
                            {
                                "sku_code": "SKU-1",
                                "name": "Товар 1",
                                "size": "42",
                                "barcode": "300000000001",
                                "goods_type": "Готовый",
                                "qty": 12,
                            }
                        ],
                    }
                ]
            }
        )
        self.assertEqual(
            rows,
            [
                {
                    "row_key": _shipping_box_row_key({"code": "BX-1"}, 1),
                    "box_code": "BX-1",
                    "qty": 12,
                    "items": [
                        {
                            "sku_code": "SKU-1",
                            "name": "Товар 1",
                            "size": "42",
                            "barcode": "300000000001",
                            "goods_type": "Готовый",
                            "qty": 12,
                        }
                    ],
                    "barcode_preview": "300000000001 - 12 шт.",
                    "receiving_order_id": "",
                }
            ],
        )

    def test_shipping_boxes_from_packing_payload_keeps_rows_with_same_code_when_row_keys_differ(self):
        rows = _shipping_boxes_from_packing_payload(
            {
                "act_boxes": [
                    {
                        "row_key": "row-1",
                        "code": "BX-1",
                        "qty": 5,
                        "barcode_preview": "300000000001 - 5 шт.",
                        "items": [],
                    },
                    {
                        "row_key": "row-2",
                        "code": "BX-1",
                        "qty": 7,
                        "barcode_preview": "300000000001 - 7 шт.",
                        "items": [],
                    },
                ]
            }
        )

        self.assertEqual([row["row_key"] for row in rows], ["row-1", "row-2"])
        self.assertEqual([row["qty"] for row in rows], [5, 7])


class ShippingAttachmentFlowTests(TestCase):
    def setUp(self):
        self.temp_media = tempfile.mkdtemp(prefix="shipping-attachments-")
        self.media_override = override_settings(MEDIA_ROOT=self.temp_media)
        self.media_override.enable()

        user_model = get_user_model()
        self.client_user = user_model.objects.create_user(username="client_attach_shipping", password="pwd")
        self.manager_user = user_model.objects.create_user(username="manager_attach_shipping", password="pwd")
        self.other_client_user = user_model.objects.create_user(username="other_client_attach_shipping", password="pwd")

        Employee.objects.create(
            full_name="Менеджер вложений",
            role="manager",
            user=self.manager_user,
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Клиент с файлами", portal_user=self.client_user)
        Agency.objects.create(agn_name="Чужой клиент", portal_user=self.other_client_user)
        self.market = Market.objects.create(id=302, name="WB Attach")
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="R-ATT-1",
            sku="SKU-ATT-1",
            name="Товар с файлами",
            size="44",
            barcode="355500000001",
            goods_type="Готовый",
            qty=24,
            box_code="BX-ATT-1",
            pallet_code="PL-ATT-1",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=1,
        )

    def tearDown(self):
        self.media_override.disable()
        shutil.rmtree(self.temp_media, ignore_errors=True)
        super().tearDown()

    def _submit_payload(self):
        picker_key = _shipping_stock_picker_rows(self.agency)[0]["key"]
        eta = (timezone.localtime() + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
        return {
            "eta_at": eta.strftime("%Y-%m-%dT%H:%M"),
            "shipping_barcode": "SHIP-ATT-1",
            "marketplace": str(self.market.id),
            "wb_supply_barcode": "SUP-ATT-1",
            "destination_warehouse": "Склад с файлами",
            "supply_type": ShippingOrder.SUPPLY_BOX,
            "vehicle_type": ShippingOrder.VEHICLE_FULFILLMENT,
            "vehicle_number": "A321BC77",
            "driver_phone": "+7 900 123-45-67",
            "comment": "Файлы приложены",
            "action": "submit",
            "stock_key_all[]": [picker_key],
            "stock_boxes[]": ["1"],
        }

    def test_client_can_upload_attachment_on_create_and_manager_can_download_it(self):
        upload = SimpleUploadedFile("packing-note.txt", b"packing note", content_type="text/plain")
        payload = self._submit_payload()
        payload["documents"] = upload

        self.client.force_login(self.client_user)
        response = self.client.post("/shipping/new/?client=%s" % self.agency.id, data=payload)

        self.assertEqual(response.status_code, 302)
        order = ShippingOrder.objects.get()
        attachment = ShippingOrderAttachment.objects.get(order=order)
        self.assertEqual(attachment.filename, "packing-note.txt")

        self.client.force_login(self.manager_user)
        detail_response = self.client.get(f"/shipping/{order.pk}/")
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "Вложения")
        self.assertContains(detail_response, "packing-note.txt")
        self.assertContains(detail_response, "Срок хранения на сервере: 60 дней")

        download_response = self.client.get(
            reverse("shipping:attachment-download", args=[order.pk, attachment.pk])
        )
        self.assertEqual(download_response.status_code, 200)
        self.assertIn("attachment;", download_response["Content-Disposition"])
        self.assertIn("packing-note.txt", download_response["Content-Disposition"])
        self.assertEqual(b"".join(download_response.streaming_content), b"packing note")

    def test_attachment_download_is_forbidden_for_other_client(self):
        upload = SimpleUploadedFile("invoice.pdf", b"%PDF-test", content_type="application/pdf")
        payload = self._submit_payload()
        payload["documents"] = upload

        self.client.force_login(self.client_user)
        self.client.post("/shipping/new/?client=%s" % self.agency.id, data=payload)
        order = ShippingOrder.objects.get()
        attachment = ShippingOrderAttachment.objects.get(order=order)

        self.client.force_login(self.other_client_user)
        response = self.client.get(
            reverse("shipping:attachment-download", args=[order.pk, attachment.pk])
        )

        self.assertEqual(response.status_code, 403)

    def test_create_form_handles_attachment_save_errors_without_500(self):
        payload = self._submit_payload()
        payload["documents"] = SimpleUploadedFile("broken.txt", b"oops", content_type="text/plain")

        self.client.force_login(self.client_user)
        with patch("shipping.views._save_shipping_attachments", side_effect=OSError("disk error")):
            response = self.client.post(f"/shipping/new/?client={self.agency.id}", data=payload)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Не удалось сохранить заявку: disk error")


class ShippingClientPermissionsTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Клиент доступа")
        self.order = ShippingOrder.objects.create(
            number="SO-009999",
            agency=self.agency,
            status=ShippingOrder.STATUS_SUBMITTED,
        )

    def test_client_cannot_edit_submitted_order(self):
        self.assertFalse(_can_edit_items("client", "client", self.order))

    def test_client_can_cancel_submitted_order_before_manager_approval(self):
        self.assertTrue(_can_cancel("client", "client", self.order))


class ShippingDisplayNumberTests(TestCase):
    def test_display_shipping_number_strips_so_prefix_and_zeroes(self):
        self.assertEqual(_display_shipping_number("SO-000001"), "1_OTG")
        self.assertEqual(_display_shipping_number("SO-000120"), "120_OTG")


class ShippingBoxCountTests(TestCase):
    def test_order_box_count_prefers_expected_boxes(self):
        agency = Agency.objects.create(agn_name="Клиент коробов")
        order = ShippingOrder.objects.create(
            number="SO-123456",
            agency=agency,
            expected_boxes=7,
        )
        ShippingOrderItem.objects.create(
            order=order,
            sku_code="SKU-1",
            name="Товар",
            qty_requested=10,
            comment="Коробов: 2; кратность: 5",
        )

        self.assertEqual(_order_box_count(order), 7)

    def test_order_box_count_falls_back_to_item_comments(self):
        agency = Agency.objects.create(agn_name="Клиент коробов 2")
        order = ShippingOrder.objects.create(
            number="SO-123457",
            agency=agency,
            expected_boxes=0,
        )
        ShippingOrderItem.objects.create(
            order=order,
            sku_code="SKU-1",
            name="Товар 1",
            qty_requested=10,
            comment="Коробов: 2; кратность: 5",
        )
        ShippingOrderItem.objects.create(
            order=order,
            sku_code="SKU-2",
            name="Товар 2",
            qty_requested=12,
            comment="Коробов: 3; кратность: 4",
        )

        self.assertEqual(_order_box_count(order), 5)


class ShippingPickerParseTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.stock_rows = [
            {
                "key": "A",
                "sku_id": 1,
                "sku_code": "SKU-A",
                "name": "Товар A",
                "size": "42",
                "barcode": "111",
                "goods_type": "Готовый",
                "box_qty": 5,
                "available_boxes": 10,
                "available_qty": 50,
                "is_mixed_box": False,
                "mixed_group": "",
            },
            {
                "key": "B",
                "sku_id": 2,
                "sku_code": "SKU-B",
                "name": "Товар B",
                "size": "43",
                "barcode": "222",
                "goods_type": "Готовый",
                "box_qty": 10,
                "available_boxes": 4,
                "available_qty": 40,
                "is_mixed_box": False,
                "mixed_group": "",
            },
        ]

    def test_parse_selected_stock_items_multiple_uses_boxes_for_selected_key(self):
        request = self.factory.post(
            "/shipping/new/",
            data={
                "stock_key_all[]": ["A", "B"],
                "stock_boxes[]": ["1", "3"],
            },
        )
        selected, errors = _parse_selected_stock_items(request, self.stock_rows, multiple=True)
        self.assertEqual(errors, [])
        self.assertEqual(len(selected), 2)
        selected_by_sku = {row["sku_code"]: row for row in selected}
        self.assertEqual(selected_by_sku["SKU-A"]["qty_requested"], 5)
        self.assertEqual(selected_by_sku["SKU-B"]["qty_requested"], 30)

    def test_parse_selected_stock_items_single_uses_boxes_for_selected_key(self):
        request = self.factory.post(
            "/shipping/1/",
            data={
                "stock_key": "B",
                "stock_key_all[]": ["A", "B"],
                "stock_boxes": ["1", "4"],
            },
        )
        selected, errors = _parse_selected_stock_items(request, self.stock_rows, multiple=False)
        self.assertEqual(errors, [])
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["sku_code"], "SKU-B")
        self.assertEqual(selected[0]["qty_requested"], 40)

    def test_parse_selected_stock_items_auto_adds_siblings_for_mixed_box(self):
        stock_rows = [
            {
                "key": "M1",
                "sku_id": 11,
                "sku_code": "SKU-MIX-1",
                "name": "Микс 1",
                "size": "41",
                "barcode": "9101",
                "goods_type": "Готовый",
                "box_qty": 6,
                "available_boxes": 3,
                "available_qty": 18,
                "is_mixed_box": True,
                "mixed_group": "mix:g1",
            },
            {
                "key": "M2",
                "sku_id": 12,
                "sku_code": "SKU-MIX-2",
                "name": "Микс 2",
                "size": "42",
                "barcode": "9102",
                "goods_type": "Готовый",
                "box_qty": 4,
                "available_boxes": 3,
                "available_qty": 12,
                "is_mixed_box": True,
                "mixed_group": "mix:g1",
            },
        ]
        request = self.factory.post(
            "/shipping/new/",
            data={
                "stock_key_all[]": ["M1", "M2"],
                "stock_boxes[]": ["2", "0"],
            },
        )
        selected, errors = _parse_selected_stock_items(request, stock_rows, multiple=True)
        self.assertEqual(errors, [])
        self.assertEqual(len(selected), 2)
        selected_by_sku = {row["sku_code"]: row for row in selected}
        self.assertEqual(selected_by_sku["SKU-MIX-1"]["qty_requested"], 12)
        self.assertEqual(selected_by_sku["SKU-MIX-2"]["qty_requested"], 8)

    def test_parse_selected_stock_items_uses_positive_boxes_without_checkbox(self):
        stock_rows = [
            {
                "key": "M1",
                "sku_id": 11,
                "sku_code": "SKU-MIX-1",
                "name": "Микс 1",
                "size": "41",
                "barcode": "9101",
                "goods_type": "Готовый",
                "box_qty": 6,
                "available_boxes": 3,
                "available_qty": 18,
                "is_mixed_box": True,
                "mixed_group": "mix:g1",
            },
            {
                "key": "M2",
                "sku_id": 12,
                "sku_code": "SKU-MIX-2",
                "name": "Микс 2",
                "size": "42",
                "barcode": "9102",
                "goods_type": "Готовый",
                "box_qty": 4,
                "available_boxes": 3,
                "available_qty": 12,
                "is_mixed_box": True,
                "mixed_group": "mix:g1",
            },
        ]
        request = self.factory.post(
            "/shipping/new/",
            data={
                "stock_key_all[]": ["M1", "M2"],
                "stock_boxes[]": ["1", "0"],
            },
        )
        selected, errors = _parse_selected_stock_items(request, stock_rows, multiple=True)
        self.assertEqual(errors, [])
        self.assertEqual(len(selected), 2)
        selected_by_sku = {row["sku_code"]: row for row in selected}
        self.assertEqual(selected_by_sku["SKU-MIX-1"]["qty_requested"], 6)
        self.assertEqual(selected_by_sku["SKU-MIX-2"]["qty_requested"], 4)

    def test_parse_selected_stock_items_multiple_ignores_zero_boxes(self):
        request = self.factory.post(
            "/shipping/new/",
            data={
                "stock_key_all[]": ["A", "B"],
                "stock_boxes[]": ["0", "2"],
            },
        )
        selected, errors = _parse_selected_stock_items(request, self.stock_rows, multiple=True)
        self.assertEqual(errors, [])
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["sku_code"], "SKU-B")
        self.assertEqual(selected[0]["qty_requested"], 20)

    def test_parse_selected_stock_items_requires_at_least_one_selected_item(self):
        request = self.factory.post(
            "/shipping/new/",
            data={
                "stock_key_all[]": ["A", "B"],
                "stock_boxes[]": ["0", "0"],
            },
        )
        selected, errors = _parse_selected_stock_items(request, self.stock_rows, multiple=True)
        self.assertEqual(selected, [])
        self.assertIn("Выберите минимум одну позицию", errors[0])


class ShippingOrderFormValidationTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Client A")
        self.market = Market.objects.create(id=201, name="WB")
        self.ozon_market = Market.objects.create(id=202, name="Ozon")

    def _base_data(self, *, marketplace_id: str | None = None):
        eta = (timezone.localtime() + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
        return {
            "agency": str(self.agency.id),
            "slot_date": eta.strftime("%Y-%m-%d"),
            "slot_time": "",
            "eta_at": eta.strftime("%Y-%m-%dT%H:%M"),
            "shipping_barcode": "SHIP-123",
            "marketplace": marketplace_id or str(self.market.id),
            "wb_supply_barcode": "SUP-123",
            "wb_transit_warehouse": "",
            "transit_address": "",
            "destination_warehouse": "Склад WB Коледино",
            "supply_type": ShippingOrder.SUPPLY_BOX,
            "vehicle_type": ShippingOrder.VEHICLE_FULFILLMENT,
            "vehicle_number": "A123BC77",
            "driver_phone": "+7 900 000-00-00",
            "comment": "",
        }

    def test_form_is_valid_with_receiving_like_fields(self):
        data = self._base_data()
        form = ShippingOrderForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)

    def test_form_sets_marketplace_datalists_for_destination_fields(self):
        form = ShippingOrderForm()
        self.assertEqual(
            form.fields["destination_warehouse"].widget.attrs.get("list"),
            "destination-warehouse-options",
        )
        self.assertEqual(
            form.fields["transit_address"].widget.attrs.get("list"),
            "transit-address-options",
        )

    def test_invalid_driver_phone_is_rejected(self):
        data = self._base_data()
        data["driver_phone"] = "123"
        form = ShippingOrderForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("driver_phone", form.errors)

    def test_driver_phone_and_vehicle_number_are_optional(self):
        data = self._base_data()
        data["driver_phone"] = ""
        data["vehicle_number"] = ""
        form = ShippingOrderForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)

    def test_transport_is_required(self):
        data = self._base_data()
        data["vehicle_type"] = ""
        form = ShippingOrderForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("vehicle_type", form.errors)

    def test_transit_address_is_required_when_transit_enabled(self):
        data = self._base_data()
        data["wb_transit_warehouse"] = "on"
        data["transit_address"] = ""
        form = ShippingOrderForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("transit_address", form.errors)

    def test_transit_address_is_valid_when_transit_enabled(self):
        data = self._base_data()
        data["wb_transit_warehouse"] = "on"
        data["transit_address"] = "Москва, Транзитный терминал 1"
        form = ShippingOrderForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)

    def test_core_fields_are_required(self):
        required_fields = [
            "shipping_barcode",
            "marketplace",
            "wb_supply_barcode",
            "destination_warehouse",
            "supply_type",
        ]
        for field_name in required_fields:
            data = self._base_data()
            data[field_name] = ""
            form = ShippingOrderForm(data=data)
            self.assertFalse(form.is_valid())
            self.assertIn(field_name, form.errors)

    def test_next_day_cutoff_after_11_is_rejected(self):
        fixed_now = datetime(2026, 3, 19, 11, 1, 0)
        data = self._base_data()
        data["eta_at"] = "2026-03-20T12:00"
        with patch("shipping.forms.timezone.localtime", return_value=fixed_now):
            form = ShippingOrderForm(data=data)
            self.assertFalse(form.is_valid())
            self.assertIn("eta_at", form.errors)

    def test_shipping_time_before_workday_is_rejected(self):
        data = self._base_data()
        eta_date = (timezone.localtime() + timedelta(days=2)).strftime("%Y-%m-%d")
        data["eta_at"] = f"{eta_date}T07:55"
        form = ShippingOrderForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("eta_at", form.errors)

    def test_shipping_time_after_workday_is_rejected(self):
        data = self._base_data()
        eta_date = (timezone.localtime() + timedelta(days=2)).strftime("%Y-%m-%d")
        data["eta_at"] = f"{eta_date}T19:05"
        form = ShippingOrderForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("eta_at", form.errors)

    def test_shipping_time_at_end_of_workday_is_allowed(self):
        data = self._base_data()
        eta_date = (timezone.localtime() + timedelta(days=2)).strftime("%Y-%m-%d")
        data["eta_at"] = f"{eta_date}T19:00"
        form = ShippingOrderForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)

    def test_ozon_requires_slot_date_and_slot_time(self):
        data = self._base_data(marketplace_id=str(self.ozon_market.id))
        data["slot_date"] = ""
        data["slot_time"] = ""
        form = ShippingOrderForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("slot_date", form.errors)
        self.assertIn("slot_time", form.errors)

    def test_ozon_accepts_slot_time(self):
        data = self._base_data(marketplace_id=str(self.ozon_market.id))
        data["slot_time"] = "13:30"
        form = ShippingOrderForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)

    def test_slot_time_requires_slot_date(self):
        data = self._base_data()
        data["slot_date"] = ""
        data["slot_time"] = "13:30"
        form = ShippingOrderForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("slot_date", form.errors)


class ShippingMarketplaceWarehouseUiTests(TestCase):
    @patch(
        "shipping.views.load_marketplace_warehouse_catalog",
        return_value={
            "wb": [{"type": "Обычный", "name": "Коледино", "address": "Московская область"}],
            "ozon": [{"type": "Обычный", "name": "Санкт-Петербург_РФЦ", "address": "Софийская, 118"}],
            "yandex": [{"type": "Транзитный", "name": "Москва — Бутырский", "address": "Москва, ул. Руставели, 3"}],
            "sber": [],
        },
    )
    def test_create_view_exposes_marketplace_warehouse_catalog(self, _mock_loader):
        user_model = get_user_model()
        client_user = user_model.objects.create_user(username="client_ui", password="pwd")
        agency = Agency.objects.create(agn_name="Клиент UI", portal_user=client_user)

        self.client.force_login(client_user)
        response = self.client.get("/shipping/new/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["marketplace_warehouse_catalog"]["wb"],
            [{"type": "Обычный", "name": "Коледино", "address": "Московская область"}],
        )
        self.assertContains(response, "destination-warehouse-options")
        self.assertContains(response, "transit-address-options")
        self.assertContains(response, "marketplace-warehouse-catalog")
        self.assertContains(response, 'data-picker-target="destination"', html=False)
        self.assertContains(response, 'data-picker-target="transit"', html=False)
        self.assertContains(response, "warehouse-picker-overlay")

    @patch(
        "shipping.views.load_marketplace_warehouse_catalog",
        return_value={
            "wb": [
                {"type": "Обычный", "name": "Коледино", "address": "Московская область"},
                {"type": "Транзитный / ППП", "name": "Гольёво", "address": "Московская область"},
                {"type": "Обычный + транзитный", "name": "Софьино", "address": "Московская область"},
            ],
            "ozon": [],
            "yandex": [],
            "sber": [],
        },
    )
    def test_create_view_splits_destination_and_transit_suggestions(self, _mock_loader):
        user_model = get_user_model()
        client_user = user_model.objects.create_user(username="client_ui_split", password="pwd")
        agency = Agency.objects.create(agn_name="Клиент UI split", portal_user=client_user)

        self.client.force_login(client_user)
        response = self.client.get("/shipping/new/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "normalizeWarehouseRow")
        self.assertContains(response, "openWarehousePicker")
        self.assertContains(response, "обычных складов")
        self.assertContains(response, "транзитных складов")


class ShippingPackingSlipStatusEndpointTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="storekeeper_status", password="pwd")
        Employee.objects.create(
            full_name="Кладовщик статуса",
            role="storekeeper",
            user=self.user,
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Клиент статуса")
        self.order = ShippingOrder.objects.create(
            number="SO-STATUS-1",
            agency=self.agency,
            created_by=self.user,
            status=ShippingOrder.STATUS_PACKED,
        )
        self.client.force_login(self.user)

    @patch("shipping.views._shipping_packing_summary", return_value={"pallet_count": 1, "box_count": 2})
    @patch(
        "shipping.views.build_print_status_snapshot",
        return_value={
            "available_printers": ["Zebra GK420d"],
            "available_printers_meta": {},
            "print_status_line": "Готово к печати: 1 из 1",
            "print_agent_line": "Warehouse-PC-01 · 14.04.2026 16:00:00",
            "print_agent_online": True,
            "print_paused": False,
            "print_queue_pending": 2,
            "print_queue_printing": 1,
            "print_queue_failed": 0,
            "print_queue_stuck": 0,
            "print_queue_pending_jobs": ["#10 · Zebra GK420d"],
            "print_queue_printing_jobs": ["#11 · Zebra GK420d"],
            "print_queue_failed_jobs": [],
            "print_queue_stuck_jobs": [],
            "printer_statuses": [
                {
                    "name": "Zebra GK420d",
                    "state_key": "ready",
                    "state_label": "Готов",
                    "color": "green",
                    "jobs": 0,
                    "status": "",
                    "is_default": True,
                    "is_local": True,
                    "is_network": False,
                    "is_virtual": False,
                }
            ],
            "printer_details_updated_at": "14.04.2026 16:00:01",
        },
    )
    def test_get_status_endpoint_returns_snapshot(self, _snapshot, _packing_summary):
        response = self.client.get(reverse("shipping:packing-slips-status", args=[self.order.pk]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["print_queue_pending"], 2)
        self.assertEqual(payload["printer_statuses"][0]["state_key"], "ready")

    @patch("shipping.views._shipping_packing_summary", return_value={"pallet_count": 1, "box_count": 2})
    @patch(
        "shipping.views.refresh_print_agent_printers",
        return_value=(
            {
                "available_printers": ["Zebra GK420d"],
                "available_printers_meta": {},
                "print_status_line": "Есть ошибки печати: 1",
                "print_agent_line": "Warehouse-PC-01 · 14.04.2026 16:05:00",
                "print_agent_online": True,
                "print_paused": False,
                "print_queue_pending": 0,
                "print_queue_printing": 0,
                "print_queue_failed": 1,
                "print_queue_stuck": 0,
                "print_queue_pending_jobs": [],
                "print_queue_printing_jobs": [],
                "print_queue_failed_jobs": ["#12 · Zebra GK420d"],
                "print_queue_stuck_jobs": [],
                "printer_statuses": [],
                "printer_details_updated_at": "14.04.2026 16:05:01",
            },
            "Диагностика обновлена.",
        ),
    )
    def test_post_status_endpoint_refreshes_agent_state(self, _refresh, _packing_summary):
        response = self.client.post(reverse("shipping:packing-slips-status", args=[self.order.pk]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["refresh_message"], "Диагностика обновлена.")
        self.assertEqual(payload["print_queue_failed"], 1)
