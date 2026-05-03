import json
from types import SimpleNamespace
from datetime import datetime
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.utils import timezone

from audit.models import AuditEntry, OrderAuditEntry
from employees.models import Employee
from marking.models import MarkingCode
from reachtruck.models import MoveRequest, MoveTask
from todo.models import Task
from orders.services import ReceivingWorkflowService
from sklad.models import StockPalletState, WarehouseOperation, WarehouseReserve, WarehouseStockSnapshot
from sklad.services.warehouse_state import WarehouseGoodsStateResolver
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sku.models import Agency, SKU
from .views import (
    _create_receiving_warehouse_moves,
    _current_responsible_label,
    _display_order_number,
    _flow_closed_from_entries,
    _history_actor_label,
    _item_key,
    _min_receiving_eta,
    _parse_qty_value,
    _receiving_flow_box_action_meta,
    _status_label_from_entry,
)


def _entry(payload: dict):
    return SimpleNamespace(payload=payload)


class OrdersHelpersTests(SimpleTestCase):
    def test_item_key_normalizes_values(self):
        key = _item_key("  SKU-1 ", " Джинсы ", " 42 ")
        self.assertEqual(key, "sku-1|джинсы|42")

    def test_parse_qty_value(self):
        self.assertEqual(_parse_qty_value("10"), 10)
        self.assertEqual(_parse_qty_value(" 0 "), 0)
        self.assertIsNone(_parse_qty_value(""))
        self.assertIsNone(_parse_qty_value("abc"))

    def test_flow_closed_from_entries_prefers_latest_reopen(self):
        entries = [
            _entry({"flow_closed": True}),
            _entry({"flow_reopened": True}),
        ]
        self.assertFalse(_flow_closed_from_entries(entries))

        entries = [
            _entry({"flow_reopened": True}),
            _entry({"flow_closed": True}),
        ]
        self.assertTrue(_flow_closed_from_entries(entries))

    def test_status_label_for_placement_state(self):
        self.assertEqual(
            _status_label_from_entry(_entry({"act": "placement", "act_state": "open"})),
            "Размещение на складе",
        )
        self.assertEqual(
            _status_label_from_entry(_entry({"act": "placement", "act_state": "closed"})),
            "Товар принят и размещен на складе",
        )

    def test_display_order_number_formats_known_types(self):
        self.assertEqual(_display_order_number("shipping", "SO-000001"), "1_OTG")
        self.assertEqual(_display_order_number("receiving", "12"), "12_PR")
        self.assertEqual(_display_order_number("processing", "7"), "7_OBR")

    def test_display_order_number_keeps_non_numeric_ids(self):
        self.assertEqual(_display_order_number("receiving", "rcv-abcd1234"), "rcv-abcd1234")
        self.assertEqual(_display_order_number("processing", "draft-xyz"), "draft-xyz")

    def test_receiving_flow_box_action_meta_supports_batch_actions(self):
        self.assertEqual(
            _receiving_flow_box_action_meta("delete_batch"),
            ("delete", "Удаление группы коробов"),
        )
        self.assertEqual(
            _receiving_flow_box_action_meta("move_batch"),
            ("update", "Перемещение группы коробов"),
        )
        self.assertEqual(
            _receiving_flow_box_action_meta("print_batch"),
            ("update", "Печать этикеток группы коробов"),
        )

    def test_min_receiving_eta_allows_same_day_future_time(self):
        current = timezone.make_aware(datetime(2026, 4, 2, 11, 2))
        self.assertEqual(
            _min_receiving_eta(current),
            timezone.make_aware(datetime(2026, 4, 2, 11, 5)),
        )


class OrdersReceivingReachtruckTests(TestCase):
    def test_create_receiving_warehouse_moves_skips_pr_to_pr_tasks(self):
        user_model = get_user_model()
        user = user_model.objects.create_user(username="receiving_guard", password="pwd")
        agency = Agency.objects.create(agn_name="Тест Клиент")
        placement_entry = OrderAuditEntry.objects.create(
            order_id="R-PR-1",
            order_type="receiving",
            action="status",
            agency=agency,
            user=user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_pallets": [
                    {
                        "code": "PAL-PR-1",
                        "location": {"zone": "PR", "row": "", "section": "", "tier": "", "cell": ""},
                    }
                ],
            },
        )

        request = RequestFactory().post("/orders/receiving/R-PR-1/create-warehouse-moves/")
        request.user = user

        created, skipped_existing, skipped_missing, total = _create_receiving_warehouse_moves(
            "R-PR-1",
            [placement_entry],
            request,
        )

        self.assertEqual((created, skipped_existing, skipped_missing, total), (0, 0, 1, 1))
        self.assertEqual(MoveTask.objects.count(), 0)

    def test_create_receiving_warehouse_moves_creates_only_pallets_with_confirmed_destination(self):
        user_model = get_user_model()
        user = user_model.objects.create_user(username="receiving_partial", password="pwd")
        agency = Agency.objects.create(agn_name="Тест Клиент 2")
        placement_entry = OrderAuditEntry.objects.create(
            order_id="R-PR-2",
            order_type="receiving",
            action="status",
            agency=agency,
            user=user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_pallets": [
                    {
                        "code": "PAL-READY-1",
                        "location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
                    },
                    {
                        "code": "PAL-PR-2",
                        "location": {"zone": "PR", "row": "", "section": "", "tier": "", "cell": ""},
                    },
                ],
            },
        )

        request = RequestFactory().post("/orders/receiving/R-PR-2/create-warehouse-moves/")
        request.user = user

        created, skipped_existing, skipped_missing, total = _create_receiving_warehouse_moves(
            "R-PR-2",
            [placement_entry],
            request,
        )

        self.assertEqual((created, skipped_existing, skipped_missing, total), (1, 0, 1, 2))
        self.assertEqual(MoveTask.objects.count(), 1)
        self.assertEqual(MoveTask.objects.get().pallet_code, "PAL-READY-1")

    def test_create_receiving_warehouse_moves_uses_selected_destinations(self):
        user_model = get_user_model()
        user = user_model.objects.create_user(username="receiving_modal", password="pwd")
        agency = Agency.objects.create(agn_name="Тест Клиент 3")
        placement_entry = OrderAuditEntry.objects.create(
            order_id="R-PR-3",
            order_type="receiving",
            action="status",
            agency=agency,
            user=user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_pallets": [
                    {
                        "code": "PAL-OVERRIDE-1",
                        "location": {"zone": "PR", "row": "", "section": "", "tier": "", "cell": ""},
                    }
                ],
            },
        )
        request = RequestFactory().post("/orders/receiving/R-PR-3/create-warehouse-moves/")
        request.user = user

        created, skipped_existing, skipped_missing, total = _create_receiving_warehouse_moves(
            "R-PR-3",
            [placement_entry],
            request,
            destinations_by_pallet={
                "PAL-OVERRIDE-1": {"zone": "OS", "row": 2, "section": 1, "tier": 1, "cell": 3},
            },
        )

        self.assertEqual((created, skipped_existing, skipped_missing, total), (1, 0, 0, 1))
        task = MoveTask.objects.get()
        self.assertEqual(task.pallet_code, "PAL-OVERRIDE-1")
        self.assertEqual(task.to_zone, "OS")
        self.assertEqual(task.to_row, 2)
        self.assertEqual(task.to_section, 1)
        self.assertEqual(task.to_tier, 1)
        self.assertEqual(task.to_cell, 3)

    def test_create_receiving_warehouse_moves_links_legacy_task_with_warehouse_putaway(self):
        user_model = get_user_model()
        user = user_model.objects.create_user(username="receiving_putaway_bridge", password="pwd")
        agency = Agency.objects.create(agn_name="Тест Клиент 4")
        SKU.objects.create(agency=agency, sku_code="SKU-BRIDGE-1", name="Товар моста", size="42")
        placement_entry = OrderAuditEntry.objects.create(
            order_id="R-PR-4",
            order_type="receiving",
            action="status",
            agency=agency,
            user=user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {
                        "code": "BOX-BRIDGE-1",
                        "sealed": True,
                        "items": [
                            {
                                "sku": "SKU-BRIDGE-1",
                                "name": "Товар моста",
                                "size": "42",
                                "barcode": "BR-1",
                                "qty": 5,
                            }
                        ],
                    }
                ],
                "act_pallets": [
                    {
                        "code": "PAL-BRIDGE-1",
                        "sealed": True,
                        "boxes": ["BOX-BRIDGE-1"],
                        "items": [],
                        "location": {"zone": "OS", "row": 2, "section": 1, "tier": 1, "cell": 3},
                    }
                ],
            },
        )

        request = RequestFactory().post("/orders/receiving/R-PR-4/create-warehouse-moves/")
        request.user = user

        created, skipped_existing, skipped_missing, total = _create_receiving_warehouse_moves(
            "R-PR-4",
            [placement_entry],
            request,
        )

        self.assertEqual((created, skipped_existing, skipped_missing, total), (1, 0, 0, 1))
        task = MoveTask.objects.get()
        operation = WarehouseOperation.objects.get(context_type="receiving", context_id="R-PR-4")
        warehouse_task = operation.tasks.get()
        self.assertEqual(operation.operation_type, WarehouseOperation.TYPE_PUTAWAY)
        self.assertEqual(operation.destination_zone_code, "OS")
        self.assertEqual(warehouse_task.payload.get("legacy_move_id"), task.legacy_order_id)
        self.assertEqual((task.payload or {}).get("warehouse_operation_id"), operation.id)
        self.assertEqual((task.payload or {}).get("warehouse_operation_task_id"), warehouse_task.id)


class OrdersReceivingMarkingFlowTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="receiving_cz", password="pwd")
        self.agency = Agency.objects.create(agn_name="Клиент ЧЗ")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-CZ-1",
            name="Маркируемый товар",
            size="42",
            honest_sign=True,
        )
        Employee.objects.create(user=self.user, role="storekeeper", full_name="Кладовщик Тест", is_active=True)
        self.client.force_login(self.user)

    def _create_receiving_status(self, order_id: str, extra_payload: dict | None = None):
        payload = {
            "status": "warehouse",
            "status_label": "В ожидании поставки товара",
            "goods_type": "op",
            "goods_type_label": "Оптовый",
            "items": [
                {
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "size": self.sku.size,
                    "qty": 2,
                }
            ],
        }
        if extra_payload:
            payload.update(extra_payload)
        return OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус",
            payload=payload,
        )

    def test_receiving_flow_persists_cz_mode_in_status_payload(self):
        order_id = "R-CZ-MODE"
        self._create_receiving_status(order_id)

        response = self.client.post(
            f"/orders/receiving/{order_id}/",
            {
                "action": "create_receiving_act",
                "goods_type": "op",
                "receiving_mode": "cz",
            },
        )

        self.assertEqual(response.status_code, 302)
        latest = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving")
            .order_by("-id")
            .first()
        )
        self.assertEqual((latest.payload or {}).get("receiving_mode"), "cz")

    def test_receiving_detail_page_renders_with_marked_items(self):
        order_id = "R-CZ-DETAIL"
        self._create_receiving_status(order_id, {"receiving_mode": "cz"})

        response = self.client.get(f"/orders/receiving/{order_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Потоковая приемка с ЧЗ")

    def test_receiving_marked_items_fallbacks_to_order_items_without_honest_sign_flag(self):
        self.sku.honest_sign = False
        self.sku.save(update_fields=["honest_sign"])
        order_id = "R-CZ-FALLBACK"
        self._create_receiving_status(order_id, {"receiving_mode": "cz"})

        response = self.client.get(f"/orders/receiving/{order_id}/flow/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Выберите SKU и размер")
        self.assertContains(response, "SKU-CZ-1")

    def test_receiving_flow_closes_with_act_units_for_marked_goods(self):
        order_id = "R-CZ-FLOW"
        self._create_receiving_status(order_id, {"receiving_mode": "cz"})
        now = timezone.localtime()
        MarkingCode.objects.create(
            order_type="receiving",
            order_id=order_id,
            agency=self.agency,
            sku=self.sku,
            sku_code=self.sku.sku_code,
            size=self.sku.size,
            barcode="20001",
            box_barcode="BOX-CZ-1",
            code="CZ-0001",
            source="scan",
            created_by=self.user,
            used_at=now,
            used_by=self.user,
        )
        MarkingCode.objects.create(
            order_type="receiving",
            order_id=order_id,
            agency=self.agency,
            sku=self.sku,
            sku_code=self.sku.sku_code,
            size=self.sku.size,
            barcode="20001",
            box_barcode="BOX-CZ-1",
            code="CZ-0002",
            source="scan",
            created_by=self.user,
            used_at=now,
            used_by=self.user,
        )

        response = self.client.post(
            f"/orders/receiving/{order_id}/flow/",
            {
                "boxes_json": '[{"code":"BOX-CZ-1","items":[{"sku_code":"SKU-CZ-1","name":"Маркируемый товар","size":"42","qty":2}],"sealed":true}]',
                "pallets_json": '[{"code":"PAL-CZ-1","boxes":["BOX-CZ-1"],"items":[],"sealed":true,"location":{"zone":"PR"}}]',
            },
        )

        self.assertEqual(response.status_code, 302)
        act_entry = OrderAuditEntry.objects.filter(
            order_id=order_id,
            order_type="receiving",
            payload__act="receiving",
        ).latest("created_at")
        act_units = (act_entry.payload or {}).get("act_units") or []
        self.assertEqual(len(act_units), 2)
        self.assertEqual({unit["marking_code"] for unit in act_units}, {"CZ-0001", "CZ-0002"})
        self.assertTrue(all(unit["pallet_code"] == "PAL-CZ-1" for unit in act_units))

        placement_entry = OrderAuditEntry.objects.filter(
            order_id=order_id,
            order_type="receiving",
            payload__act="placement",
        ).latest("created_at")
        self.assertEqual(len((placement_entry.payload or {}).get("act_units") or []), 2)

    def test_print_receiving_act_shows_marking_column_for_act_units(self):
        order_id = "R-CZ-PRINT"
        self._create_receiving_status(order_id)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки",
            payload={
                "act": "receiving",
                "eta_at": timezone.localtime().isoformat(),
                "vehicle_number": "A123BC",
                "act_units": [
                    {
                        "sku_code": "SKU-CZ-1",
                        "name": "Маркируемый товар",
                        "size": "42",
                        "barcode": "20001",
                        "marking_code": "CZ-PRINT-1",
                        "box_code": "BOX-CZ-1",
                    }
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [{"code": "BOX-CZ-1", "items": []}],
                "act_pallets": [{"code": "PAL-CZ-1", "boxes": ["BOX-CZ-1"], "items": []}],
            },
        )

        response = self.client.get(f"/orders/receiving/{order_id}/act/print/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ЧЗ")
        self.assertContains(response, "CZ-PRINT-1")


class OrdersPlacementOperationalStockTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="receiving_ops_stock", password="pwd")
        Employee.objects.create(user=self.user, role="storekeeper", full_name="Кладовщик OPS", is_active=True)
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Клиент OPS")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-OPS-1",
            name="Оперативный товар",
            size="42",
        )

    def _seed_receiving_order(self, order_id: str):
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус",
            payload={
                "status": "warehouse",
                "status_label": "Размещение на складе",
                "goods_type": "op",
                "goods_type_label": "Оптовый",
                "items": [
                    {
                        "sku_code": self.sku.sku_code,
                        "name": self.sku.name,
                        "size": self.sku.size,
                        "qty": 5,
                    }
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки",
            payload={
                "act": "receiving",
                "act_label": "Акт приемки",
                "act_state": "closed",
                "goods_type": "op",
                "goods_type_label": "Оптовый",
                "act_items": [
                    {
                        "sku_code": self.sku.sku_code,
                        "name": self.sku.name,
                        "size": self.sku.size,
                        "actual_qty": 5,
                        "barcode": "OPS-BAR-1",
                    }
                ],
            },
        )

    def test_receiving_placement_close_and_open_update_operational_stock(self):
        order_id = "R-OPS-1"
        self._seed_receiving_order(order_id)

        close_response = self.client.post(
            f"/orders/receiving/{order_id}/placement/",
            {
                "boxes_json": (
                    '[{"code":"BOX-OPS-1","items":[{"sku":"SKU-OPS-1","name":"Оперативный товар",'
                    '"size":"42","barcode":"OPS-BAR-1","qty":5}],"sealed":true}]'
                ),
                "pallets_json": (
                    '[{"code":"PAL-OPS-1","boxes":["BOX-OPS-1"],"items":[],"sealed":true,'
                    '"location":{"zone":"OS","row":2,"section":1,"tier":1,"cell":3}}]'
                ),
            },
        )

        self.assertEqual(close_response.status_code, 302)
        self.assertIn("?ok=1", close_response.url)
        stock_row = StockPalletState.objects.get(
            agency=self.agency,
            order_type="receiving",
            order_id=order_id,
        )
        self.assertEqual(stock_row.sku, "SKU-OPS-1")
        self.assertEqual(stock_row.box_code, "BOX-OPS-1")
        self.assertEqual(stock_row.pallet_code, "PAL-OPS-1")
        self.assertEqual(stock_row.zone, "OS")
        self.assertEqual(stock_row.location, "OS · Ряд 2 · Секция 1 · Ярус 1 · Ячейка 3")
        self.assertEqual(stock_row.available_qty, 5)
        snapshot = WarehouseStockSnapshot.objects.get(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
        )
        self.assertEqual(snapshot.container_code, "PAL-OPS-1")
        self.assertEqual(snapshot.zone_code, "PR")
        self.assertEqual(snapshot.warehouse_state_code, "placed_in_receiving")
        self.assertEqual(snapshot.available_qty, 5)

        open_response = self.client.post(
            f"/orders/receiving/{order_id}/placement/",
            {"action": "open"},
        )

        self.assertEqual(open_response.status_code, 302)
        self.assertFalse(
            StockPalletState.objects.filter(
                agency=self.agency,
                order_type="receiving",
                order_id=order_id,
            ).exists()
        )
        self.assertFalse(
            WarehouseStockSnapshot.objects.filter(
                agency=self.agency,
                source_context_type="receiving",
                source_context_id=order_id,
            ).exists()
        )


class OrdersProcessingWarehouseDetailTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="orders_processing_detail", password="pwd")
        Employee.objects.create(
            full_name="Руководитель обработки деталки",
            role="processing_head",
            user=self.user,
            is_active=True,
        )
        self.client.login(username="orders_processing_detail", password="pwd")
        self.agency = Agency.objects.create(agn_name="Клиент обработки деталки")

    def test_processing_detail_prefers_warehouse_status_and_responsible(self):
        order_id = "P-ORD-1"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_head",
                "status_label": "Передано в обработку",
                "article": "SKU-DETAIL",
                "product_name": "Товар деталки",
            },
        )
        location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OBR",
        )
        snapshot = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="legacy_stock",
            source_context_id=f"legacy-{order_id}",
            sku_code="SKU-DETAIL",
            name="Товар деталки",
            size="42",
            barcode="200000009001",
            goods_type="gv",
            qty=8,
            available_qty=0,
            processing_reserved_qty=8,
            container_code="PAL-DETAIL-1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="processing_in_progress",
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
            sku_code=snapshot.sku_code,
            size=snapshot.size,
            barcode=snapshot.barcode,
            goods_type=snapshot.goods_type,
            qty_reserved=8,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        response = self.client.get(f"/orders/processing/{order_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Товар в обработке")
        self.assertIn("Руководитель обработки", response.context["responsible"])
        self.assertFalse(response.context["can_take_processing"])
        self.assertEqual(
            response.context["next_step_label"],
            "Завершить обработку и подготовить размещение",
        )

    def test_processing_status_helper_prefers_warehouse_state(self):
        agency = Agency.objects.create(agn_name="Helper Processing Agency")
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OBR")
        snapshot = WarehouseStockSnapshot.objects.create(
            agency=agency,
            source_context_type="legacy_stock",
            source_context_id="legacy-P-H-1",
            sku_code="SKU-H-1",
            name="Helper Processing",
            size="42",
            barcode="200000009101",
            goods_type="gv",
            qty=3,
            available_qty=0,
            processing_reserved_qty=3,
            container_code="PAL-H-1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="processing_in_progress",
        )
        WarehouseReserve.objects.create(
            agency=agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="P-H-1",
            sku_code=snapshot.sku_code,
            size=snapshot.size,
            barcode=snapshot.barcode,
            goods_type=snapshot.goods_type,
            qty_reserved=3,
            status=WarehouseReserve.STATUS_ACTIVE,
        )
        entry = SimpleNamespace(
            payload={"status": "processing_head", "status_label": "Передано в обработку"},
            order_type="processing",
            order_id="P-H-1",
            agency=agency,
        )

        self.assertEqual(_status_label_from_entry(entry), "Товар в обработке")

    def test_processing_history_actor_helper_prefers_warehouse_state(self):
        agency = Agency.objects.create(agn_name="History Processing Agency")
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OBR")
        snapshot = WarehouseStockSnapshot.objects.create(
            agency=agency,
            source_context_type="legacy_stock",
            source_context_id="legacy-P-H-2",
            sku_code="SKU-H-2",
            name="History Processing",
            size="42",
            barcode="200000009102",
            goods_type="gv",
            qty=4,
            available_qty=0,
            processing_reserved_qty=4,
            container_code="PAL-H-2",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="processing_in_progress",
        )
        WarehouseReserve.objects.create(
            agency=agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="P-H-2",
            sku_code=snapshot.sku_code,
            size=snapshot.size,
            barcode=snapshot.barcode,
            goods_type=snapshot.goods_type,
            qty_reserved=4,
            status=WarehouseReserve.STATUS_ACTIVE,
        )
        Employee.objects.create(full_name="Главный обработчик", role="processing_head", is_active=True)
        entry = SimpleNamespace(
            payload={"status": "processing_head", "status_label": "Передано в обработку"},
            order_type="processing",
            order_id="P-H-2",
            agency=agency,
            action="status",
            user=None,
        )

        self.assertIn("Руководитель обработки", _history_actor_label(entry))

    def test_processing_placement_page_prefers_warehouse_status(self):
        order_id = "P-PLACEMENT-WH-1"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_head",
                "status_label": "Передано в обработку",
                "article": "SKU-PLACEMENT",
                "product_name": "Товар размещения",
            },
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OBR")
        snapshot = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="legacy_stock",
            source_context_id=f"legacy-{order_id}",
            sku_code="SKU-PLACEMENT",
            name="Товар размещения",
            size="42",
            barcode="200000009103",
            goods_type="gv",
            qty=4,
            available_qty=0,
            processing_reserved_qty=4,
            container_code="PAL-P-PL1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="processing_in_progress",
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
            sku_code=snapshot.sku_code,
            size=snapshot.size,
            barcode=snapshot.barcode,
            goods_type=snapshot.goods_type,
            qty_reserved=4,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        response = self.client.get(f"/orders/processing/{order_id}/placement/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Товар в обработке")

    def test_processing_placement_post_is_rejected_after_storage(self):
        order_id = "P-PLACEMENT-POST-STORED-1"
        storekeeper_user = get_user_model().objects.create_user(
            username="processing_placement_storekeeper",
            password="pwd",
        )
        Employee.objects.create(
            full_name="Кладовщик обработки",
            role="storekeeper",
            user=storekeeper_user,
            is_active=True,
        )
        self.client.login(username="processing_placement_storekeeper", password="pwd")
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            user=self.user,
            payload={
                "status": "done",
                "status_label": "Выполнена",
                "cards": [{"id": "card-a", "article": "SKU-PLACEMENT-POST", "rows": [{"size": "42", "qty": "2"}]}],
                "processed_cards": ["card-a"],
                "placed_cards": ["card-a"],
                "processing_results": [
                    {
                        "card_id": "card-a",
                        "article": "SKU-PLACEMENT-POST",
                        "size": "42",
                        "destination": "-",
                        "processed": "2",
                    }
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="update",
            agency=self.agency,
            user=self.user,
            payload={
                "act": "receiving",
                "act_label": "Акт приемки (обработка)",
                "act_state": "closed",
                "act_items": [
                    {
                        "sku_code": "SKU-PLACEMENT-POST",
                        "name": "Товар размещения post",
                        "size": "42",
                        "actual_qty": 2,
                    }
                ],
            },
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OS")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="legacy_stock",
            source_context_id=f"legacy-{order_id}",
            sku_code="SKU-PLACEMENT-POST",
            name="Товар размещения post",
            size="42",
            barcode="200000009215",
            goods_type="gv",
            qty=2,
            available_qty=2,
            processing_reserved_qty=0,
            container_code="PAL-P-PST1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="stored",
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
            sku_code="SKU-PLACEMENT-POST",
            size="42",
            barcode="200000009215",
            goods_type="gv",
            qty_reserved=2,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        response = self.client.post(
            f"/orders/processing/{order_id}/placement/",
            {
                "boxes_json": (
                    '[{"code":"BOX-P-PST-1","items":[{"sku":"SKU-PLACEMENT-POST","name":"Товар размещения post",'
                    '"size":"42","qty":2}],"sealed":true}]'
                ),
                "pallets_json": (
                    '[{"code":"PAL-P-PST1","boxes":["BOX-P-PST-1"],"items":[],"sealed":true,'
                    '"location":{"zone":"OS","row":1,"section":1,"tier":1,"cell":1}}]'
                ),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"/orders/processing/{order_id}/placement/?error=1")


class OrdersReceivingWorkflowServiceTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="receiving_workflow_actor", password="pwd")
        self.manager_user = user_model.objects.create_user(username="receiving_workflow_manager", password="pwd")
        self.storekeeper_user = user_model.objects.create_user(
            username="receiving_workflow_storekeeper",
            password="pwd",
        )
        Employee.objects.create(
            full_name="Исполнитель приемки",
            role="storekeeper",
            user=self.user,
            is_active=True,
        )
        self.manager = Employee.objects.create(
            full_name="Менеджер приемки",
            role="manager",
            user=self.manager_user,
            is_active=True,
        )
        self.storekeeper = Employee.objects.create(
            full_name="Кладовщик приемки",
            role="storekeeper",
            user=self.storekeeper_user,
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Клиент workflow")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-WF-1",
            name="Товар workflow",
            size="42",
        )

    def _create_receiving_snapshot(self, order_id: str, state_code: str = "placed_in_receiving"):
        location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="PR" if state_code != "stored" else "OS",
        )
        return WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            size=self.sku.size,
            barcode=f"WF-{order_id}",
            goods_type="op",
            qty=2,
            available_qty=2,
            container_code=f"PAL-{order_id}",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code=state_code,
        )

    def test_submit_receiving_for_review_creates_manager_task(self):
        order_id = "R-WF-SUBMIT"

        result = ReceivingWorkflowService.submit_receiving_for_review(
            order_id=order_id,
            agency=self.agency,
            user=self.user,
            submitted_at=timezone.make_aware(datetime(2026, 4, 22, 11, 0)),
        )

        self.assertTrue(result.manager_task_created)
        task = Task.objects.get(
            route=f"/orders/receiving/{order_id}/",
            assigned_to=self.manager,
        )
        self.assertIn("Подтвердите заявку на приемку товара", task.title)
        self.assertEqual(task.created_by_id, self.user.id)

    def test_submit_receiving_order_creates_entry_and_dispatches_review(self):
        order_id = "R-WF-CREATE"

        result = ReceivingWorkflowService.submit_receiving_order(
            order_id=order_id,
            agency=self.agency,
            payload={
                "status": "sent_unconfirmed",
                "status_label": "Ждет подтверждения",
                "submit_action": "send",
                "eta_at": "2026-04-22T10:00:00+03:00",
                "items": [{"sku_code": self.sku.sku_code, "qty": 2}],
            },
            submit_action="send",
            user=self.user,
            submitted_at=timezone.make_aware(datetime(2026, 4, 22, 11, 0)),
            dispatch_review=True,
        )

        self.assertEqual(result.order_id, order_id)
        self.assertFalse(result.was_update)
        self.assertTrue(result.review_dispatched)
        entry = OrderAuditEntry.objects.get(order_id=order_id, order_type="receiving")
        self.assertEqual(entry.description, f"Заявка на приемку №{order_id} (заявка)")
        self.assertEqual(entry.payload.get("status"), "sent_unconfirmed")
        manager_task = Task.objects.get(
            route=f"/orders/receiving/{order_id}/",
            assigned_to=self.manager,
        )
        self.assertIn("Подтвердите заявку", manager_task.title)

    def test_submit_receiving_order_updates_entry_and_dispatches_review(self):
        order_id = "R-WF-EDIT"
        old_payload = {
            "status": "draft",
            "status_label": "Черновик",
            "eta_at": "2026-04-21T10:00:00+03:00",
            "items": [{"sku_code": self.sku.sku_code, "qty": 1}],
        }

        result = ReceivingWorkflowService.submit_receiving_order(
            order_id=order_id,
            agency=self.agency,
            payload={
                "status": "sent_unconfirmed",
                "status_label": "Ждет подтверждения",
                "submit_action": "send",
                "eta_at": "2026-04-22T12:00:00+03:00",
                "items": [{"sku_code": self.sku.sku_code, "qty": 2}],
            },
            submit_action="send",
            user=self.user,
            submitted_at=timezone.make_aware(datetime(2026, 4, 22, 12, 0)),
            existing_order_id=order_id,
            old_payload=old_payload,
            dispatch_review=True,
        )

        self.assertTrue(result.was_update)
        self.assertTrue(result.review_dispatched)
        entry = OrderAuditEntry.objects.get(order_id=order_id, order_type="receiving")
        self.assertIn("Отправлено менеджеру.", entry.description)
        self.assertIn("Состав поставки: 1 → 1 поз.", entry.description)
        manager_task = Task.objects.get(
            route=f"/orders/receiving/{order_id}/",
            assigned_to=self.manager,
        )
        self.assertIn("Подтвердите заявку", manager_task.title)

    def test_configure_receiving_act_updates_goods_type_and_mode(self):
        order_id = "R-WF-ACTCFG"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "items": [{"sku_code": self.sku.sku_code, "qty": 1}],
            },
        )

        result = ReceivingWorkflowService.configure_receiving_act(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            role="storekeeper",
            goods_type="op",
            receiving_mode="cz",
            user=self.user,
        )

        self.assertTrue(result.applied)
        self.assertEqual(result.payload.get("goods_type"), "op")
        self.assertEqual(result.payload.get("goods_type_label"), "Оптовый")
        self.assertEqual(result.payload.get("receiving_mode"), "cz")
        entry = OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").latest("id")
        self.assertEqual(entry.description, "Тип товара: Оптовый")
        self.assertEqual(entry.payload.get("receiving_mode"), "cz")

    def test_start_receiving_work_logs_status_with_storekeeper_data(self):
        order_id = "R-WF-START"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "items": [{"sku_code": self.sku.sku_code, "qty": 1}],
            },
        )

        result = ReceivingWorkflowService.start_receiving_work(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            role="storekeeper",
            user=self.user,
        )

        self.assertEqual(result.status, "started")
        self.assertEqual(result.payload.get("status_label"), "Взята в работу")
        self.assertTrue(result.payload.get("storekeeper_employee_id"))
        entry = OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").latest("id")
        self.assertEqual(entry.description, "Заявка взята в работу кладовщиком")
        self.assertEqual(entry.payload.get("status_label"), "Взята в работу")

    def test_reopen_receiving_flow_logs_overaction_and_status(self):
        order_id = "R-WF-REOPEN"
        self._create_receiving_snapshot(order_id, "placed_in_receiving")
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "Размещение на складе",
                "flow_closed": True,
                "flow_closed_at": "2026-04-22T10:00:00+03:00",
            },
        )

        result = ReceivingWorkflowService.reopen_receiving_flow(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            role="storekeeper",
            user=self.user,
        )

        self.assertEqual(result.status, "reopened")
        self.assertTrue(result.payload.get("flow_reopened"))
        entry = OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").latest("id")
        self.assertEqual(entry.description, "Повторное открытие приемки потоком")
        overaction = AuditEntry.objects.filter(journal__code="staff_overactions").latest("id")
        self.assertIn("повторное открытие приемки потоком", overaction.description.lower())

    def test_open_receiving_placement_logs_open_status(self):
        order_id = "R-WF-OPEN-PL"
        self._create_receiving_snapshot(order_id, "placed_in_receiving")
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "Размещение на складе",
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_label": "Акт размещения",
                "act_state": "closed",
            },
        )

        result = ReceivingWorkflowService.open_receiving_placement(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            role="storekeeper",
            user=self.user,
        )

        self.assertEqual(result.status, "opened")
        self.assertEqual(result.payload.get("act_state"), "open")
        entry = OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").latest("id")
        self.assertEqual(entry.description, "Открыт акт размещения")
        self.assertEqual(entry.payload.get("act_state"), "open")

    def test_save_receiving_flow_draft_updates_existing_entry(self):
        order_id = "R-WF-DRAFT"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "items": [{"sku_code": self.sku.sku_code, "qty": 1}],
            },
        )
        draft_entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Старый черновик",
            payload={"flow_state": {"boxes": [], "pallets": []}},
        )

        result = ReceivingWorkflowService.save_receiving_flow_draft(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            role="storekeeper",
            boxes_raw=(
                '[{"code":"BOX-WF-DRAFT-1","items":[{"sku_code":"SKU-WF-1",'
                '"name":"Товар workflow","size":"42","qty":1}],"sealed":false}]'
            ),
            pallets_raw=(
                '[{"code":"PAL-WF-DRAFT-1","boxes":["BOX-WF-DRAFT-1"],'
                '"items":[],"sealed":false,"location":{"zone":"PR"}}]'
            ),
            active_box="BOX-WF-DRAFT-1",
            active_pallet="PAL-WF-DRAFT-1",
            user=self.user,
        )

        self.assertEqual(result.status, "saved")
        self.assertTrue(result.meta.get("updated_existing"))
        draft_entry.refresh_from_db()
        self.assertEqual(draft_entry.description, "Черновик приемки потоком")
        self.assertEqual(draft_entry.payload["flow_state"]["activeBox"], "BOX-WF-DRAFT-1")
        self.assertEqual(draft_entry.payload["flow_state"]["activePallet"], "PAL-WF-DRAFT-1")
        self.assertEqual(draft_entry.payload["flow_state"]["boxes"][0]["code"], "BOX-WF-DRAFT-1")
        self.assertEqual(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").count(),
            2,
        )

    def test_prepare_receiving_placement_close_builds_normalized_payload(self):
        order_id = "R-WF-PLACEMENT-PREP"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={"status": "warehouse", "status_label": "Размещение на складе"},
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки",
            payload={
                "act": "receiving",
                "act_label": "Акт приемки",
                "act_state": "closed",
                "act_items": [
                    {
                        "sku_code": self.sku.sku_code,
                        "name": self.sku.name,
                        "size": self.sku.size,
                        "actual_qty": 2,
                    }
                ],
            },
        )

        result = ReceivingWorkflowService.prepare_receiving_placement_close(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            boxes_raw=(
                '[{"code":"BOX-WF-PL-1","items":[{"sku":"SKU-WF-1","name":"Товар workflow","size":"42","qty":2}],"sealed":true}]'
            ),
            pallets_raw=(
                '[{"code":"PAL-WF-PL-1","boxes":["BOX-WF-PL-1"],"items":[],"sealed":true,'
                '"location":{"zone":"OS","row":2,"section":1,"tier":1,"cell":3}}]'
            ),
        )

        self.assertEqual(result.status, "ok")
        self.assertFalse(result.has_closed_act)
        self.assertEqual(result.placement_items[0]["actual_qty"], 2)
        self.assertEqual(result.placement_items[0]["box_qty"], 2)
        self.assertEqual(result.pallets[0]["location"]["zone"], "OS")
        self.assertEqual(result.pallets[0]["location"]["row"], 2)

    def test_prepare_receiving_placement_close_rejects_unassigned_boxes(self):
        order_id = "R-WF-PLACEMENT-BAD"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки",
            payload={
                "act": "receiving",
                "act_label": "Акт приемки",
                "act_state": "closed",
                "act_items": [
                    {
                        "sku_code": self.sku.sku_code,
                        "name": self.sku.name,
                        "size": self.sku.size,
                        "actual_qty": 1,
                    }
                ],
            },
        )

        result = ReceivingWorkflowService.prepare_receiving_placement_close(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            boxes_raw=(
                '[{"code":"BOX-WF-PL-BAD","items":[{"sku":"SKU-WF-1","name":"Товар workflow","size":"42","qty":1}],"sealed":true}]'
            ),
            pallets_raw='[]',
        )

        self.assertEqual(result.status, "invalid")
        self.assertEqual(result.reason, "unassigned_boxes")

    def test_log_receiving_flow_box_action_writes_staff_overaction(self):
        order_id = "R-WF-BOX-ACTION"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={"status": "warehouse", "status_label": "В ожидании поставки товара"},
        )

        result = ReceivingWorkflowService.log_receiving_flow_box_action(
            order_id=order_id,
            action="move_batch",
            payload={
                "box_codes": ["BOX-1", "BOX-2"],
                "source_pallet_index": 1,
                "target_pallet_index": 2,
            },
            user=self.user,
        )

        self.assertEqual(result.status, "logged")
        self.assertEqual(result.snapshot["box_count"], 2)
        overaction = AuditEntry.objects.filter(journal__code="staff_overactions").latest("id")
        self.assertIn("Перемещение группы коробов", overaction.description)
        self.assertEqual((overaction.snapshot or {}).get("order_id"), order_id)

    def test_build_receiving_flow_page_context_prefers_warehouse_state(self):
        order_id = "R-WF-CONTEXT"
        self._create_receiving_snapshot(order_id, "placed_in_receiving")
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "Размещение на складе",
                "items": [{"sku_code": self.sku.sku_code, "name": self.sku.name, "size": self.sku.size, "qty": 2}],
            },
        )

        context = ReceivingWorkflowService.build_receiving_flow_page_context(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            role="storekeeper",
        )

        self.assertEqual(context["status_label"], "Завершена приемка")
        self.assertEqual(context["items"][0]["sku_code"], self.sku.sku_code)
        self.assertEqual(context["warehouse_not_created_pallets"], 0)

    def test_build_receiving_act_page_context_hides_submit_after_storage(self):
        order_id = "R-WF-ACT-CONTEXT"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "items": [{"sku_code": self.sku.sku_code, "name": self.sku.name, "size": self.sku.size, "qty": 2}],
            },
        )
        self._create_receiving_snapshot(order_id, "stored")

        context = ReceivingWorkflowService.build_receiving_act_page_context(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            role="storekeeper",
        )

        self.assertEqual(context["status_label"], "Товар принят и размещен на складе")
        self.assertFalse(context["can_submit"])
        self.assertFalse(context["can_add_items"])

    def test_confirm_receiving_to_warehouse_logs_and_reassigns_tasks(self):
        order_id = "R-WF-CONFIRM"
        manager_task = Task.objects.create(
            title=f"Подтвердите заявку на приемку товара №{order_id}",
            description="Задача менеджеру",
            route=f"/orders/receiving/{order_id}/",
            assigned_to=self.manager,
            created_by=self.user,
        )

        result = ReceivingWorkflowService.confirm_receiving_to_warehouse(
            order_id=order_id,
            agency=self.agency,
            status_payload={
                "status": "sent_unconfirmed",
                "status_label": "Ждет подтверждения",
                "items": [{"sku_code": self.sku.sku_code, "qty": 1}],
            },
            user=self.user,
            submitted_at=timezone.make_aware(datetime(2026, 4, 22, 12, 0)),
        )

        self.assertEqual(result.payload.get("status"), "warehouse")
        self.assertEqual(result.payload.get("status_label"), "В ожидании поставки товара")
        self.assertEqual(result.manager_tasks_closed, 1)
        self.assertTrue(result.storekeeper_task_created)

        manager_task.refresh_from_db()
        self.assertEqual(manager_task.status, "done")
        storekeeper_task = (
            Task.objects.filter(
                route=f"/orders/receiving/{order_id}/",
                assigned_to__role="storekeeper",
            )
            .exclude(id=manager_task.id)
            .get()
        )
        self.assertIn("Принять заявку на приемку товара", storekeeper_task.title)
        self.assertEqual(storekeeper_task.observer.user_id, self.user.id)

        entry = OrderAuditEntry.objects.get(order_id=order_id, order_type="receiving")
        self.assertEqual(entry.description, "Подтверждено и отправлено на склад")
        self.assertEqual(entry.payload.get("status"), "warehouse")

    def test_send_receiving_to_storage_creates_move_tasks_with_selected_destination(self):
        order_id = "R-WF-STORAGE"
        self._create_receiving_snapshot(order_id, "placed_in_receiving")
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "Размещение на складе",
                "flow_closed": True,
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_label": "Акт размещения",
                "act_state": "closed",
                "act_pallets": [
                    {
                        "code": "PAL-R-WF-STORAGE",
                        "boxes": [],
                        "items": [],
                        "location": {"zone": "PR"},
                    }
                ],
            },
        )

        result = ReceivingWorkflowService.send_receiving_to_storage(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            role="storekeeper",
            user=self.user,
            destinations_raw=(
                '[{"pallet_code":"PAL-R-WF-STORAGE","destination":{"zone":"OS","row":2,"section":1,"tier":1,"cell":3}}]'
            ),
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.created_count, 1)
        task = MoveTask.objects.get()
        self.assertEqual(task.pallet_code, "PAL-R-WF-STORAGE")
        self.assertEqual(task.to_zone, "OS")
        self.assertEqual(task.to_row, 2)
        self.assertEqual(task.to_section, 1)
        self.assertEqual(task.to_tier, 1)
        self.assertEqual(task.to_cell, 3)

    def test_send_receiving_to_storage_rejects_invalid_destination_payload(self):
        order_id = "R-WF-STORAGE-BAD"
        self._create_receiving_snapshot(order_id, "placed_in_receiving")
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "Размещение на складе",
                "flow_closed": True,
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_label": "Акт размещения",
                "act_state": "closed",
                "act_pallets": [
                    {
                        "code": "PAL-R-WF-STORAGE-BAD",
                        "boxes": [],
                        "items": [],
                        "location": {"zone": "PR"},
                    }
                ],
            },
        )

        result = ReceivingWorkflowService.send_receiving_to_storage(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            role="storekeeper",
            user=self.user,
            destinations_raw='[{"pallet_code":"PAL-R-WF-STORAGE-BAD","destination":{"zone":"OS","row":2}}]',
        )

        self.assertEqual(result.status, "invalid_destination")
        self.assertIn("для зоны OS укажите ряд, секцию, ярус и ячейку", result.error_message)
        self.assertEqual(MoveTask.objects.count(), 0)

    def test_suggest_receiving_destinations_keeps_os_and_falls_back_to_mr(self):
        result = ReceivingWorkflowService.suggest_receiving_destinations(
            [
                {"code": "PAL-KEEP", "location": {"zone": "OS", "row": 2, "section": 1, "tier": 1, "cell": 3}},
                {"code": "PAL-FALLBACK", "location": {"zone": "PR"}},
            ],
            exclude_order_type="receiving",
            exclude_order_id="R-WF-SUGGEST",
            agency_id=self.agency.id,
        )

        self.assertEqual(result["PAL-KEEP"]["zone"], "OS")
        self.assertEqual(result["PAL-KEEP"]["row"], 2)
        self.assertEqual(result["PAL-FALLBACK"]["zone"], "MR")
        self.assertEqual(result["PAL-FALLBACK"]["row"], 1)

    def test_build_receiving_warehouse_move_progress_counts_existing_statuses(self):
        order_id = "R-WF-PROGRESS"
        placement_pallets = [
            {"code": "PAL-PROGRESS-1"},
            {"code": "PAL-PROGRESS-2"},
            {"code": "PAL-PROGRESS-3"},
            {"code": "PAL-PROGRESS-4"},
        ]
        OrderAuditEntry.objects.create(
            order_id="9001",
            order_type="stock_move",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Move created",
            payload={"receiving_order_id": order_id, "pallet_code": "PAL-PROGRESS-1", "status": "created"},
        )
        OrderAuditEntry.objects.create(
            order_id="9002",
            order_type="stock_move",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Move in progress",
            payload={"receiving_order_id": order_id, "pallet_code": "PAL-PROGRESS-2", "status": "in_progress"},
        )
        OrderAuditEntry.objects.create(
            order_id="9003",
            order_type="stock_move",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Move done",
            payload={"receiving_order_id": order_id, "pallet_code": "PAL-PROGRESS-3", "status": "done"},
        )

        progress = ReceivingWorkflowService.build_receiving_warehouse_move_progress(order_id, placement_pallets)

        self.assertEqual(progress["total_pallets"], 4)
        self.assertEqual(progress["created_count"], 1)
        self.assertEqual(progress["in_progress_count"], 1)
        self.assertEqual(progress["done_count"], 1)
        self.assertEqual(progress["not_created_count"], 1)
        self.assertTrue(progress["has_any_task"])

    def test_build_receiving_warehouse_move_rows_prefers_existing_move_destination(self):
        order_id = "R-WF-ROWS"
        OrderAuditEntry.objects.create(
            order_id="9101",
            order_type="stock_move",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Move created",
            payload={
                "receiving_order_id": order_id,
                "pallet_code": "PAL-ROWS-1",
                "status": "created",
                "status_label": "Передано ричтракеру",
                "to_zone": "OS",
                "to_row": 2,
                "to_section": 1,
                "to_tier": 1,
                "to_cell": 3,
            },
        )

        rows = ReceivingWorkflowService.build_receiving_warehouse_move_rows(
            order_id,
            [{"code": "PAL-ROWS-1", "location": {"zone": "PR"}}],
            agency_id=self.agency.id,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "created")
        self.assertEqual(rows[0]["status_class"], "created")
        self.assertFalse(rows[0]["selectable"])
        self.assertEqual(rows[0]["destination"]["zone"], "OS")
        self.assertEqual(rows[0]["destination"]["row"], 2)
        self.assertIn("OS", rows[0]["to_label"])

    def test_build_receiving_warehouse_move_panel_calculates_can_send(self):
        order_id = "R-WF-PANEL"
        receiving_entry = self._create_receiving_snapshot(order_id, "placed_in_receiving")
        panel = ReceivingWorkflowService.build_receiving_warehouse_move_panel(
            order_id=order_id,
            placement_pallets=[{"code": "PAL-PANEL-1", "location": {"zone": "PR"}}],
            receiving_result=WarehouseGoodsStateResolver.resolve_for_receiving_order(
                order_id=order_id,
                agency=self.agency,
                payload={"status": "warehouse", "status_label": "Размещение на складе"},
            ),
            flow_locked=True,
            role_allowed=True,
            agency_id=self.agency.id,
        )

        self.assertTrue(panel.can_send)
        self.assertEqual(panel.progress["not_created_count"], 1)
        self.assertEqual(panel.rows[0]["destination"]["zone"], "MR")
        self.assertEqual(receiving_entry.warehouse_state_code, "placed_in_receiving")

    def test_send_receiving_act_to_client_marks_order_done_and_closes_manager_tasks(self):
        order_id = "R-WF-SEND-ACT"
        manager_task = Task.objects.create(
            title=f"Проверьте размещение по заявке на приемку товара №{order_id}",
            description="Финальная задача менеджеру",
            route=f"/orders/receiving/{order_id}/",
            assigned_to=self.manager,
            created_by=self.user,
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={"status": "warehouse", "status_label": "Размещение на складе"},
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки",
            payload={
                "act": "receiving",
                "act_label": "Акт приемки",
                "act_storekeeper_signed": True,
                "act_manager_signed": True,
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_label": "Акт размещения",
                "act_state": "closed",
            },
        )

        result = ReceivingWorkflowService.send_receiving_act_to_client(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            user=self.user,
        )

        self.assertTrue(result.applied)
        self.assertEqual(result.payload.get("status"), "done")
        self.assertEqual(result.payload.get("status_label"), "Выполнена")
        self.assertEqual(result.payload.get("act_sent"), "Акт приемки")
        self.assertEqual(result.manager_tasks_closed, 1)
        manager_task.refresh_from_db()
        self.assertEqual(manager_task.status, "done")
        descriptions = list(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving")
            .order_by("created_at")
            .values_list("description", flat=True)
        )
        self.assertIn("Акт отправлен клиенту", descriptions)
        self.assertIn("Акт приемки отправлен клиенту", descriptions)

    def test_prepare_receiving_flow_completion_builds_marked_units_and_payloads(self):
        order_id = "R-WF-PREP-CZ"
        self.sku.honest_sign = True
        self.sku.save(update_fields=["honest_sign"])
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "Размещение на складе",
                "goods_type": "op",
                "receiving_mode": "cz",
                "eta_at": "2026-04-22T10:00:00",
                "vehicle_number": "A123AA790",
                "items": [
                    {
                        "sku_code": self.sku.sku_code,
                        "name": self.sku.name,
                        "size": self.sku.size,
                        "qty": 2,
                    }
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_label": "Акт размещения",
                "act_state": "closed",
            },
        )
        now = timezone.localtime()
        for idx in range(1, 3):
            MarkingCode.objects.create(
                order_type="receiving",
                order_id=order_id,
                agency=self.agency,
                sku=self.sku,
                sku_code=self.sku.sku_code,
                size=self.sku.size,
                barcode="WF-MARKED-BAR",
                box_barcode="BOX-WF-PREP-1",
                code=f"WF-CZ-{idx:04d}",
                source="scan",
                created_by=self.user,
                used_at=now,
                used_by=self.user,
            )

        result = ReceivingWorkflowService.prepare_receiving_flow_completion(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            boxes_raw=(
                '[{"code":"BOX-WF-PREP-1","items":[{"sku_code":"SKU-WF-1",'
                '"name":"Товар workflow","size":"42","qty":2}],"sealed":true}]'
            ),
            pallets_raw=(
                '[{"code":"PAL-WF-PREP-1","boxes":["BOX-WF-PREP-1"],'
                '"items":[],"sealed":true,"location":{"zone":"PR"}}]'
            ),
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.receiving_mode, "cz")
        self.assertEqual(result.status_payload.get("status"), "warehouse")
        self.assertFalse(result.has_mismatch)
        self.assertEqual(result.vehicle_number, "A123AA790")
        self.assertIn("2026-04-22T10:00:00", result.normalized_eta)
        self.assertTrue(result.has_closed_placement_act)
        self.assertEqual(len(result.act_units), 2)
        self.assertEqual(
            {unit["marking_code"] for unit in result.act_units},
            {"WF-CZ-0001", "WF-CZ-0002"},
        )
        self.assertTrue(all(unit["pallet_code"] == "PAL-WF-PREP-1" for unit in result.act_units))
        self.assertEqual(result.act_items[0]["actual_qty"], 2)
        self.assertEqual(result.placement_items[0]["box_qty"], 2)
        self.assertEqual(result.flow_state["boxes"][0]["code"], "BOX-WF-PREP-1")

    def test_prepare_receiving_flow_completion_rejects_unassigned_boxes(self):
        order_id = "R-WF-PREP-INVALID"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "Размещение на складе",
                "goods_type": "op",
                "items": [
                    {
                        "sku_code": self.sku.sku_code,
                        "name": self.sku.name,
                        "size": self.sku.size,
                        "qty": 1,
                    }
                ],
            },
        )

        result = ReceivingWorkflowService.prepare_receiving_flow_completion(
            order_id=order_id,
            entries=list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")),
            boxes_raw=(
                '[{"code":"BOX-WF-PREP-INVALID","items":[{"sku_code":"SKU-WF-1",'
                '"name":"Товар workflow","size":"42","qty":1}],"sealed":true}]'
            ),
            pallets_raw=(
                '[{"code":"PAL-WF-PREP-INVALID","boxes":[],"items":[{"sku_code":"SKU-WF-1",'
                '"name":"Товар workflow","size":"42","qty":1}],"sealed":true,'
                '"location":{"zone":"PR"}}]'
            ),
        )

        self.assertEqual(result.status, "invalid")
        self.assertEqual(result.reason, "unassigned_boxes")

    def test_complete_receiving_flow_handles_audit_and_followup_tasks(self):
        order_id = "R-WF-1"
        Task.objects.create(
            title="Принять заявку на приемку товара",
            description="Исходная задача кладовщику",
            route=f"/orders/receiving/{order_id}/",
            assigned_to=self.storekeeper,
            created_by=self.user,
        )

        result = ReceivingWorkflowService.complete_receiving_flow(
            order_id=order_id,
            agency=self.agency,
            status_payload={
                "status": "warehouse",
                "status_label": "Взята в работу",
                "goods_type": "op",
            },
            has_mismatch=False,
            receiving_mode="standard",
            act_items=[
                {
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "size": self.sku.size,
                    "planned_qty": 2,
                    "actual_qty": 2,
                    "comment": "",
                }
            ],
            placement_items=[
                {
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "size": self.sku.size,
                    "actual_qty": 2,
                    "box_qty": 2,
                    "pallet_qty": 2,
                    "comment": "",
                }
            ],
            boxes=[
                {
                    "code": "BOX-WF-1",
                    "sealed": True,
                    "items": [
                        {
                            "sku": self.sku.sku_code,
                            "name": self.sku.name,
                            "size": self.sku.size,
                            "barcode": "WF-BOX-1",
                            "qty": 2,
                        }
                    ],
                }
            ],
            pallets=[
                {
                    "code": "PAL-WF-1",
                    "sealed": True,
                    "boxes": ["BOX-WF-1"],
                    "items": [],
                    "location": {"zone": "PR"},
                }
            ],
            flow_state={"boxes": [], "pallets": []},
            eta_at="2026-04-22T10:00:00+03:00",
            vehicle_number="A123AA790",
            has_closed_placement_act=False,
            user=self.user,
            submitted_at=timezone.make_aware(datetime(2026, 4, 22, 11, 0)),
        )

        self.assertFalse(result.placement_previously_closed)
        self.assertEqual(result.storekeeper_tasks_closed, 1)
        self.assertTrue(result.manager_followup_created)
        self.assertEqual(result.act_payload.get("act"), "receiving")
        self.assertEqual(result.placement_payload.get("act"), "placement")

        storekeeper_task = Task.objects.get(
            route=f"/orders/receiving/{order_id}/",
            assigned_to=self.storekeeper,
        )
        self.assertEqual(storekeeper_task.status, "done")

        manager_task = Task.objects.get(
            route=f"/orders/receiving/{order_id}/",
            assigned_to=self.manager,
        )
        self.assertIn("Проверьте размещение", manager_task.title)
        self.assertEqual(manager_task.observer.user_id, self.user.id)

        entries = list(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="receiving").order_by("created_at")
        )
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].description, "Создан акт приемки")
        self.assertEqual(entries[0].payload.get("act"), "receiving")
        self.assertEqual(entries[1].description, "Создан акт размещения")
        self.assertEqual(entries[1].payload.get("act"), "placement")

    def test_close_receiving_placement_reuses_existing_manager_followup(self):
        order_id = "R-WF-2"
        Task.objects.create(
            title=f"Проверьте размещение по заявке на приемку товара №{order_id}",
            description="Уже существует follow-up",
            route=f"/orders/receiving/{order_id}/",
            assigned_to=self.manager,
            created_by=self.user,
        )
        open_storekeeper_task = Task.objects.create(
            title="Принять заявку на приемку товара",
            description="Открытая задача кладовщику",
            route=f"/orders/receiving/{order_id}/",
            assigned_to=self.storekeeper,
            created_by=self.user,
        )

        result = ReceivingWorkflowService.close_receiving_placement(
            order_id=order_id,
            agency=self.agency,
            status_payload={
                "status": "warehouse",
                "status_label": "Размещение на складе",
                "act": "placement",
                "act_state": "open",
                "goods_type": "op",
            },
            placement_items=[
                {
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "size": self.sku.size,
                    "actual_qty": 1,
                    "box_qty": 1,
                    "pallet_qty": 1,
                    "comment": "",
                }
            ],
            boxes=[
                {
                    "code": "BOX-WF-2",
                    "sealed": True,
                    "items": [
                        {
                            "sku": self.sku.sku_code,
                            "name": self.sku.name,
                            "size": self.sku.size,
                            "barcode": "WF-BOX-2",
                            "qty": 1,
                        }
                    ],
                }
            ],
            pallets=[
                {
                    "code": "PAL-WF-2",
                    "sealed": True,
                    "boxes": ["BOX-WF-2"],
                    "items": [],
                    "location": {"zone": "PR"},
                }
            ],
            has_closed_act=True,
            user=self.user,
            submitted_at=timezone.make_aware(datetime(2026, 4, 22, 12, 0)),
        )

        self.assertTrue(result.placement_previously_closed)
        self.assertEqual(result.storekeeper_tasks_closed, 0)
        self.assertFalse(result.manager_followup_created)
        self.assertEqual(
            Task.objects.filter(
                route=f"/orders/receiving/{order_id}/",
                assigned_to=self.manager,
            ).count(),
            1,
        )
        open_storekeeper_task.refresh_from_db()
        self.assertNotEqual(open_storekeeper_task.status, "done")

        entry = OrderAuditEntry.objects.get(order_id=order_id, order_type="receiving")
        self.assertEqual(entry.description, "Обновлен акт размещения")
        self.assertEqual(entry.payload.get("act_state"), "closed")


class OrdersReceivingWarehouseDetailTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="orders_receiving_detail", password="pwd")
        Employee.objects.create(
            full_name="Кладовщик деталей",
            role="storekeeper",
            user=self.user,
            is_active=True,
        )
        self.client.login(username="orders_receiving_detail", password="pwd")
        self.agency = Agency.objects.create(agn_name="Клиент приемки деталки")

    def _create_receiving_status(self, order_id: str, extra_payload: dict | None = None):
        payload = {
            "status": "warehouse",
            "status_label": "В ожидании поставки товара",
            "goods_type": "op",
            "goods_type_label": "Оптовый",
            "items": [
                {
                    "sku_code": "SKU-R-BASE",
                    "name": "Receiving Base",
                    "size": "42",
                    "qty": 2,
                }
            ],
        }
        if extra_payload:
            payload.update(extra_payload)
        return OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload=payload,
        )

    def test_receiving_status_helper_prefers_warehouse_state(self):
        entry = SimpleNamespace(
            payload={"act": "placement", "act_state": "open"},
            order_type="receiving",
            order_id="R-H-1",
            agency=self.agency,
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="R-H-1",
            sku_code="SKU-R-1",
            name="Receiving Helper",
            size="42",
            barcode="200000009201",
            goods_type="gv",
            qty=5,
            available_qty=5,
            container_code="PAL-R-1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="placed_in_receiving",
        )

        self.assertEqual(_status_label_from_entry(entry), "Завершена приемка")

    def test_receiving_current_responsible_prefers_warehouse_state(self):
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="R-H-2",
            sku_code="SKU-R-2",
            name="Receiving Responsible",
            size="42",
            barcode="200000009202",
            goods_type="gv",
            qty=6,
            available_qty=6,
            container_code="PAL-R-2",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="stored",
        )
        entry = SimpleNamespace(
            payload={"status": "warehouse"},
            order_type="receiving",
            order_id="R-H-2",
            agency=self.agency,
        )

        self.assertIn("Кладовщик", _current_responsible_label(entry))

    def test_receiving_history_actor_prefers_warehouse_state(self):
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="R-H-3",
            sku_code="SKU-R-3",
            name="Receiving History",
            size="42",
            barcode="200000009203",
            goods_type="gv",
            qty=7,
            available_qty=7,
            container_code="PAL-R-3",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="placed_in_receiving",
        )
        entry = SimpleNamespace(
            payload={"act": "placement", "act_state": "open"},
            order_type="receiving",
            order_id="R-H-3",
            agency=self.agency,
            action="status",
            user=None,
        )

        self.assertIn("Кладовщик", _history_actor_label(entry))

    def test_receiving_detail_page_prefers_warehouse_status_and_responsible(self):
        order_id = "R-DETAIL-WH-1"
        self._create_receiving_status(order_id)
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-DETAIL",
            name="Receiving Detail",
            size="42",
            barcode="200000009204",
            goods_type="gv",
            qty=4,
            available_qty=4,
            container_code="PAL-R-D1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="placed_in_receiving",
        )

        response = self.client.get(f"/orders/receiving/{order_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Завершена приемка")
        self.assertTrue(response.context["can_create_receiving_act"])
        self.assertTrue(response.context["can_open_flow"])
        self.assertEqual(
            response.context["receiving_flow_url"],
            f"/orders/receiving/{order_id}/flow/",
        )
        self.assertFalse(response.context["can_manage_storage_placement"])
        self.assertEqual(response.context["reachtruck_request_url"], "")
        self.assertEqual(
            response.context["next_step_label"],
            "Создать или завершить размещение в хранение",
        )

    def test_receiving_detail_hides_receiving_actions_after_storage(self):
        order_id = "R-DETAIL-STORED-1"
        self._create_receiving_status(order_id)
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OS")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-STORED",
            name="Receiving Stored",
            size="42",
            barcode="200000009209",
            goods_type="gv",
            qty=4,
            available_qty=4,
            container_code="PAL-R-S1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="stored",
        )

        response = self.client.get(f"/orders/receiving/{order_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Товар принят и размещен на складе")
        self.assertEqual(response.context["next_step_label"], "Товар доступен на складе")
        self.assertFalse(response.context["can_create_receiving_act"])
        self.assertFalse(response.context["can_open_flow"])
        self.assertFalse(response.context["can_manage_storage_placement"])

    def test_receiving_detail_page_offers_storage_placement_after_flow_closed(self):
        order_id = "R-DETAIL-FLOW-CLOSED-1"
        self._create_receiving_status(order_id)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Поток закрыт",
            payload={
                "flow_closed": True,
                "flow_closed_at": timezone.localtime().isoformat(),
                "flow_boxes": [{"code": "BOX-R-CLOSED-1"}],
                "flow_pallets": [{"code": "PAL-R-CLOSED-1"}],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки",
            payload={
                "act": "receiving",
                "act_label": "Акт приемки",
                "act_items": [],
            },
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-CLOSED",
            name="Receiving Detail Closed",
            size="42",
            barcode="200000009211",
            goods_type="gv",
            qty=4,
            available_qty=4,
            container_code="PAL-R-CLOSED-1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="placed_in_receiving",
        )

        response = self.client.get(f"/orders/receiving/{order_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Завершена приемка")
        self.assertTrue(response.context["can_manage_storage_placement"])
        self.assertEqual(
            response.context["storage_placement_action_label"],
            "Создать размещение в хранение",
        )
        self.assertEqual(
            response.context["receiving_flow_url"],
            f"/orders/receiving/{order_id}/flow/",
        )
        self.assertEqual(response.context["reachtruck_request_url"], "")

    def test_receiving_detail_page_links_to_reachtruck_when_move_exists(self):
        order_id = "R-DETAIL-MOVE-1"
        self._create_receiving_status(order_id)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Поток закрыт",
            payload={
                "flow_closed": True,
                "flow_closed_at": timezone.localtime().isoformat(),
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки",
            payload={
                "act": "receiving",
                "act_label": "Акт приемки",
                "act_items": [],
            },
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-MOVE",
            name="Receiving Detail Move",
            size="42",
            barcode="200000009210",
            goods_type="gv",
            qty=4,
            available_qty=4,
            container_code="PAL-R-M1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="placed_in_receiving",
        )
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_RECEIVING,
            context_id=order_id,
            agency=self.agency,
            destination_zone="OS",
        )
        MoveTask.objects.create(
            request=move_request,
            pallet_code="PAL-R-M1",
            to_zone="OS",
            status=MoveTask.STATUS_CREATED,
            legacy_order_id="MOVE-R-DETAIL-1",
        )

        response = self.client.get(f"/orders/receiving/{order_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Размещение на складе")
        self.assertTrue(response.context["can_manage_storage_placement"])
        self.assertEqual(
            response.context["storage_placement_action_label"],
            "Контроль размещения в хранение",
        )
        self.assertEqual(
            response.context["reachtruck_request_url"],
            f"/reachtruck/?mobile_category=movement&mobile_request=receiving:{order_id}",
        )


class OrdersJournalWarehouseStatusTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="orders_journal_wh", password="pwd")
        Employee.objects.create(
            full_name="Кладовщик журнала",
            role="storekeeper",
            user=self.user,
            is_active=True,
        )
        Employee.objects.create(
            full_name="Руководитель обработки журнала",
            role="processing_head",
            is_active=True,
        )
        self.client.login(username="orders_journal_wh", password="pwd")
        self.agency = Agency.objects.create(agn_name="Клиент журнала")

    def _create_receiving_status(self, order_id: str, extra_payload: dict | None = None):
        payload = {
            "status": "warehouse",
            "status_label": "В ожидании поставки товара",
            "goods_type": "op",
            "goods_type_label": "Оптовый",
            "items": [
                {
                    "sku_code": "SKU-R-J-BASE",
                    "name": "Receiving Journal Base",
                    "size": "42",
                    "qty": 2,
                }
            ],
        }
        if extra_payload:
            payload.update(extra_payload)
        return OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload=payload,
        )

    def test_journal_receiving_entry_prefers_warehouse_status(self):
        order_id = "R-JOURNAL-1"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "goods_type": "op",
                "items": [
                    {
                        "sku_code": "SKU-R-J",
                        "name": "Товар журнала приемки",
                        "size": "42",
                        "qty": 2,
                    }
                ],
            },
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-J",
            name="Товар журнала приемки",
            size="42",
            barcode="200000009207",
            goods_type="gv",
            qty=2,
            available_qty=2,
            container_code="PAL-R-J1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="placed_in_receiving",
        )

        response = self.client.get("/orders/?tab=journal")

        self.assertEqual(response.status_code, 200)
        entries = list(response.context["entries"])
        row = next(item for item in entries if item["order_type"] == "receiving" and item["order_id"] == order_id)
        self.assertEqual(row["status_label"], "Завершена приемка")
        self.assertIn("кладовщика", row["action_label"].lower())
        self.assertEqual(row["next_step_label"], "Передать паллеты на размещение в хранение")

    def test_journal_processing_entry_prefers_warehouse_status(self):
        order_id = "P-JOURNAL-1"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус обработки",
            payload={
                "status": "processing_head",
                "status_label": "Передано в обработку",
                "article": "SKU-P-J",
                "product_name": "Товар журнала обработки",
            },
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OBR")
        snapshot = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="legacy_stock",
            source_context_id=f"legacy-{order_id}",
            sku_code="SKU-P-J",
            name="Товар журнала обработки",
            size="42",
            barcode="200000009208",
            goods_type="gv",
            qty=5,
            available_qty=0,
            processing_reserved_qty=5,
            container_code="PAL-P-J1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="processing_in_progress",
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
            sku_code=snapshot.sku_code,
            size=snapshot.size,
            barcode=snapshot.barcode,
            goods_type=snapshot.goods_type,
            qty_reserved=5,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        response = self.client.get("/orders/?tab=journal")

        self.assertEqual(response.status_code, 200)
        entries = list(response.context["entries"])
        row = next(item for item in entries if item["order_type"] == "processing" and item["order_id"] == order_id)
        self.assertEqual(row["status_label"], "Товар в обработке")
        self.assertIn("руководителя обработки", row["action_label"].lower())
        self.assertEqual(row["next_step_label"], "Завершить обработку и подготовить размещение")

    def test_receiving_flow_page_prefers_warehouse_status(self):
        order_id = "R-FLOW-WH-1"
        self._create_receiving_status(order_id)
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-FLOW",
            name="Receiving Flow",
            size="42",
            barcode="200000009205",
            goods_type="gv",
            qty=3,
            available_qty=3,
            container_code="PAL-R-F1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="placed_in_receiving",
        )

        response = self.client.get(f"/orders/receiving/{order_id}/flow/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Завершена приемка")

    def test_receiving_flow_page_shows_storage_placement_after_reachtruck_task_created(self):
        order_id = "R-FLOW-WH-2"
        self._create_receiving_status(order_id)
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-FLOW-2",
            name="Receiving Flow Reachtruck",
            size="42",
            barcode="200000009215",
            goods_type="gv",
            qty=3,
            available_qty=3,
            container_code="PAL-R-F2",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="placed_in_receiving",
        )
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_RECEIVING,
            context_id=order_id,
            agency=self.agency,
            destination_zone="OS",
        )
        MoveTask.objects.create(
            request=move_request,
            pallet_code="PAL-R-F2",
            to_zone="OS",
            status=MoveTask.STATUS_CREATED,
            legacy_order_id="MOVE-R-FLOW-2",
        )

        response = self.client.get(f"/orders/receiving/{order_id}/flow/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Размещение на складе")

    def test_receiving_flow_page_contains_separate_pallet_label_template(self):
        order_id = "R-FLOW-LABELS-1"
        self._create_receiving_status(order_id)

        response = self.client.get(f"/orders/receiving/{order_id}/flow/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="receiving-pallet-label-template"', html=False)
        self.assertContains(response, 'data-label-type="pallet"', html=False)

    def test_receiving_flow_page_contains_stockmap_picker_for_warehouse_move(self):
        order_id = "R-FLOW-STOCKMAP-1"
        self._create_receiving_status(order_id)

        response = self.client.get(f"/orders/receiving/{order_id}/flow/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "warehouse-move-picker-btn", html=False)
        self.assertContains(response, "stockmap_location_pick", html=False)
        self.assertContains(response, "openWarehouseStockMapPicker", html=False)

    def test_receiving_act_page_prefers_warehouse_status(self):
        order_id = "R-ACT-WH-1"
        self._create_receiving_status(order_id)
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="PR")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-ACT",
            name="Receiving Act",
            size="42",
            barcode="200000009206",
            goods_type="gv",
            qty=2,
            available_qty=2,
            container_code="PAL-R-A1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="placed_in_receiving",
        )

        response = self.client.get(f"/orders/receiving/{order_id}/act/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Завершена приемка")
        self.assertTrue(response.context["can_submit"])
        self.assertTrue(response.context["can_add_items"])

    def test_receiving_act_page_hides_submit_after_storage(self):
        order_id = "R-ACT-STORED-1"
        self._create_receiving_status(order_id)
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OS")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-ACT-ST",
            name="Receiving Act Stored",
            size="42",
            barcode="200000009210",
            goods_type="gv",
            qty=2,
            available_qty=2,
            container_code="PAL-R-AS1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="stored",
        )

        response = self.client.get(f"/orders/receiving/{order_id}/act/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Товар принят и размещен на складе")
        self.assertFalse(response.context["can_submit"])
        self.assertFalse(response.context["can_add_items"])

    def test_receiving_placement_page_hides_reopen_after_storage(self):
        order_id = "R-PLACEMENT-STORED-1"
        self._create_receiving_status(order_id)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_label": "Акт размещения",
                "act_state": "closed",
                "goods_type": "op",
                "goods_type_label": "Оптовый",
                "act_items": [
                    {
                        "sku_code": "SKU-R-PL-ST",
                        "name": "Receiving Placement Stored",
                        "size": "42",
                        "actual_qty": 2,
                    }
                ],
                "act_boxes": [],
                "act_pallets": [],
            },
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OS")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-PL-ST",
            name="Receiving Placement Stored",
            size="42",
            barcode="200000009211",
            goods_type="gv",
            qty=2,
            available_qty=2,
            container_code="PAL-R-PS1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="stored",
        )

        response = self.client.get(f"/orders/receiving/{order_id}/placement/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Товар принят и размещен на складе")
        self.assertFalse(response.context["can_open_act"])

    def test_receiving_placement_post_is_rejected_after_storage(self):
        order_id = "R-PLACEMENT-POST-STORED-1"
        self._create_receiving_status(order_id)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт приемки",
            payload={
                "act": "receiving",
                "act_label": "Акт приемки",
                "act_state": "closed",
                "act_items": [
                    {
                        "sku_code": "SKU-R-POST-ST",
                        "name": "Receiving Placement Post Stored",
                        "size": "42",
                        "actual_qty": 2,
                    }
                ],
            },
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OS")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-POST-ST",
            name="Receiving Placement Post Stored",
            size="42",
            barcode="200000009214",
            goods_type="gv",
            qty=2,
            available_qty=2,
            container_code="PAL-R-PST1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="stored",
        )

        response = self.client.post(
            f"/orders/receiving/{order_id}/placement/",
            {
                "boxes_json": (
                    '[{"code":"BOX-R-PST-1","items":[{"sku":"SKU-R-POST-ST","name":"Receiving Placement Post Stored",'
                    '"size":"42","qty":2}],"sealed":true}]'
                ),
                "pallets_json": (
                    '[{"code":"PAL-R-PST-1","boxes":["BOX-R-PST-1"],"items":[],"sealed":true,'
                    '"location":{"zone":"OS","row":1,"section":1,"tier":1,"cell":1}}]'
                ),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/orders/receiving/", response.url)
        self.assertIn("?error=1", response.url)

    def test_receiving_flow_hides_move_action_after_storage(self):
        order_id = "R-FLOW-STORED-1"
        self._create_receiving_status(order_id)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="update",
            agency=self.agency,
            user=self.user,
            description="Поток закрыт",
            payload={
                "flow_closed": True,
                "flow_closed_at": timezone.localtime().isoformat(),
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт размещения",
            payload={
                "act": "placement",
                "act_label": "Акт размещения",
                "act_state": "closed",
                "act_pallets": [
                    {
                        "code": "PAL-R-FS-1",
                        "boxes": [],
                        "items": [],
                        "sealed": True,
                        "location": {"zone": "OS"},
                    }
                ],
            },
        )
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OS")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-FLOW-ST",
            name="Receiving Flow Stored",
            size="42",
            barcode="200000009212",
            goods_type="gv",
            qty=2,
            available_qty=2,
            container_code="PAL-R-FS-1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="stored",
        )

        response = self.client.get(f"/orders/receiving/{order_id}/flow/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Товар принят и размещен на складе")
        self.assertFalse(response.context["can_send_to_warehouse_action"])

    def test_receiving_flow_item_weight_endpoint_updates_sku_weight(self):
        order_id = "R-FLOW-WEIGHT-1"
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-R-WEIGHT",
            name="Receiving Weight",
            size="43",
        )
        self._create_receiving_status(
            order_id,
            {
                "items": [
                    {
                        "sku_code": sku.sku_code,
                        "name": sku.name,
                        "size": sku.size,
                        "qty": 3,
                    }
                ]
            },
        )

        response = self.client.post(
            f"/orders/receiving/{order_id}/flow/item-weight/",
            data=json.dumps({"sku_code": sku.sku_code, "weight_kg": "0.245"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            response.content,
            {
                "ok": True,
                "sku_code": sku.sku_code,
                "weight_kg": "0.245",
            },
        )
        sku.refresh_from_db()
        self.assertEqual(str(sku.weight_kg), "0.245")
        self.assertEqual(str(sku.weight_gross_kg), "0.245")

    def test_receiving_flow_rejects_draft_save_after_storage(self):
        order_id = "R-FLOW-DRAFT-STORED-1"
        self._create_receiving_status(order_id)
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OS")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-R-FLOW-DR",
            name="Receiving Flow Draft Stored",
            size="42",
            barcode="200000009213",
            goods_type="gv",
            qty=2,
            available_qty=2,
            container_code="PAL-R-FD-1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="stored",
        )

        response = self.client.post(
            f"/orders/receiving/{order_id}/flow/",
            {
                "flow_action": "draft",
                "boxes_json": "[]",
                "pallets_json": "[]",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertJSONEqual(response.content, {"ok": False, "error": "not_allowed"})
