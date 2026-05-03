from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.utils import timezone

from audit.models import OrderAuditEntry
from reachtruck.models import MoveTask
from shipping.models import ShippingOrder, ShippingOrderItem, ShippingReserve
from sku.models import Agency
from sku.models import SKU, SKUBarcode
from sklad.models import (
    InventoryState,
    StockPalletState,
    WarehouseContainer,
    WarehouseEvent,
    WarehouseLocation,
    WarehouseOperation,
    WarehouseOperationTask,
    WarehouseReserve,
    WarehouseStockSnapshot,
)
from sklad.services import OperationalStockService
from sklad.services import (
    WarehouseActionPolicy,
    WarehouseCommandService,
    WarehouseEventType,
    WarehouseStateCode,
    WarehouseTransitionError,
    WarehouseTransitionService,
    WarehouseWritePathService,
)
from sklad.services.stock_availability import StockAvailabilityService
from sklad.ui_services import build_inventory_journal_page
from .stock_state import (
    _apply_processing_source_deductions,
    _ensure_pallet_location,
    _goods_type_label,
    _inventory_key,
    _normalize_goods_type,
    _normalize_location,
    _receiving_removed_qty_map,
    refresh_materialized_stock_state_for_agency,
    rebuild_stock_snapshot_for_agency,
    _shipping_shipped_qty_map,
)


class StockStateHelpersTests(SimpleTestCase):
    def test_normalize_goods_type_and_inventory_key(self):
        self.assertEqual(_normalize_goods_type("op"), "оптовый")
        self.assertEqual(_normalize_goods_type("  Готовый "), "готовый")
        self.assertEqual(_inventory_key(" SKU-1 ", " 42 ", " OP "), ("sku-1", "42", "оптовый"))

    def test_goods_type_label_prefers_explicit_label(self):
        self.assertEqual(_goods_type_label("op", "Мой тип", "Оптовый"), "Мой тип")
        self.assertEqual(_goods_type_label("gv", "", "Оптовый"), "Готовый")
        self.assertEqual(_goods_type_label("", "", "Оптовый"), "Оптовый")

    def test_apply_processing_source_deductions_uses_older_rows_first(self):
        rows = [
            {
                "sku": "SKU-1",
                "size": "42",
                "goods_type": "Оптовый",
                "qty": 50,
                "order_type": "receiving",
                "created_at": 1,
            },
            {
                "sku": "SKU-1",
                "size": "42",
                "goods_type": "Оптовый",
                "qty": 30,
                "order_type": "processing",
                "created_at": 2,
            },
            {
                "sku": "SKU-1",
                "size": "42",
                "goods_type": "Готовый",
                "qty": 40,
                "order_type": "processing",
                "created_at": 3,
            },
        ]
        key = _inventory_key("SKU-1", "42", "Оптовый")
        adjusted = _apply_processing_source_deductions(rows, {key: 60}, {key: 10})

        self.assertEqual(len(adjusted), 2)
        self.assertEqual(adjusted[0]["goods_type"], "Оптовый")
        self.assertEqual(adjusted[0]["qty"], 30)
        self.assertEqual(adjusted[1]["goods_type"], "Готовый")
        self.assertEqual(adjusted[1]["qty"], 40)

    def test_receiving_removed_qty_map_uses_expected_minus_factual(self):
        entry = SimpleNamespace(
            order_type="receiving",
            order_id="9",
            payload={
                "act_items_removed": True,
                "act_items": [
                    {"sku": "SKU-1", "size": "42", "qty": 10},
                ],
                "act_boxes": [
                    {"items": [{"sku": "SKU-1", "size": "42", "qty": 7}]},
                ],
                "act_pallets": [],
            },
        )
        latest = {("receiving", "9"): entry}
        removed = _receiving_removed_qty_map(latest, {})

        self.assertEqual(removed.get(("sku-1", "42", "оптовый")), 3)

    def test_location_normalization_and_defaulting(self):
        normalized = _normalize_location(
            {"location": {"zone": "основной склад", "row": "2", "section": "3", "tier": "4", "cell": "5"}},
            "PR",
        )
        self.assertEqual(normalized["zone"], "OS")
        self.assertEqual(normalized["location"], "OS · Ряд 2 · Секция 3 · Ярус 4 · Ячейка 5")

        row = _ensure_pallet_location({"pallet_code": "P-1", "order_type": "processing"})
        self.assertEqual(row["zone"], "OBR")
        self.assertEqual(row["location"], "OBR · Зона обработки")

    def test_shipping_shipped_qty_map_uses_latest_shipped_payload(self):
        entries = [
            SimpleNamespace(
                order_type="shipping",
                order_id="SO-1",
                payload={
                    "shipping_state": "reserved",
                    "shipped_items": [
                        {"sku": "SKU-1", "size": "42", "goods_type": "Готовый", "qty": 99},
                    ],
                },
            ),
            SimpleNamespace(
                order_type="shipping",
                order_id="SO-1",
                payload={
                    "shipping_state": "shipped",
                    "shipped_items": [
                        {"sku": "SKU-1", "size": "42", "goods_type": "Готовый", "qty": 10},
                    ],
                },
            ),
        ]
        shipped_map = _shipping_shipped_qty_map(entries)
        self.assertEqual(shipped_map, {("sku-1", "42", "готовый"): 10})


class StockAvailabilityServiceTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Availability Agency")
        StockPalletState.objects.create(
            agency=self.agency,
            order_type="receiving",
            order_id="1000",
            sku="SKU-1",
            name="Товар 1",
            size="42",
            goods_type="Не обработанный",
            qty=50,
            state=StockPalletState.STATE_WAREHOUSE,
        )
        InventoryState.objects.create(
            agency=self.agency,
            order_type="processing",
            order_id="2000",
            sku="SKU-1",
            size="42",
            goods_type="Не обработанный",
            qty=50,
            state=InventoryState.STATE_PROCESSING,
        )

    def test_inventory_items_for_agency_excludes_current_processing_reserve(self):
        without_exclusion = StockAvailabilityService.inventory_items_for_agency(self.agency)
        self.assertEqual(without_exclusion, [])

        with_exclusion = StockAvailabilityService.inventory_items_for_agency(
            self.agency,
            exclude_processing_order_id="2000",
        )
        self.assertEqual(len(with_exclusion), 1)
        self.assertEqual(with_exclusion[0]["sku"], "SKU-1")
        self.assertEqual(with_exclusion[0]["qty"], 50)

    def test_inventory_items_for_agency_prefers_warehouse_processing_reserve_over_inventory_state(self):
        InventoryState.objects.filter(agency=self.agency).delete()
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="2000",
            sku_code="SKU-1",
            size="42",
            goods_type="Не обработанный",
            qty_reserved=50,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        without_exclusion = StockAvailabilityService.inventory_items_for_agency(self.agency)
        self.assertEqual(without_exclusion, [])

        with_exclusion = StockAvailabilityService.inventory_items_for_agency(
            self.agency,
            exclude_processing_order_id="2000",
        )
        self.assertEqual(len(with_exclusion), 1)
        self.assertEqual(with_exclusion[0]["sku"], "SKU-1")
        self.assertEqual(with_exclusion[0]["qty"], 50)

    def test_inventory_items_for_agency_excludes_shipping_reserve(self):
        InventoryState.objects.filter(agency=self.agency).delete()
        shipping_order = ShippingOrder.objects.create(
            number="SO-TEST-1",
            agency=self.agency,
            status=ShippingOrder.STATUS_SUBMITTED,
        )
        shipping_item = ShippingOrderItem.objects.create(
            order=shipping_order,
            sku_code="SKU-1",
            name="Товар 1",
            size="42",
            goods_type="Не обработанный",
            qty_requested=20,
        )
        ShippingReserve.objects.create(
            order=shipping_order,
            item=shipping_item,
            agency=self.agency,
            sku_code="SKU-1",
            size="42",
            goods_type="Не обработанный",
            qty=20,
        )

        items = StockAvailabilityService.inventory_items_for_agency(self.agency)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["sku"], "SKU-1")
        self.assertEqual(items[0]["qty"], 30)

    def test_inventory_items_for_agency_does_not_rebuild_from_audit_when_stock_empty(self):
        StockPalletState.objects.filter(agency=self.agency).delete()
        OrderAuditEntry.objects.create(
            order_id="R-AUDIT-1",
            order_type="receiving",
            action="status",
            agency=self.agency,
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_items": [
                    {
                        "sku": "SKU-1",
                        "name": "Товар 1",
                        "size": "42",
                        "qty": 50,
                    }
                ],
            },
        )

        items = StockAvailabilityService.inventory_items_for_agency(self.agency)

        self.assertEqual(items, [])
        self.assertFalse(StockPalletState.objects.filter(agency=self.agency).exists())

    def test_inventory_items_for_agency_uses_warehouse_snapshot_when_legacy_rows_missing(self):
        StockPalletState.objects.filter(agency=self.agency).delete()
        InventoryState.objects.filter(agency=self.agency).delete()
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=1,
            display_name="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
        )
        pallet = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="PAL-INV-1",
            current_location=location,
        )
        box = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="BOX-INV-1",
            parent_container=pallet,
            current_location=location,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="R-SNAP-INV",
            sku_code="SKU-1",
            name="Товар 1",
            size="42",
            goods_type="Не обработанный",
            qty=50,
            available_qty=50,
            container=box,
            container_code=box.container_code,
            parent_container=pallet,
            location=location,
            zone_code="OS",
            zone_kind=location.zone_kind,
            warehouse_state_code=WarehouseStateCode.STORED.value,
        )

        items = StockAvailabilityService.inventory_items_for_agency(self.agency)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["sku"], "SKU-1")
        self.assertEqual(items[0]["qty"], 50)

    def test_occupied_os_helpers_use_warehouse_snapshots_when_available(self):
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=2,
            section_no=3,
            tier_no=1,
            cell_no=2,
            display_name="OS · Ряд 2 · Секция 3 · Ярус 1 · Ячейка 2",
        )
        container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="PAL-SNAPSHOT-OS",
            current_location=location,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="RCV-OS-1",
            sku_code="SKU-OS-1",
            name="Snapshot товар",
            qty=12,
            available_qty=12,
            container=container,
            container_code=container.container_code,
            location=location,
            zone_code="OS",
            zone_kind=location.zone_kind,
            warehouse_state_code="stored",
        )

        keys = StockAvailabilityService.occupied_os_cell_keys()
        cells = StockAvailabilityService.occupied_os_cells(include_agency=True)
        sections = StockAvailabilityService.occupied_os_section_agencies()

        self.assertIn((2, 3, 1, 2), keys)
        self.assertEqual(cells[0]["agency_id"], self.agency.id)
        self.assertIn(self.agency.id, sections[(2, 3)])


class WarehouseActionPolicyTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Policy Availability Agency")
        StockPalletState.objects.create(
            agency=self.agency,
            order_type="receiving",
            order_id="1000",
            sku="SKU-1",
            name="Товар 1",
            size="42",
            goods_type="Не обработанный",
            qty=50,
            state=StockPalletState.STATE_WAREHOUSE,
        )
        InventoryState.objects.create(
            agency=self.agency,
            order_type="processing",
            order_id="2000",
            sku="SKU-1",
            size="42",
            goods_type="Не обработанный",
            qty=50,
            state=InventoryState.STATE_PROCESSING,
        )

    def _state(self, code: WarehouseStateCode):
        return SimpleNamespace(code=code)

    def test_can_create_receiving_act_allows_receiving_states(self):
        decision = WarehouseActionPolicy.can_create_receiving_act(
            self._state(WarehouseStateCode.PLACED_IN_RECEIVING),
            has_receiving_act=False,
            role="storekeeper",
        )

        self.assertTrue(decision.allowed)

    def test_can_create_receiving_act_blocks_after_storage(self):
        decision = WarehouseActionPolicy.can_create_receiving_act(
            self._state(WarehouseStateCode.STORED),
            has_receiving_act=False,
            role="storekeeper",
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "state_forbidden")

    def test_can_open_receiving_flow_requires_active_receiving_context(self):
        decision = WarehouseActionPolicy.can_open_receiving_flow(
            self._state(WarehouseStateCode.PLACED_IN_RECEIVING),
            role="storekeeper",
            client_view=False,
            flow_closed=False,
            can_create_receiving_act=True,
            has_receiving_act=False,
            flow_has_data=False,
        )

        self.assertTrue(decision.allowed)

    def test_can_open_receiving_flow_blocks_after_storage(self):
        decision = WarehouseActionPolicy.can_open_receiving_flow(
            self._state(WarehouseStateCode.STORED),
            role="storekeeper",
            client_view=False,
            flow_closed=False,
            can_create_receiving_act=False,
            has_receiving_act=True,
            flow_has_data=True,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "state_forbidden")

    def test_can_send_receiving_to_storage_requires_open_work(self):
        decision = WarehouseActionPolicy.can_send_receiving_to_storage(
            self._state(WarehouseStateCode.PLACED_IN_RECEIVING),
            flow_closed=True,
            role_allowed=True,
            not_created_count=1,
        )

        self.assertTrue(decision.allowed)

    def test_can_send_receiving_to_storage_blocks_after_storage(self):
        decision = WarehouseActionPolicy.can_send_receiving_to_storage(
            self._state(WarehouseStateCode.STORED),
            flow_closed=True,
            role_allowed=True,
            not_created_count=1,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "state_forbidden")

    def test_can_take_processing_allows_processing_queue_states(self):
        decision = WarehouseActionPolicy.can_take_processing(
            self._state(WarehouseStateCode.IN_PROCESSING_ZONE),
            role="processing_head",
            client_view=False,
        )

        self.assertTrue(decision.allowed)

    def test_can_take_processing_blocks_active_processing(self):
        decision = WarehouseActionPolicy.can_take_processing(
            self._state(WarehouseStateCode.PROCESSING_IN_PROGRESS),
            role="processing_head",
            client_view=False,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "state_forbidden")

    def test_can_create_processing_placement_blocks_stored_goods(self):
        decision = WarehouseActionPolicy.can_create_processing_placement(
            self._state(WarehouseStateCode.STORED),
            has_items=True,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "state_forbidden")


class WarehouseCommandServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="warehouse_command_user", password="pwd")
        self.agency = Agency.objects.create(agn_name="Warehouse Command Agency")
        StockPalletState.objects.create(
            agency=self.agency,
            order_type="receiving",
            order_id="1000",
            sku="SKU-1",
            name="Товар 1",
            size="42",
            goods_type="Не обработанный",
            qty=50,
            state=StockPalletState.STATE_WAREHOUSE,
        )
        InventoryState.objects.create(
            agency=self.agency,
            order_type="processing",
            order_id="2000",
            sku="SKU-1",
            size="42",
            goods_type="Не обработанный",
            qty=50,
            state=InventoryState.STATE_PROCESSING,
        )

    def _create_processing_snapshot(self, order_id: str, state_code: str):
        location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OBR" if state_code != "stored" else "OS",
        )
        snapshot = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="legacy_stock",
            source_context_id=f"legacy-{order_id}",
            sku_code="SKU-CMD",
            name="Командный товар",
            size="42",
            barcode=f"20000000{order_id[-4:]}",
            goods_type="gv",
            qty=10,
            available_qty=0 if state_code != "stored" else 10,
            processing_reserved_qty=10 if state_code != "stored" else 0,
            container_code=f"PAL-{order_id}",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code=state_code,
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
            qty_reserved=10,
            status=WarehouseReserve.STATUS_ACTIVE,
        )
        return snapshot

    def _create_receiving_snapshot(self, order_id: str, state_code: str):
        location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="PR" if state_code != "stored" else "OS",
        )
        return WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            sku_code="SKU-RCV",
            name="Товар приемки",
            size="42",
            barcode=f"10000000{order_id[-4:]}",
            goods_type="op",
            qty=10,
            available_qty=10,
            processing_reserved_qty=0,
            shipping_reserved_qty=0,
            container_code=f"PAL-RCV-{order_id}",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code=state_code,
        )

    def test_start_receiving_flow_command_starts_for_receiving_state(self):
        order_id = "RCV-3001"
        self._create_receiving_snapshot(order_id, "received_unplaced")

        result = WarehouseCommandService.start_receiving_flow(
            order_id=order_id,
            agency=self.agency,
            role="storekeeper",
            status_payload={"status": "warehouse", "status_label": "В ожидании поставки товара"},
            flow_closed=False,
        )

        self.assertEqual(result.status, "started")
        self.assertEqual(result.state_code, WarehouseStateCode.PLACED_IN_RECEIVING)
        self.assertEqual(result.payload_update.get("status_label"), "Взята в работу")

    def test_start_receiving_flow_command_returns_already_started(self):
        order_id = "RCV-3002"
        self._create_receiving_snapshot(order_id, "received_unplaced")

        result = WarehouseCommandService.start_receiving_flow(
            order_id=order_id,
            agency=self.agency,
            role="storekeeper",
            status_payload={"status": "warehouse", "status_label": "Взята в работу"},
            flow_closed=False,
        )

        self.assertEqual(result.status, "already_in_progress")
        self.assertEqual(result.state_code, WarehouseStateCode.PLACED_IN_RECEIVING)
        self.assertEqual(result.payload_update.get("status_label"), "Взята в работу")

    def test_start_receiving_flow_command_denies_for_stored_goods(self):
        order_id = "RCV-3003"
        self._create_receiving_snapshot(order_id, "stored")

        result = WarehouseCommandService.start_receiving_flow(
            order_id=order_id,
            agency=self.agency,
            role="storekeeper",
            status_payload={"status": "warehouse", "status_label": "В ожидании поставки товара"},
            flow_closed=False,
        )

        self.assertEqual(result.status, "denied")
        self.assertEqual(result.reason, "state_forbidden")

    def test_reopen_receiving_flow_command_returns_payload_patch(self):
        order_id = "RCV-3003A"
        self._create_receiving_snapshot(order_id, "placed_in_receiving")

        result = WarehouseCommandService.reopen_receiving_flow(
            order_id=order_id,
            agency=self.agency,
            role="storekeeper",
            status_payload={"status": "warehouse", "status_label": "Размещение на складе"},
            flow_closed=True,
            flow_closed_at="2026-04-22T10:00:00+03:00",
        )

        self.assertEqual(result.status, "reopened")
        self.assertTrue(result.payload_update.get("flow_reopened"))
        self.assertTrue(result.payload_update.get("flow_reopened_at"))
        self.assertEqual(result.meta.get("order_id"), order_id)
        self.assertEqual(result.meta.get("flow_closed_at"), "2026-04-22T10:00:00+03:00")

    def test_reopen_receiving_flow_command_denies_wrong_role(self):
        order_id = "RCV-3003B"
        self._create_receiving_snapshot(order_id, "placed_in_receiving")

        result = WarehouseCommandService.reopen_receiving_flow(
            order_id=order_id,
            agency=self.agency,
            role="manager",
            status_payload={"status": "warehouse", "status_label": "Размещение на складе"},
            flow_closed=True,
        )

        self.assertEqual(result.status, "denied")
        self.assertEqual(result.reason, "role_forbidden")

    def test_send_receiving_to_storage_command_allows_putaway_creation(self):
        order_id = "RCV-3004"
        self._create_receiving_snapshot(order_id, "placed_in_receiving")

        result = WarehouseCommandService.send_receiving_to_storage(
            order_id=order_id,
            agency=self.agency,
            role="storekeeper",
            status_payload={"status": "warehouse", "status_label": "Размещение на складе"},
            flow_closed=True,
            not_created_count=1,
        )

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.state_code, WarehouseStateCode.PLACED_IN_RECEIVING)

    def test_send_receiving_to_storage_command_denies_for_stored_goods(self):
        order_id = "RCV-3005"
        self._create_receiving_snapshot(order_id, "stored")

        result = WarehouseCommandService.send_receiving_to_storage(
            order_id=order_id,
            agency=self.agency,
            role="storekeeper",
            status_payload={"status": "warehouse", "status_label": "Товар принят и размещен на складе"},
            flow_closed=True,
            not_created_count=1,
        )

        self.assertEqual(result.status, "denied")
        self.assertEqual(result.reason, "state_forbidden")

    def test_create_receiving_putaway_tasks_creates_move_and_warehouse_bridge(self):
        order_id = "RCV-3005A"
        SKU.objects.create(agency=self.agency, sku_code="SKU-BRIDGE-CMD", name="Товар моста", size="42")

        result = WarehouseCommandService.create_receiving_putaway_tasks(
            order_id=order_id,
            agency=self.agency,
            role="storekeeper",
            placement_payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {
                        "code": "BOX-BRIDGE-CMD",
                        "sealed": True,
                        "items": [
                            {
                                "sku": "SKU-BRIDGE-CMD",
                                "name": "Товар моста",
                                "size": "42",
                                "barcode": "BR-CMD-1",
                                "qty": 5,
                            }
                        ],
                    }
                ],
                "act_pallets": [
                    {
                        "code": "PAL-BRIDGE-CMD",
                        "sealed": True,
                        "boxes": ["BOX-BRIDGE-CMD"],
                        "items": [],
                        "location": {"zone": "OS", "row": 2, "section": 1, "tier": 1, "cell": 3},
                    }
                ],
            },
            requested_by=self.user,
            requested_by_name="Исполнитель склада",
            requested_by_role="storekeeper",
            latest_moves_by_pallet={},
            flow_closed=True,
            not_created_count=1,
        )

        self.assertEqual(result.status, "created")
        self.assertEqual(result.created_count, 1)
        task = MoveTask.objects.get()
        operation = WarehouseOperation.objects.get(context_type="receiving", context_id=order_id)
        warehouse_task = operation.tasks.get()
        self.assertEqual(task.pallet_code, "PAL-BRIDGE-CMD")
        self.assertEqual(operation.operation_type, WarehouseOperation.TYPE_PUTAWAY)
        self.assertEqual(warehouse_task.payload.get("legacy_move_id"), task.legacy_order_id)
        self.assertEqual((task.payload or {}).get("warehouse_operation_id"), operation.id)
        self.assertEqual((task.payload or {}).get("warehouse_operation_task_id"), warehouse_task.id)

    def test_open_receiving_placement_command_clears_contexts(self):
        order_id = "RCV-3006"
        self._create_receiving_snapshot(order_id, "placed_in_receiving")
        OperationalStockService.replace_order_placement(
            self.agency,
            "receiving",
            order_id,
            {
                "act_items": [{"sku": "SKU-RCV", "name": "Товар приемки", "size": "42", "qty": 10}],
                "act_boxes": [],
                "act_pallets": [
                    {
                        "code": "PAL-RCV-3006",
                        "items": [{"sku": "SKU-RCV", "name": "Товар приемки", "size": "42", "qty": 10}],
                        "boxes": [],
                        "sealed": True,
                        "location": {"zone": "PR"},
                    }
                ],
            },
        )

        result = WarehouseCommandService.open_receiving_placement(
            order_id=order_id,
            agency=self.agency,
            role="storekeeper",
            act_state="closed",
            signed_by_storekeeper=False,
            status_payload={"status": "warehouse", "status_label": "Размещение на складе"},
        )

        self.assertEqual(result.status, "opened")
        self.assertEqual(result.payload_update.get("act_state"), "open")
        self.assertFalse(
            WarehouseStockSnapshot.objects.filter(
                agency=self.agency,
                source_context_type="receiving",
                source_context_id=order_id,
            ).exists()
        )
        self.assertFalse(
            StockPalletState.objects.filter(
                agency=self.agency,
                order_type="receiving",
                order_id=order_id,
                state=StockPalletState.STATE_WAREHOUSE,
            ).exists()
        )

    def test_open_receiving_placement_command_returns_already_open(self):
        order_id = "RCV-3007"
        self._create_receiving_snapshot(order_id, "placed_in_receiving")

        result = WarehouseCommandService.open_receiving_placement(
            order_id=order_id,
            agency=self.agency,
            role="storekeeper",
            act_state="open",
            signed_by_storekeeper=False,
            status_payload={"status": "warehouse", "status_label": "Размещение на складе"},
        )

        self.assertEqual(result.status, "already_open")
        self.assertEqual(result.payload_update.get("act_state"), "open")

    def test_complete_receiving_flow_command_syncs_operational_and_warehouse_state(self):
        order_id = "RCV-3008"
        SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-RCV-COMP",
            name="Товар закрытия приемки",
            size="42",
        )

        result = WarehouseCommandService.complete_receiving_flow(
            order_id=order_id,
            agency=self.agency,
            status_payload={
                "status": "warehouse",
                "status_label": "Взята в работу",
                "goods_type": "op",
            },
            has_mismatch=False,
            receiving_mode="cz",
            act_items=[
                {
                    "sku_code": "SKU-RCV-COMP",
                    "name": "Товар закрытия приемки",
                    "size": "42",
                    "planned_qty": 1,
                    "actual_qty": 1,
                    "comment": "",
                }
            ],
            placement_items=[
                {
                    "sku_code": "SKU-RCV-COMP",
                    "name": "Товар закрытия приемки",
                    "size": "42",
                    "actual_qty": 1,
                    "box_qty": 1,
                    "pallet_qty": 1,
                    "comment": "",
                }
            ],
            boxes=[
                {
                    "code": "BOX-COMP-1",
                    "sealed": True,
                    "items": [
                        {
                            "sku": "SKU-RCV-COMP",
                            "name": "Товар закрытия приемки",
                            "size": "42",
                            "barcode": "CZ-COMP-1",
                            "marking_code": "CZ-3008-1",
                            "qty": 1,
                        }
                    ],
                }
            ],
            pallets=[
                {
                    "code": "PAL-COMP-1",
                    "sealed": True,
                    "boxes": ["BOX-COMP-1"],
                    "items": [],
                    "location": {"zone": "PR"},
                }
            ],
            flow_state={"boxes": [], "pallets": []},
            act_units=[
                {
                    "sku_code": "SKU-RCV-COMP",
                    "name": "Товар закрытия приемки",
                    "size": "42",
                    "barcode": "CZ-COMP-1",
                    "marking_code": "CZ-3008-1",
                    "box_code": "BOX-COMP-1",
                    "pallet_code": "PAL-COMP-1",
                }
            ],
            eta_at="2026-04-22T10:00:00+03:00",
            vehicle_number="A123AA790",
            has_closed_placement_act=False,
            performed_by=self.user,
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.state_code, WarehouseStateCode.PLACED_IN_RECEIVING)
        self.assertTrue(result.payload_update.get("flow_closed"))
        self.assertEqual(result.payload_update.get("act"), "receiving")
        self.assertEqual(result.payload_update.get("status_label"), "Товар принят")
        self.assertFalse(result.meta.get("placement_previously_closed"))

        placement_payload = result.meta.get("placement_payload") or {}
        self.assertEqual(placement_payload.get("act"), "placement")
        self.assertEqual(placement_payload.get("act_state"), "closed")
        self.assertEqual(len(placement_payload.get("act_units") or []), 1)

        stock_row = StockPalletState.objects.get(
            agency=self.agency,
            order_type="receiving",
            order_id=order_id,
            marking_code="CZ-3008-1",
        )
        self.assertEqual(stock_row.sku, "SKU-RCV-COMP")
        self.assertEqual(stock_row.qty, 1)
        self.assertEqual(stock_row.available_qty, 1)
        self.assertEqual(stock_row.box_code, "BOX-COMP-1")
        self.assertEqual(stock_row.pallet_code, "PAL-COMP-1")
        self.assertEqual(stock_row.zone, "PR")
        self.assertEqual(stock_row.location, "PR · Зона приемки")

        snapshot = WarehouseStockSnapshot.objects.get(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            marking_code="CZ-3008-1",
        )
        self.assertEqual(snapshot.sku_code, "SKU-RCV-COMP")
        self.assertEqual(snapshot.qty, 1)
        self.assertEqual(snapshot.available_qty, 1)
        self.assertEqual(snapshot.container_code, "PAL-COMP-1")
        self.assertEqual(snapshot.zone_code, "PR")
        self.assertEqual(snapshot.warehouse_state_code, "placed_in_receiving")

    def test_close_receiving_placement_command_replaces_existing_receiving_context(self):
        order_id = "RCV-3009"
        SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-RCV-CLOSE",
            name="Товар закрытия размещения",
            size="43",
        )
        self._create_receiving_snapshot(order_id, "placed_in_receiving")
        StockPalletState.objects.create(
            agency=self.agency,
            order_type="receiving",
            order_id=order_id,
            sku="SKU-OLD",
            name="Старый товар",
            size="41",
            goods_type="Оптовый",
            qty=3,
            available_qty=3,
            zone="PR",
            location="PR · Зона приемки",
            state=StockPalletState.STATE_WAREHOUSE,
        )

        result = WarehouseCommandService.close_receiving_placement(
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
                    "sku_code": "SKU-RCV-CLOSE",
                    "name": "Товар закрытия размещения",
                    "size": "43",
                    "actual_qty": 4,
                    "box_qty": 4,
                    "pallet_qty": 4,
                    "comment": "",
                }
            ],
            boxes=[
                {
                    "code": "BOX-CLOSE-1",
                    "sealed": True,
                    "items": [
                        {
                            "sku": "SKU-RCV-CLOSE",
                            "name": "Товар закрытия размещения",
                            "size": "43",
                            "barcode": "CLOSE-1",
                            "qty": 4,
                        }
                    ],
                }
            ],
            pallets=[
                {
                    "code": "PAL-CLOSE-1",
                    "sealed": True,
                    "boxes": ["BOX-CLOSE-1"],
                    "items": [],
                    "location": {"zone": "PR"},
                }
            ],
            has_closed_act=True,
            performed_by=self.user,
        )

        self.assertEqual(result.status, "closed")
        self.assertEqual(result.state_code, WarehouseStateCode.PLACED_IN_RECEIVING)
        self.assertEqual(result.payload_update.get("act"), "placement")
        self.assertEqual(result.payload_update.get("act_state"), "closed")
        self.assertEqual(result.payload_update.get("status_label"), "Товар принят и размещен на складе")
        self.assertTrue(result.meta.get("placement_previously_closed"))
        self.assertFalse(
            StockPalletState.objects.filter(
                agency=self.agency,
                order_type="receiving",
                order_id=order_id,
                sku="SKU-OLD",
            ).exists()
        )

        stock_row = StockPalletState.objects.get(
            agency=self.agency,
            order_type="receiving",
            order_id=order_id,
            sku="SKU-RCV-CLOSE",
        )
        self.assertEqual(stock_row.qty, 4)
        self.assertEqual(stock_row.available_qty, 4)
        self.assertEqual(stock_row.pallet_code, "PAL-CLOSE-1")
        self.assertEqual(stock_row.zone, "PR")

        snapshots = list(
            WarehouseStockSnapshot.objects.filter(
                agency=self.agency,
                source_context_type="receiving",
                source_context_id=order_id,
            ).order_by("id")
        )
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].sku_code, "SKU-RCV-CLOSE")
        self.assertEqual(snapshots[0].qty, 4)
        self.assertEqual(snapshots[0].container_code, "PAL-CLOSE-1")
        self.assertEqual(snapshots[0].zone_code, "PR")
        self.assertEqual(snapshots[0].warehouse_state_code, "placed_in_receiving")

    def test_take_processing_command_starts_processing(self):
        order_id = "CMD-2001"
        snapshot = self._create_processing_snapshot(order_id, "in_processing_zone")

        result = WarehouseCommandService.take_processing(
            order_id=order_id,
            agency=self.agency,
            role="processing_head",
            status_payload={"status": "processing_head", "status_label": "Передано в обработку"},
            started_by=self.user,
        )

        self.assertEqual(result.status, "started")
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "processing_in_progress")

    def test_take_processing_command_returns_already_started(self):
        order_id = "CMD-2002"
        self._create_processing_snapshot(order_id, "processing_in_progress")

        result = WarehouseCommandService.take_processing(
            order_id=order_id,
            agency=self.agency,
            role="processing_head",
            status_payload={"status": "processing_head", "status_label": "Передано в обработку"},
            started_by=self.user,
        )

        self.assertEqual(result.status, "already_in_progress")
        self.assertEqual(result.state_code, WarehouseStateCode.PROCESSING_IN_PROGRESS)

    def test_take_processing_command_denies_for_stored_goods(self):
        order_id = "CMD-2003"
        self._create_processing_snapshot(order_id, "stored")

        result = WarehouseCommandService.take_processing(
            order_id=order_id,
            agency=self.agency,
            role="processing_head",
            status_payload={"status": "processing_head", "status_label": "Передано в обработку"},
            started_by=self.user,
        )

        self.assertEqual(result.status, "denied")
        self.assertEqual(result.reason, "state_forbidden")

    def test_refresh_materialized_stock_state_updates_reserved_and_available_qty(self):
        InventoryState.objects.filter(agency=self.agency).delete()
        shipping_order = ShippingOrder.objects.create(
            number="SO-TEST-2",
            agency=self.agency,
            status=ShippingOrder.STATUS_SUBMITTED,
        )
        shipping_item = ShippingOrderItem.objects.create(
            order=shipping_order,
            sku_code="SKU-1",
            name="Товар 1",
            size="42",
            goods_type="Не обработанный",
            qty_requested=20,
        )
        ShippingReserve.objects.create(
            order=shipping_order,
            item=shipping_item,
            agency=self.agency,
            sku_code="SKU-1",
            size="42",
            goods_type="Не обработанный",
            qty=20,
        )

        refresh_materialized_stock_state_for_agency(self.agency)

        row = StockPalletState.objects.get(agency=self.agency, sku="SKU-1", size="42")
        self.assertEqual(row.qty, 50)
        self.assertEqual(row.processing_reserved_qty, 0)
        self.assertEqual(row.shipping_reserved_qty, 20)
        self.assertEqual(row.available_qty, 30)

    def test_occupied_os_cells_deduplicates_coordinates(self):
        StockPalletState.objects.create(
            agency=self.agency,
            order_type="receiving",
            order_id="3000",
            sku="SKU-2",
            name="Товар 2",
            size="43",
            qty=10,
            pallet_code="PAL-3000",
            zone="OS",
            row=1,
            section=2,
            tier=3,
            cell=4,
            location="OS · Ряд 1 · Секция 2 · Ярус 3 · Ячейка 4",
            state=StockPalletState.STATE_WAREHOUSE,
        )
        StockPalletState.objects.create(
            agency=self.agency,
            order_type="processing",
            order_id="3001",
            sku="SKU-3",
            name="Товар 3",
            size="44",
            qty=5,
            pallet_code="PAL-3001",
            zone="OS",
            row=1,
            section=2,
            tier=3,
            cell=4,
            location="OS · Ряд 1 · Секция 2 · Ярус 3 · Ячейка 4",
            state=StockPalletState.STATE_WAREHOUSE,
        )
        StockPalletState.objects.create(
            agency=self.agency,
            order_type="processing",
            order_id="3002",
            sku="SKU-4",
            name="Товар 4",
            size="45",
            qty=7,
            pallet_code="PAL-3002",
            zone="OS",
            row=1,
            section=2,
            tier=3,
            cell=5,
            location="OS · Ряд 1 · Секция 2 · Ярус 3 · Ячейка 5",
            state=StockPalletState.STATE_WAREHOUSE,
        )

        occupied_cells = StockAvailabilityService.occupied_os_cells()
        self.assertEqual(
            occupied_cells,
            [
                {"row": 1, "section": 2, "tier": 3, "cell": 4},
                {"row": 1, "section": 2, "tier": 3, "cell": 5},
            ],
        )

    def test_occupied_os_cells_excludes_current_order(self):
        StockPalletState.objects.create(
            agency=self.agency,
            order_type="processing",
            order_id="4001",
            sku="SKU-5",
            name="Товар 5",
            size="46",
            qty=9,
            pallet_code="PAL-4001",
            zone="OS",
            row=2,
            section=1,
            tier=1,
            cell=1,
            location="OS · Ряд 2 · Секция 1 · Ярус 1 · Ячейка 1",
            state=StockPalletState.STATE_WAREHOUSE,
        )

        keys = StockAvailabilityService.occupied_os_cell_keys(
            exclude_order_type="processing",
            exclude_order_id="4001",
        )
        self.assertNotIn((2, 1, 1, 1), keys)

    def test_occupied_os_cells_excludes_pallet_code(self):
        StockPalletState.objects.create(
            agency=self.agency,
            order_type="receiving",
            order_id="5001",
            sku="SKU-6",
            name="Товар 6",
            size="47",
            qty=4,
            pallet_code="PAL-KEEP",
            zone="OS",
            row=3,
            section=1,
            tier=1,
            cell=1,
            location="OS · Ряд 3 · Секция 1 · Ярус 1 · Ячейка 1",
            state=StockPalletState.STATE_WAREHOUSE,
        )
        StockPalletState.objects.create(
            agency=self.agency,
            order_type="receiving",
            order_id="5002",
            sku="SKU-7",
            name="Товар 7",
            size="48",
            qty=4,
            pallet_code="PAL-EXCLUDE",
            zone="OS",
            row=3,
            section=1,
            tier=1,
            cell=2,
            location="OS · Ряд 3 · Секция 1 · Ярус 1 · Ячейка 2",
            state=StockPalletState.STATE_WAREHOUSE,
        )

        keys = StockAvailabilityService.occupied_os_cell_keys(exclude_pallet_code="PAL-EXCLUDE")
        self.assertIn((3, 1, 1, 1), keys)
        self.assertNotIn((3, 1, 1, 2), keys)

    def test_suggest_os_cell_prefers_same_client_section(self):
        other_agency = Agency.objects.create(agn_name="Другой клиент")
        StockPalletState.objects.create(
            agency=self.agency,
            order_type="receiving",
            order_id="6001",
            sku="SKU-A",
            name="Товар A",
            size="42",
            qty=4,
            pallet_code="PAL-A",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=1,
            location="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
            state=StockPalletState.STATE_WAREHOUSE,
        )
        StockPalletState.objects.create(
            agency=other_agency,
            order_type="receiving",
            order_id="6002",
            sku="SKU-B",
            name="Товар B",
            size="42",
            qty=4,
            pallet_code="PAL-B",
            zone="OS",
            row=1,
            section=2,
            tier=1,
            cell=1,
            location="OS · Ряд 1 · Секция 2 · Ярус 1 · Ячейка 1",
            state=StockPalletState.STATE_WAREHOUSE,
        )

        suggestion = StockAvailabilityService.suggest_os_cell_for_agency(
            agency_id=self.agency.id,
            row_sections={1: 3},
            tiers=1,
            cells_per_tier=2,
            occupied_keys=StockAvailabilityService.occupied_os_cell_keys(),
            section_agencies=StockAvailabilityService.occupied_os_section_agencies(),
        )

        self.assertEqual(
            suggestion,
            {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 2},
        )

    def test_suggest_os_cell_prefers_empty_section_before_other_client_section(self):
        other_agency = Agency.objects.create(agn_name="Клиент B")
        StockPalletState.objects.create(
            agency=other_agency,
            order_type="receiving",
            order_id="7001",
            sku="SKU-C",
            name="Товар C",
            size="42",
            qty=4,
            pallet_code="PAL-C",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=1,
            location="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
            state=StockPalletState.STATE_WAREHOUSE,
        )
        StockPalletState.objects.create(
            agency=other_agency,
            order_type="receiving",
            order_id="7002",
            sku="SKU-D",
            name="Товар D",
            size="42",
            qty=4,
            pallet_code="PAL-D",
            zone="OS",
            row=1,
            section=3,
            tier=1,
            cell=1,
            location="OS · Ряд 1 · Секция 3 · Ярус 1 · Ячейка 1",
            state=StockPalletState.STATE_WAREHOUSE,
        )

        suggestion = StockAvailabilityService.suggest_os_cell_for_agency(
            agency_id=self.agency.id,
            row_sections={1: 3},
            tiers=1,
            cells_per_tier=2,
            occupied_keys=StockAvailabilityService.occupied_os_cell_keys(),
            section_agencies=StockAvailabilityService.occupied_os_section_agencies(),
        )

        self.assertEqual(
            suggestion,
            {"zone": "OS", "row": 1, "section": 2, "tier": 1, "cell": 1},
        )


class ClientInventoryJournalReserveColumnsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="client_inventory_user",
            password="x",
        )
        self.agency = Agency.objects.create(
            agn_name="Индивидуальный предприниматель Тестов Тест",
            portal_user=self.user,
        )
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-1",
            name="Джинсы",
            size="42",
        )
        self.client.force_login(self.user)

    def test_client_inventory_journal_shows_separate_obr_and_otg_reserves(self):
        StockPalletState.objects.create(
            agency=self.agency,
            order_type="receiving",
            order_id="1001",
            sku="SKU-1",
            name="Джинсы",
            size="42",
            goods_type="Оптовый",
            qty=10,
            state=StockPalletState.STATE_WAREHOUSE,
        )
        InventoryState.objects.create(
            agency=self.agency,
            order_type="processing",
            order_id="2001",
            sku="SKU-1",
            size="42",
            goods_type="Оптовый",
            qty=2,
            state=InventoryState.STATE_PROCESSING,
        )
        submitted_order = ShippingOrder.objects.create(
            number="SO-000001",
            agency=self.agency,
            status=ShippingOrder.STATUS_SUBMITTED,
        )
        submitted_item = ShippingOrderItem.objects.create(
            order=submitted_order,
            sku=self.sku,
            sku_code="SKU-1",
            name="Джинсы",
            size="42",
            goods_type="Оптовый",
            qty_requested=3,
            qty_reserved=3,
        )
        ShippingReserve.objects.create(
            order=submitted_order,
            item=submitted_item,
            agency=self.agency,
            sku_code="SKU-1",
            size="42",
            goods_type="Оптовый",
            qty=3,
        )
        canceled_order = ShippingOrder.objects.create(
            number="SO-000002",
            agency=self.agency,
            status=ShippingOrder.STATUS_CANCELED,
        )
        canceled_item = ShippingOrderItem.objects.create(
            order=canceled_order,
            sku=self.sku,
            sku_code="SKU-1",
            name="Джинсы",
            size="42",
            goods_type="Оптовый",
            qty_requested=9,
            qty_reserved=9,
        )
        ShippingReserve.objects.create(
            order=canceled_order,
            item=canceled_item,
            agency=self.agency,
            sku_code="SKU-1",
            size="42",
            goods_type="Оптовый",
            qty=9,
        )

        response = self.client.get("/sklad/journal/")

        self.assertEqual(response.status_code, 200)
        rows = response.context["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["qty"], 10)
        self.assertEqual(rows[0]["processing_reserved_qty"], 2)
        self.assertEqual(rows[0]["shipping_reserved_qty"], 3)
        self.assertEqual(rows[0]["available_qty"], 5)
        self.assertContains(response, "Резерв OBR")
        self.assertContains(response, "Резерв OTG")
        self.assertContains(response, "Доступный остаток")

    def test_client_inventory_journal_uses_warehouse_processing_reserve_when_present(self):
        StockPalletState.objects.create(
            agency=self.agency,
            order_type="receiving",
            order_id="1002",
            sku="SKU-1",
            name="Джинсы",
            size="42",
            goods_type="Оптовый",
            qty=10,
            state=StockPalletState.STATE_WAREHOUSE,
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="2002",
            sku_code="SKU-1",
            size="42",
            goods_type="Оптовый",
            qty_reserved=4,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        response = self.client.get("/sklad/journal/")

        self.assertEqual(response.status_code, 200)
        rows = response.context["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["qty"], 10)
        self.assertEqual(rows[0]["processing_reserved_qty"], 4)
        self.assertEqual(rows[0]["available_qty"], 6)

    def test_client_inventory_journal_uses_warehouse_snapshot_rows_when_legacy_rows_missing(self):
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=3,
            section_no=2,
            tier_no=1,
            cell_no=1,
            display_name="OS · Ряд 3 · Секция 2 · Ярус 1 · Ячейка 1",
        )
        pallet = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="PAL-JRN-1",
            current_location=location,
        )
        box = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code="BOX-JRN-1",
            parent_container=pallet,
            current_location=location,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="R-JRN-1",
            sku_code="SKU-3",
            name="Худи",
            size="46",
            goods_type="Готовый",
            qty=7,
            available_qty=7,
            container=box,
            container_code=box.container_code,
            parent_container=pallet,
            location=location,
            zone_code="OS",
            zone_kind=location.zone_kind,
            warehouse_state_code=WarehouseStateCode.STORED.value,
        )

        response = self.client.get("/sklad/journal/")

        self.assertEqual(response.status_code, 200)
        rows = response.context["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sku"], "SKU-3")
        self.assertEqual(rows[0]["qty"], 7)
        self.assertEqual(rows[0]["pallet_code"], "PAL-JRN-1")

    def test_client_inventory_journal_shows_reserved_only_row(self):
        submitted_order = ShippingOrder.objects.create(
            number="SO-000003",
            agency=self.agency,
            status=ShippingOrder.STATUS_SUBMITTED,
        )
        submitted_item = ShippingOrderItem.objects.create(
            order=submitted_order,
            sku_code="SKU-2",
            name="Футболка",
            size="44",
            goods_type="Готовый",
            qty_requested=5,
            qty_reserved=5,
        )
        ShippingReserve.objects.create(
            order=submitted_order,
            item=submitted_item,
            agency=self.agency,
            sku_code="SKU-2",
            size="44",
            goods_type="Готовый",
            qty=5,
        )

        response = self.client.get("/sklad/journal/")

        self.assertEqual(response.status_code, 200)
        rows = response.context["rows"]
        reserve_only_row = next(row for row in rows if row["sku"] == "SKU-2")
        self.assertEqual(reserve_only_row["qty"], 0)
        self.assertEqual(reserve_only_row["processing_reserved_qty"], 0)
        self.assertEqual(reserve_only_row["shipping_reserved_qty"], 5)
        self.assertEqual(reserve_only_row["available_qty"], 0)

    def test_client_inventory_journal_shows_goods_already_in_processing(self):
        OrderAuditEntry.objects.create(
            order_id="2002",
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "stock_rows": [
                    {
                        "sku": "SKU-1",
                        "article": "SKU-1",
                        "size": "42",
                        "goods_type": "Оптовый",
                        "qty": 10,
                    }
                ],
            },
        )
        StockPalletState.objects.create(
            agency=self.agency,
            order_type="receiving",
            order_id="1002",
            sku="SKU-1",
            name="Джинсы",
            size="42",
            goods_type="Оптовый",
            qty=2,
            state=StockPalletState.STATE_WAREHOUSE,
        )
        InventoryState.objects.create(
            agency=self.agency,
            order_type="processing",
            order_id="2002",
            sku="SKU-1",
            size="42",
            goods_type="Оптовый",
            qty=2,
            state=InventoryState.STATE_PROCESSING,
        )

        response = self.client.get("/sklad/journal/")

        self.assertEqual(response.status_code, 200)
        rows = response.context["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["qty"], 2)
        self.assertEqual(rows[0]["processing_reserved_qty"], 2)
        self.assertEqual(rows[0]["processing_in_progress_qty"], 8)
        self.assertEqual(rows[0]["available_qty"], 0)
        self.assertContains(response, "Товар в обработке")
        self.assertContains(response, ">8<", html=False)

    def test_client_inventory_journal_uses_warehouse_in_progress_qty_without_double_counting_reserve(self):
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OBR")
        operation = WarehouseOperation.objects.create(
            agency=self.agency,
            operation_type=WarehouseOperation.TYPE_PROCESSING,
            context_type="processing",
            context_id="2003",
            source_location=location,
            destination_location=location,
            source_zone_code=location.zone_code,
            destination_zone_code=location.zone_code,
            status=WarehouseOperation.STATUS_IN_PROGRESS,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="legacy_stock",
            source_context_id="legacy-2003",
            sku_code="SKU-1",
            name="Джинсы",
            size="42",
            goods_type="Оптовый",
            qty=8,
            available_qty=0,
            processing_reserved_qty=8,
            container_code="PAL-PROC-1",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code=WarehouseStateCode.PROCESSING_IN_PROGRESS.value,
            active_operation=operation,
            active_operation_type=WarehouseOperation.TYPE_PROCESSING,
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="2003",
            sku_code="SKU-1",
            size="42",
            goods_type="Оптовый",
            qty_reserved=8,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        response = self.client.get("/sklad/journal/")

        self.assertEqual(response.status_code, 200)
        rows = response.context["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sku"], "SKU-1")
        self.assertEqual(rows[0]["qty"], 0)
        self.assertEqual(rows[0]["processing_reserved_qty"], 0)
        self.assertEqual(rows[0]["processing_in_progress_qty"], 8)
        self.assertEqual(rows[0]["available_qty"], 0)
        self.assertContains(response, "Товар в обработке")

    def test_client_inventory_journal_uses_warehouse_processing_zone_qty_as_processing_reserve(self):
        location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OBR")
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="legacy_stock",
            source_context_id="legacy-2004",
            sku_code="SKU-1",
            name="Джинсы",
            size="42",
            goods_type="Оптовый",
            qty=5,
            available_qty=0,
            processing_reserved_qty=5,
            container_code="PAL-PROC-2",
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code=WarehouseStateCode.IN_PROCESSING_ZONE.value,
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="2004",
            sku_code="SKU-1",
            size="42",
            goods_type="Оптовый",
            qty_reserved=5,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        response = self.client.get("/sklad/journal/")

        self.assertEqual(response.status_code, 200)
        rows = response.context["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sku"], "SKU-1")
        self.assertEqual(rows[0]["qty"], 0)
        self.assertEqual(rows[0]["processing_reserved_qty"], 5)
        self.assertEqual(rows[0]["processing_in_progress_qty"], 0)
        self.assertEqual(rows[0]["available_qty"], 0)


class StaffInventoryJournalAvailableQtyTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="staff_inventory_user",
            password="x",
            is_staff=True,
        )
        self.agency_a = Agency.objects.create(agn_name="Клиент А")
        self.agency_b = Agency.objects.create(agn_name="Клиент Б")
        self.client.force_login(self.user)

    def test_staff_inventory_journal_uses_agency_specific_available_qty(self):
        StockPalletState.objects.create(
            agency=self.agency_a,
            order_type="receiving",
            order_id="A-1",
            sku="SKU-X",
            name="Товар X",
            size="42",
            goods_type="Оптовый",
            qty=4,
            box_code="BOX-A1",
            pallet_code="PAL-A1",
            zone="OS",
            location="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
            state=StockPalletState.STATE_WAREHOUSE,
        )
        StockPalletState.objects.create(
            agency=self.agency_a,
            order_type="receiving",
            order_id="A-2",
            sku="SKU-X",
            name="Товар X",
            size="42",
            goods_type="Оптовый",
            qty=6,
            box_code="BOX-A2",
            pallet_code="PAL-A2",
            zone="OS",
            location="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 2",
            state=StockPalletState.STATE_WAREHOUSE,
        )
        StockPalletState.objects.create(
            agency=self.agency_b,
            order_type="receiving",
            order_id="B-1",
            sku="SKU-X",
            name="Товар X",
            size="42",
            goods_type="Оптовый",
            qty=5,
            box_code="BOX-B1",
            pallet_code="PAL-B1",
            zone="OS",
            location="OS · Ряд 2 · Секция 1 · Ярус 1 · Ячейка 1",
            state=StockPalletState.STATE_WAREHOUSE,
        )
        InventoryState.objects.create(
            agency=self.agency_a,
            order_type="processing",
            order_id="P-A",
            sku="SKU-X",
            size="42",
            goods_type="Оптовый",
            qty=3,
            state=InventoryState.STATE_PROCESSING,
        )
        order_b = ShippingOrder.objects.create(
            number="SO-009999",
            agency=self.agency_b,
            status=ShippingOrder.STATUS_SUBMITTED,
        )
        item_b = ShippingOrderItem.objects.create(
            order=order_b,
            sku_code="SKU-X",
            name="Товар X",
            size="42",
            goods_type="Оптовый",
            qty_requested=2,
            qty_reserved=2,
        )
        ShippingReserve.objects.create(
            order=order_b,
            item=item_b,
            agency=self.agency_b,
            sku_code="SKU-X",
            size="42",
            goods_type="Оптовый",
            qty=2,
        )

        response = self.client.get("/sklad/journal/")

        self.assertEqual(response.status_code, 200)
        rows = response.context["rows"]
        agency_a_rows = [row for row in rows if row["agency_id"] == self.agency_a.id]
        agency_b_rows = [row for row in rows if row["agency_id"] == self.agency_b.id]
        self.assertEqual(sum(row["available_qty"] for row in agency_a_rows), 7)
        self.assertEqual(sum(row["available_qty"] for row in agency_b_rows), 3)


class InventoryJournalUiServiceTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        user_model = get_user_model()
        self.client_user = user_model.objects.create_user(
            username="inventory_service_client",
            password="x",
        )
        self.staff_user = user_model.objects.create_user(
            username="inventory_service_staff",
            password="x",
            is_staff=True,
        )
        self.client_agency = Agency.objects.create(
            agn_name="Клиент service journal",
            portal_user=self.client_user,
        )
        self.other_agency = Agency.objects.create(agn_name="Другой клиент service journal")

    def test_build_inventory_journal_page_for_client_uses_client_template_and_filters_by_portal_user(self):
        StockPalletState.objects.create(
            agency=self.client_agency,
            order_type="receiving",
            order_id="R-SVC-1",
            sku="SKU-SVC",
            name="Товар клиента",
            size="42",
            goods_type="Оптовый",
            qty=4,
            state=StockPalletState.STATE_WAREHOUSE,
        )
        StockPalletState.objects.create(
            agency=self.other_agency,
            order_type="receiving",
            order_id="R-SVC-2",
            sku="SKU-OTHER",
            name="Чужой товар",
            size="43",
            goods_type="Оптовый",
            qty=9,
            state=StockPalletState.STATE_WAREHOUSE,
        )
        request = self.factory.get("/sklad/journal/")
        request.user = self.client_user

        page = build_inventory_journal_page(request=request)

        self.assertEqual(page["template_name"], "client_cabinet/inventory_journal.html")
        self.assertEqual(page["context"]["client_agency"], self.client_agency)
        rows = page["context"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sku"], "SKU-SVC")

    def test_build_inventory_journal_page_for_staff_respects_agency_filter(self):
        StockPalletState.objects.create(
            agency=self.client_agency,
            order_type="receiving",
            order_id="R-SVC-3",
            sku="SKU-A",
            name="Товар А",
            size="42",
            goods_type="Оптовый",
            qty=5,
            state=StockPalletState.STATE_WAREHOUSE,
        )
        StockPalletState.objects.create(
            agency=self.other_agency,
            order_type="receiving",
            order_id="R-SVC-4",
            sku="SKU-B",
            name="Товар Б",
            size="44",
            goods_type="Оптовый",
            qty=7,
            state=StockPalletState.STATE_WAREHOUSE,
        )
        request = self.factory.get("/sklad/journal/", {"client": str(self.other_agency.id)})
        request.user = self.staff_user

        page = build_inventory_journal_page(request=request)

        self.assertEqual(page["template_name"], "sklad/inventory_journal.html")
        self.assertEqual(page["context"]["client_agency"], self.other_agency)
        rows = page["context"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["agency_id"], self.other_agency.id)


class StockSnapshotBarcodeRecoveryTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Snapshot Agency")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-BC-1",
            name="Товар для штрихкода",
            size="42",
        )
        SKUBarcode.objects.create(
            sku=self.sku,
            value="2000999000001",
            size="42",
            is_primary=True,
        )

    def test_rebuild_stock_snapshot_restores_missing_barcode_from_nomenclature(self):
        OrderAuditEntry.objects.create(
            order_id="R-100",
            order_type="receiving",
            action="status",
            agency=self.agency,
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_items": [
                    {
                        "sku": "SKU-BC-1",
                        "name": "Товар для штрихкода",
                        "size": "42",
                        "barcode": "",
                        "qty": 10,
                    }
                ],
            },
        )

        rebuild_stock_snapshot_for_agency(self.agency)
        stock_row = StockPalletState.objects.get(agency=self.agency, sku="SKU-BC-1", size="42")
        self.assertEqual(stock_row.barcode, "2000999000001")

    def test_rebuild_stock_snapshot_keeps_explicit_payload_barcode(self):
        OrderAuditEntry.objects.create(
            order_id="R-101",
            order_type="receiving",
            action="status",
            agency=self.agency,
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_items": [
                    {
                        "sku": "SKU-BC-1",
                        "name": "Товар для штрихкода",
                        "size": "42",
                        "barcode": "5550001112223",
                        "qty": 5,
                    }
                ],
            },
        )

        rebuild_stock_snapshot_for_agency(self.agency)
        stock_row = StockPalletState.objects.get(agency=self.agency, sku="SKU-BC-1", size="42")
        self.assertEqual(stock_row.barcode, "5550001112223")


class StockSnapshotMarkedUnitsTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Marked Snapshot Agency")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-CZ-2",
            name="Маркируемый товар",
            size="43",
            honest_sign=True,
        )

    def test_rebuild_stock_snapshot_creates_separate_rows_per_marking_code(self):
        OrderAuditEntry.objects.create(
            order_id="R-CZ-200",
            order_type="receiving",
            action="status",
            agency=self.agency,
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {"code": "BOX-1", "items": []},
                ],
                "act_pallets": [
                    {
                        "code": "PAL-1",
                        "boxes": ["BOX-1"],
                        "items": [],
                        "location": {"zone": "PR"},
                    }
                ],
                "act_units": [
                    {
                        "sku_code": "SKU-CZ-2",
                        "name": "Маркируемый товар",
                        "size": "43",
                        "barcode": "2200000000011",
                        "marking_code": "CZ-200-1",
                        "box_code": "BOX-1",
                        "pallet_code": "PAL-1",
                    },
                    {
                        "sku_code": "SKU-CZ-2",
                        "name": "Маркируемый товар",
                        "size": "43",
                        "barcode": "2200000000011",
                        "marking_code": "CZ-200-2",
                        "box_code": "BOX-1",
                        "pallet_code": "PAL-1",
                    },
                ],
            },
        )

        rebuild_stock_snapshot_for_agency(self.agency)

        rows = list(
            StockPalletState.objects.filter(agency=self.agency, sku="SKU-CZ-2", size="43").order_by("marking_code")
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual([row.qty for row in rows], [1, 1])
        self.assertEqual([row.marking_code for row in rows], ["CZ-200-1", "CZ-200-2"])


class OperationalStockReadApiTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Operational Stock Read API")
        StockPalletState.objects.create(
            agency=self.agency,
            order_type="receiving",
            order_id="R-API-1",
            sku="SKU-READ-1",
            name="Товар 1",
            size="42",
            barcode="2000000011111",
            goods_type="gv",
            qty=5,
            box_code="BOX-READ-1",
            pallet_code="PAL-READ-1",
            zone="OS",
            row=2,
            section=1,
            tier=1,
            cell=3,
            location="OS · Ряд 2 · Секция 1 · Ярус 1 · Ячейка 3",
            state=StockPalletState.STATE_WAREHOUSE,
        )
        StockPalletState.objects.create(
            agency=self.agency,
            order_type="receiving",
            order_id="R-API-1",
            sku="SKU-READ-1",
            name="Товар 1",
            size="42",
            barcode="2000000011111",
            marking_code="CZ-READ-1",
            goods_type="gv",
            qty=1,
            box_code="BOX-READ-1",
            pallet_code="PAL-READ-1",
            zone="OS",
            row=2,
            section=1,
            tier=1,
            cell=3,
            location="OS · Ряд 2 · Секция 1 · Ярус 1 · Ячейка 3",
            state=StockPalletState.STATE_WAREHOUSE,
        )
        StockPalletState.objects.create(
            agency=self.agency,
            order_type="receiving",
            order_id="R-API-1",
            sku="SKU-READ-2",
            name="Товар 2",
            size="43",
            barcode="2000000012222",
            goods_type="op",
            qty=7,
            box_code="BOX-READ-2",
            pallet_code="PAL-READ-1",
            zone="OS",
            row=2,
            section=1,
            tier=1,
            cell=3,
            location="OS · Ряд 2 · Секция 1 · Ярус 1 · Ячейка 3",
            state=StockPalletState.STATE_WAREHOUSE,
        )

    def test_get_pallet_boxes_returns_grouped_box_structure(self):
        boxes = OperationalStockService.get_pallet_boxes("PAL-READ-1", agency_id=self.agency.id)

        self.assertEqual([box["code"] for box in boxes], ["BOX-READ-1", "BOX-READ-2"])
        self.assertEqual(boxes[0]["qty"], 6)
        self.assertEqual(boxes[0]["barcode_qty"], {"2000000011111": 6})
        self.assertEqual(boxes[0]["marked_units"][0]["marking_code"], "CZ-READ-1")

    def test_get_box_items_returns_atomic_rows_from_operational_stock(self):
        items = OperationalStockService.get_box_items(
            "BOX-READ-1",
            agency_id=self.agency.id,
            pallet_code="PAL-READ-1",
        )

        self.assertEqual(len(items), 2)
        self.assertEqual(sum(int(item.get("qty") or 0) for item in items), 6)
        self.assertEqual(
            [item.get("marking_code") for item in items if item.get("marking_code")],
            ["CZ-READ-1"],
        )

    def test_get_marked_units_returns_unit_to_box_pallet_location_mapping(self):
        units = OperationalStockService.get_marked_units(
            agency_id=self.agency.id,
            pallet_code="PAL-READ-1",
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["marking_code"], "CZ-READ-1")
        self.assertEqual(units[0]["box_code"], "BOX-READ-1")
        self.assertEqual(units[0]["pallet_code"], "PAL-READ-1")
        self.assertEqual((units[0]["location"] or {}).get("zone"), "OS")


class WarehouseCoreModelsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="warehouse_core", password="pwd")
        self.agency = Agency.objects.create(agn_name="Тестовый клиент ядра")
        self.sku = SKU.objects.create(agency=self.agency, sku_code="CORE-001", name="Ядро SKU")

    def test_can_create_first_wave_warehouse_core_entities(self):
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OS",
            zone_kind=WarehouseLocation.ZONE_KIND_STORAGE,
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="OS-1-1-1-1",
            display_name="OS · Ряд 1 · Секция 1 · Ярус 1 · Ячейка 1",
            is_active=True,
            is_pickable=True,
            is_storage=True,
        )
        container = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_PALLET,
            container_code="PL-CORE-1",
            current_location=location,
            created_by=self.user,
        )
        reserve = WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id="SO-CORE-1",
            sku_ref=self.sku,
            sku_code=self.sku.sku_code,
            qty_reserved=10,
            status=WarehouseReserve.STATUS_ACTIVE,
            created_by=self.user,
        )
        operation = WarehouseOperation.objects.create(
            agency=self.agency,
            operation_type=WarehouseOperation.TYPE_MOVE_TO_OTG,
            context_type="shipping",
            context_id="SO-CORE-1",
            reserve=reserve,
            source_location=location,
            destination_zone_code="OTG",
            status=WarehouseOperation.STATUS_CREATED,
            requested_by=self.user,
            requested_by_role="storekeeper",
            assigned_executor_role="reachtruck",
            planned_qty=10,
        )
        task = WarehouseOperationTask.objects.create(
            operation=operation,
            task_type=WarehouseOperationTask.TYPE_PALLET_MOVE,
            container=container,
            from_location=location,
            from_zone_code="OS",
            to_zone_code="OTG",
            qty_planned=10,
            status=WarehouseOperationTask.STATUS_CREATED,
            assigned_to=self.user,
            assigned_to_name="Исполнитель склада",
            executor_role="reachtruck",
        )
        event = WarehouseEvent.objects.create(
            agency=self.agency,
            event_type="shipping_reserved",
            stock_context_type="shipping",
            stock_context_id="SO-CORE-1",
            container=container,
            reserve=reserve,
            operation=operation,
            operation_task=task,
            from_location=location,
            from_zone_code="OS",
            qty=10,
            performed_by=self.user,
            performed_by_role="storekeeper",
            occurred_at=timezone.now(),
        )
        snapshot = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="shipping",
            source_context_id="SO-CORE-1",
            sku_ref=self.sku,
            sku_code=self.sku.sku_code,
            name=self.sku.name,
            qty=10,
            available_qty=0,
            shipping_reserved_qty=10,
            container=container,
            container_code=container.container_code,
            location=location,
            zone_code=location.zone_code,
            zone_kind=location.zone_kind,
            warehouse_state_code="reserved_for_shipping",
            active_operation=operation,
            active_operation_type=operation.operation_type,
            last_event=event,
        )

        self.assertEqual(snapshot.container_id, container.id)
        self.assertEqual(snapshot.location_id, location.id)
        self.assertEqual(snapshot.active_operation_id, operation.id)
        self.assertEqual(snapshot.last_event_id, event.id)
        self.assertEqual(operation.reserve_id, reserve.id)
        self.assertEqual(task.operation_id, operation.id)

    def test_receiving_write_path_moves_snapshot_from_placement_to_stored(self):
        placement = WarehouseWritePathService.create_receiving_placement(
            agency=self.agency,
            order_id="RCV-CORE-1",
            performed_by=self.user,
            items=[
                {
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "size": "42",
                    "barcode": "2000000099999",
                    "goods_type": "gv",
                    "qty": 12,
                    "pallet_code": "PAL-RCV-1",
                }
            ],
        )

        self.assertEqual(len(placement.snapshot_ids), 1)
        snapshot = WarehouseStockSnapshot.objects.get(id=placement.snapshot_ids[0])
        self.assertEqual(snapshot.warehouse_state_code, "placed_in_receiving")
        self.assertEqual(snapshot.zone_code, "PR")
        self.assertEqual(snapshot.available_qty, 12)

        operation = WarehouseWritePathService.request_putaway_for_receiving(
            agency=self.agency,
            order_id="RCV-CORE-1",
            destination_zone_code="OS",
            destination_row_no=3,
            destination_section_no=2,
            destination_tier_no=1,
            destination_cell_no=4,
            requested_by=self.user,
            requested_by_role="storekeeper",
        )

        snapshot.refresh_from_db()
        self.assertEqual(snapshot.active_operation_id, operation.id)
        self.assertEqual(snapshot.active_operation_type, WarehouseOperation.TYPE_PUTAWAY)
        self.assertEqual(operation.status, WarehouseOperation.STATUS_PLANNED)
        self.assertEqual(operation.tasks.count(), 1)

        WarehouseWritePathService.complete_putaway_operation(
            operation=operation,
            performed_by=self.user,
        )

        snapshot.refresh_from_db()
        operation.refresh_from_db()
        task = operation.tasks.get()

        self.assertEqual(snapshot.warehouse_state_code, "stored")
        self.assertEqual(snapshot.zone_code, "OS")
        self.assertEqual(snapshot.active_operation_id, None)
        self.assertEqual(snapshot.location.row_no, 3)
        self.assertEqual(snapshot.location.section_no, 2)
        self.assertEqual(snapshot.location.tier_no, 1)
        self.assertEqual(snapshot.location.cell_no, 4)
        self.assertEqual(operation.status, WarehouseOperation.STATUS_DONE)
        self.assertEqual(operation.done_qty, 12)
        self.assertEqual(task.status, WarehouseOperationTask.STATUS_DONE)
        self.assertEqual(task.qty_done, 12)
        self.assertTrue(
            WarehouseEvent.objects.filter(
                stock_context_type="receiving",
                stock_context_id="RCV-CORE-1",
                event_type="putaway_completed",
            ).exists()
        )

    def test_processing_write_path_moves_snapshot_from_reserved_to_processed(self):
        placement = WarehouseWritePathService.create_receiving_placement(
            agency=self.agency,
            order_id="RCV-PRC-1",
            performed_by=self.user,
            items=[
                {
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "size": "44",
                    "barcode": "2000000088888",
                    "goods_type": "gv",
                    "qty": 8,
                    "pallet_code": "PAL-PRC-1",
                }
            ],
        )
        putaway = WarehouseWritePathService.request_putaway_for_receiving(
            agency=self.agency,
            order_id="RCV-PRC-1",
            destination_zone_code="OS",
            destination_row_no=5,
            destination_section_no=1,
            destination_tier_no=2,
            destination_cell_no=1,
            requested_by=self.user,
        )
        WarehouseWritePathService.complete_putaway_operation(operation=putaway, performed_by=self.user)

        snapshot = WarehouseStockSnapshot.objects.get(id=placement.snapshot_ids[0])
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "stored")
        self.assertEqual(snapshot.available_qty, 8)

        reserves = WarehouseWritePathService.reserve_for_processing(
            agency=self.agency,
            order_id="PROC-1",
            created_by=self.user,
            items=[
                {
                    "sku_code": self.sku.sku_code,
                    "size": "44",
                    "barcode": "2000000088888",
                    "goods_type": "gv",
                    "qty": 8,
                }
            ],
        )
        self.assertEqual(len(reserves), 1)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "reserved_for_processing")
        self.assertEqual(snapshot.processing_reserved_qty, 8)
        self.assertEqual(snapshot.available_qty, 0)

        move_op = WarehouseWritePathService.request_move_to_processing(
            agency=self.agency,
            order_id="PROC-1",
            destination_row_no=1,
            destination_section_no=1,
            destination_tier_no=1,
            destination_cell_no=1,
            requested_by=self.user,
        )
        WarehouseWritePathService.start_move_to_processing(operation=move_op, performed_by=self.user)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "moving_to_processing")

        WarehouseWritePathService.complete_move_to_processing(operation=move_op, performed_by=self.user)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "in_processing_zone")
        self.assertEqual(snapshot.zone_code, "OBR")

        processing_op = WarehouseWritePathService.start_processing(
            agency=self.agency,
            order_id="PROC-1",
            started_by=self.user,
        )
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "processing_in_progress")
        self.assertEqual(snapshot.active_operation_id, processing_op.id)
        self.assertEqual(snapshot.active_operation_type, WarehouseOperation.TYPE_PROCESSING)

        WarehouseWritePathService.complete_processing(operation=processing_op, performed_by=self.user)
        snapshot.refresh_from_db()
        reserve = WarehouseReserve.objects.get(id=reserves[0].id)
        processing_op.refresh_from_db()

        self.assertEqual(snapshot.warehouse_state_code, "placed_after_processing")
        self.assertEqual(snapshot.processing_reserved_qty, 0)
        self.assertEqual(snapshot.available_qty, 8)
        self.assertEqual(snapshot.active_operation_id, None)
        self.assertEqual(snapshot.zone_code, "OBR")
        self.assertEqual(reserve.status, WarehouseReserve.STATUS_SATISFIED)
        self.assertEqual(reserve.qty_satisfied, 8)
        self.assertEqual(processing_op.status, WarehouseOperation.STATUS_DONE)
        self.assertTrue(
            WarehouseEvent.objects.filter(
                stock_context_type="processing",
                stock_context_id="PROC-1",
                event_type="processing_completed",
            ).exists()
        )

    def test_shipping_write_path_moves_snapshot_from_reserved_to_shipped(self):
        placement = WarehouseWritePathService.create_receiving_placement(
            agency=self.agency,
            order_id="RCV-SHP-1",
            performed_by=self.user,
            items=[
                {
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "size": "46",
                    "barcode": "2000000077777",
                    "goods_type": "gv",
                    "qty": 6,
                    "pallet_code": "PAL-SHP-1",
                }
            ],
        )
        putaway = WarehouseWritePathService.request_putaway_for_receiving(
            agency=self.agency,
            order_id="RCV-SHP-1",
            destination_zone_code="OS",
            destination_row_no=6,
            destination_section_no=1,
            destination_tier_no=1,
            destination_cell_no=2,
            requested_by=self.user,
        )
        WarehouseWritePathService.complete_putaway_operation(operation=putaway, performed_by=self.user)

        snapshot = WarehouseStockSnapshot.objects.get(id=placement.snapshot_ids[0])
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "stored")

        reserves = WarehouseWritePathService.reserve_for_shipping(
            agency=self.agency,
            order_id="SHIP-1",
            created_by=self.user,
            items=[
                {
                    "sku_code": self.sku.sku_code,
                    "size": "46",
                    "barcode": "2000000077777",
                    "goods_type": "gv",
                    "qty": 6,
                }
            ],
        )
        self.assertEqual(len(reserves), 1)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "reserved_for_shipping")
        self.assertEqual(snapshot.shipping_reserved_qty, 6)
        self.assertEqual(snapshot.available_qty, 0)

        move_op = WarehouseWritePathService.request_move_to_otg(
            agency=self.agency,
            order_id="SHIP-1",
            destination_row_no=1,
            destination_section_no=1,
            destination_tier_no=1,
            destination_cell_no=1,
            requested_by=self.user,
        )
        WarehouseWritePathService.start_move_to_otg(operation=move_op, performed_by=self.user)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "moving_to_otg")

        WarehouseWritePathService.complete_move_to_otg(operation=move_op, performed_by=self.user)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "in_otg")
        self.assertEqual(snapshot.zone_code, "OTG")

        palletization = WarehouseWritePathService.start_palletization(
            agency=self.agency,
            order_id="SHIP-1",
            started_by=self.user,
        )
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "palletizing")

        WarehouseWritePathService.complete_palletization(operation=palletization, performed_by=self.user)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "ready_for_loading")

        WarehouseWritePathService.assign_to_trip(
            agency=self.agency,
            order_id="SHIP-1",
            trip_id="TRIP-1",
            assigned_by=self.user,
        )
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "assigned_to_trip")
        self.assertEqual(snapshot.current_trip_id, "TRIP-1")

        loading = WarehouseWritePathService.start_loading(
            agency=self.agency,
            order_id="SHIP-1",
            trip_id="TRIP-1",
            started_by=self.user,
        )
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "loading_in_progress")
        self.assertEqual(snapshot.active_operation_id, loading.id)

        WarehouseWritePathService.complete_loading(operation=loading, performed_by=self.user)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.warehouse_state_code, "loaded_to_vehicle")
        self.assertTrue(snapshot.is_in_vehicle)

        WarehouseWritePathService.ship_order(
            agency=self.agency,
            order_id="SHIP-1",
            trip_id="TRIP-1",
            performed_by=self.user,
        )
        snapshot.refresh_from_db()
        reserve = WarehouseReserve.objects.get(id=reserves[0].id)

        self.assertEqual(snapshot.warehouse_state_code, "shipped")
        self.assertEqual(snapshot.shipping_reserved_qty, 0)
        self.assertEqual(snapshot.available_qty, 0)
        self.assertTrue(snapshot.is_archived)
        self.assertEqual(reserve.status, WarehouseReserve.STATUS_SATISFIED)
        self.assertEqual(reserve.qty_satisfied, 6)
        self.assertTrue(
            WarehouseEvent.objects.filter(
                stock_context_type="shipping",
                stock_context_id="SHIP-1",
                event_type="shipped",
            ).exists()
        )


class WarehouseTransitionServiceTests(SimpleTestCase):
    def test_receiving_flow_reaches_stored(self):
        result = WarehouseTransitionService.apply_event(
            WarehouseStateCode.UNKNOWN,
            WarehouseEventType.RECEIVING_ARRIVED,
        )
        self.assertEqual(result.code, WarehouseStateCode.RECEIVED_UNPLACED)

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.PLACEMENT_COMPLETED,
        )
        self.assertEqual(result.code, WarehouseStateCode.PLACED_IN_RECEIVING)

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.PUTAWAY_REQUESTED,
            operation_type="putaway",
        )
        self.assertEqual(result.code, WarehouseStateCode.PLACED_IN_RECEIVING)
        self.assertTrue(result.is_noop)

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.PUTAWAY_COMPLETED,
            operation_type="putaway",
            zone_to="OS",
        )
        self.assertEqual(result.code, WarehouseStateCode.STORED)

    def test_processing_flow_requires_obr_arrival(self):
        result = WarehouseTransitionService.apply_event(
            WarehouseStateCode.STORED,
            WarehouseEventType.PROCESSING_RESERVED,
        )
        self.assertEqual(result.code, WarehouseStateCode.RESERVED_FOR_PROCESSING)

        with self.assertRaises(WarehouseTransitionError):
            WarehouseTransitionService.apply_event(
                result.code,
                WarehouseEventType.PROCESSING_STARTED,
            )

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.MOVEMENT_STARTED,
            operation_type="move_to_processing",
        )
        self.assertEqual(result.code, WarehouseStateCode.MOVING_TO_PROCESSING)

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.PROCESSING_ZONE_ARRIVED,
            zone_to="OBR",
        )
        self.assertEqual(result.code, WarehouseStateCode.IN_PROCESSING_ZONE)

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.PROCESSING_STARTED,
        )
        self.assertEqual(result.code, WarehouseStateCode.PROCESSING_IN_PROGRESS)

    def test_shipping_flow_reaches_shipped(self):
        result = WarehouseTransitionService.apply_event(
            WarehouseStateCode.STORED,
            WarehouseEventType.SHIPPING_RESERVED,
        )
        self.assertEqual(result.code, WarehouseStateCode.RESERVED_FOR_SHIPPING)

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.MOVEMENT_STARTED,
            operation_type="move_to_otg",
        )
        self.assertEqual(result.code, WarehouseStateCode.MOVING_TO_OTG)

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.OTG_ARRIVED,
            zone_to="OTG",
        )
        self.assertEqual(result.code, WarehouseStateCode.IN_OTG)

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.PALLETIZATION_STARTED,
        )
        self.assertEqual(result.code, WarehouseStateCode.PALLETIZING)

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.PALLETIZATION_COMPLETED,
        )
        self.assertEqual(result.code, WarehouseStateCode.READY_FOR_LOADING)

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.ASSIGNED_TO_TRIP,
        )
        self.assertEqual(result.code, WarehouseStateCode.ASSIGNED_TO_TRIP)

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.LOADING_STARTED,
        )
        self.assertEqual(result.code, WarehouseStateCode.LOADING_IN_PROGRESS)

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.LOADED_TO_VEHICLE,
        )
        self.assertEqual(result.code, WarehouseStateCode.LOADED_TO_VEHICLE)

        result = WarehouseTransitionService.apply_event(
            result.code,
            WarehouseEventType.SHIPPED,
        )
        self.assertEqual(result.code, WarehouseStateCode.SHIPPED)
