from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.utils import timezone

from audit.models import OrderAuditEntry
from reachtruck.models import MoveTask
from sku.models import Agency
from sku.models import SKU, SKUBarcode
from sklad.models import (
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
from sklad.services.warehouse_stock_rows import snapshot_stock_rows
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
    _shipping_shipped_qty_map,
)


def create_warehouse_snapshot_row(
    *,
    agency: Agency,
    order_type: str = "receiving",
    order_id: str,
    sku: str,
    name: str,
    size: str,
    barcode: str = "",
    marking_code: str = "",
    goods_type: str,
    qty: int,
    available_qty: int | None = None,
    processing_reserved_qty: int = 0,
    shipping_reserved_qty: int = 0,
    box_code: str = "",
    pallet_code: str = "",
    zone: str = "OS",
    row: int = 1,
    section: int = 1,
    tier: int = 1,
    cell: int = 1,
    warehouse_state_code: str = WarehouseStateCode.STORED.value,
) -> WarehouseStockSnapshot:
    location = WarehouseWritePathService.ensure_location(
        warehouse_code="MSK",
        zone_code=zone,
        row_no=row,
        section_no=section,
        tier_no=tier,
        cell_no=cell,
    )
    parent_container = None
    container = None
    if pallet_code:
        parent_container, _ = WarehouseContainer.objects.get_or_create(
            agency=agency,
            container_code=pallet_code,
            defaults={
                "container_type": WarehouseContainer.TYPE_PALLET,
                "current_location": location,
                "source_context_type": order_type,
                "source_context_id": str(order_id),
            },
        )
    if box_code:
        container, _ = WarehouseContainer.objects.get_or_create(
            agency=agency,
            container_code=box_code,
            defaults={
                "container_type": WarehouseContainer.TYPE_BOX,
                "parent_container": parent_container,
                "current_location": location,
                "source_context_type": order_type,
                "source_context_id": str(order_id),
            },
        )
        if parent_container and container.parent_container_id != parent_container.id:
            container.parent_container = parent_container
            container.save(update_fields=["parent_container", "updated_at"])
    elif parent_container:
        container = parent_container
    return WarehouseStockSnapshot.objects.create(
        agency=agency,
        source_context_type=order_type,
        source_context_id=order_id,
        sku_code=sku,
        name=name,
        size=size,
        barcode=barcode,
        marking_code=marking_code,
        goods_type=goods_type,
        qty=qty,
        available_qty=qty if available_qty is None else available_qty,
        processing_reserved_qty=processing_reserved_qty,
        shipping_reserved_qty=shipping_reserved_qty,
        container=container,
        container_code=container.container_code if container else "",
        parent_container=parent_container if container and container.container_type == WarehouseContainer.TYPE_BOX else None,
        location=location,
        zone_code=location.zone_code,
        zone_kind=location.zone_kind,
        warehouse_state_code=warehouse_state_code,
    )


class StockStateHelpersTests(SimpleTestCase):
    def test_normalize_goods_type_and_inventory_key(self):
        self.assertEqual(_normalize_goods_type("op"), "оптовый")
        self.assertEqual(_normalize_goods_type("  Готовый "), "готовый")
        self.assertEqual(_inventory_key(" SKU-1 ", " 42 ", " OP "), ("sku-1", "42", "оптовый"))

    def test_goods_type_label_prefers_explicit_label(self):
        self.assertEqual(_goods_type_label("receiving", {"goods_type_label": "Мой тип"}), "Мой тип")
        self.assertEqual(_goods_type_label("processing", {}), "Готовый")
        self.assertEqual(_goods_type_label("receiving", {}), "Оптовый")

    def test_legacy_processing_source_deductions_are_disabled(self):
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

        self.assertIsNone(adjusted)

    def test_legacy_audit_qty_maps_do_not_rebuild_stock(self):
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
        shipped = SimpleNamespace(
            order_type="shipping",
            order_id="SO-1",
            payload={
                "shipping_state": "shipped",
                "shipped_items": [
                    {"sku": "SKU-1", "size": "42", "goods_type": "Готовый", "qty": 10},
                ],
            },
        )

        self.assertEqual(_receiving_removed_qty_map([entry]), {})
        self.assertEqual(_shipping_shipped_qty_map([shipped]), {})

    def test_location_normalization_and_defaulting(self):
        normalized = _normalize_location({"zone": "os", "row": "2", "section": "3", "tier": "4", "cell": "5"})
        self.assertEqual(normalized["zone"], "OS")
        self.assertEqual(normalized["row"], 2)
        self.assertEqual(normalized["section"], 3)
        self.assertEqual(normalized["tier"], 4)
        self.assertEqual(normalized["cell"], 5)

        row = _ensure_pallet_location({"pallet_code": "P-1"}, default_zone="OBR")
        self.assertEqual(row["zone"], "OBR")


class StockAvailabilityServiceTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Availability Agency")
        self.location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OS",
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=1,
        )
        self.snapshot = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="1000",
            sku_code="SKU-1",
            name="Товар 1",
            size="42",
            goods_type="Не обработанный",
            qty=50,
            available_qty=0,
            processing_reserved_qty=50,
            location=self.location,
            zone_code=self.location.zone_code,
            zone_kind=self.location.zone_kind,
            warehouse_state_code=WarehouseStateCode.STORED.value,
        )
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

    def test_inventory_items_for_agency_uses_warehouse_processing_reserve(self):
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
        WarehouseReserve.objects.filter(agency=self.agency).delete()
        WarehouseStockSnapshot.objects.filter(agency=self.agency).update(
            available_qty=30,
            processing_reserved_qty=0,
            shipping_reserved_qty=20,
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id="SO-TEST-1",
            sku_code="SKU-1",
            size="42",
            goods_type="Не обработанный",
            qty_reserved=20,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        items = StockAvailabilityService.inventory_items_for_agency(self.agency)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["sku"], "SKU-1")
        self.assertEqual(items[0]["qty"], 30)

    def test_inventory_items_for_agency_does_not_rebuild_from_audit_when_stock_empty(self):
        WarehouseStockSnapshot.objects.filter(agency=self.agency).delete()
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
        self.assertFalse(WarehouseStockSnapshot.objects.filter(agency=self.agency).exists())

    def test_inventory_items_for_agency_uses_warehouse_snapshot_when_legacy_rows_missing(self):
        WarehouseStockSnapshot.objects.filter(agency=self.agency).delete()
        WarehouseReserve.objects.filter(agency=self.agency).delete()
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_id="R-SNAP-INV",
            sku="SKU-1",
            name="Товар 1",
            size="42",
            goods_type="Не обработанный",
            qty=50,
            box_code="BOX-INV-1",
            pallet_code="PAL-INV-1",
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

    def test_create_receiving_putaway_tasks_skips_reserved_os_destination(self):
        SKU.objects.create(agency=self.agency, sku_code="SKU-RESERVED-CMD", name="Товар резерва", size="42")
        WarehouseWritePathService.create_receiving_placement(
            agency=self.agency,
            order_id="RCV-RESERVED-1",
            performed_by=self.user,
            items=[
                {
                    "sku_code": "SKU-RESERVED-CMD",
                    "name": "Товар резерва",
                    "size": "42",
                    "barcode": "BR-RES-1",
                    "goods_type": "gv",
                    "qty": 5,
                    "pallet_code": "PAL-RESERVED-1",
                }
            ],
        )
        WarehouseWritePathService.request_putaway_for_receiving(
            agency=self.agency,
            order_id="RCV-RESERVED-1",
            container_codes=["PAL-RESERVED-1"],
            destination_zone_code="OS",
            destination_row_no=2,
            destination_section_no=1,
            destination_tier_no=1,
            destination_cell_no=3,
            requested_by=self.user,
            requested_by_role="storekeeper",
        )

        result = WarehouseCommandService.create_receiving_putaway_tasks(
            order_id="RCV-RESERVED-2",
            agency=self.agency,
            role="storekeeper",
            placement_payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {
                        "code": "BOX-RESERVED-CMD-2",
                        "sealed": True,
                        "items": [
                            {
                                "sku": "SKU-RESERVED-CMD",
                                "name": "Товар резерва",
                                "size": "42",
                                "barcode": "BR-RES-2",
                                "qty": 5,
                            }
                        ],
                    }
                ],
                "act_pallets": [
                    {
                        "code": "PAL-RESERVED-CMD-2",
                        "sealed": True,
                        "boxes": ["BOX-RESERVED-CMD-2"],
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

        self.assertEqual(result.status, "noop")
        self.assertEqual(result.created_count, 0)
        self.assertEqual(result.skipped_existing_count, 1)
        self.assertFalse(MoveTask.objects.exists())

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

        snapshot = WarehouseStockSnapshot.objects.get(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id=order_id,
            marking_code="CZ-3008-1",
        )
        self.assertEqual(snapshot.sku_code, "SKU-RCV-COMP")
        self.assertEqual(snapshot.qty, 1)
        self.assertEqual(snapshot.available_qty, 1)
        self.assertEqual(snapshot.container_code, "BOX-COMP-1")
        self.assertEqual(snapshot.container.container_code, "BOX-COMP-1")
        self.assertEqual(snapshot.parent_container.container_code, "PAL-COMP-1")
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
            WarehouseStockSnapshot.objects.filter(
                agency=self.agency,
                source_context_type="receiving",
                source_context_id=order_id,
                sku_code="SKU-RCV",
            ).exists()
        )

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
        self.assertEqual(snapshots[0].container_code, "BOX-CLOSE-1")
        self.assertEqual(snapshots[0].container.container_code, "BOX-CLOSE-1")
        self.assertEqual(snapshots[0].parent_container.container_code, "PAL-CLOSE-1")
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

    def test_refresh_materialized_stock_state_reports_warehouse_core_totals(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_id="1000",
            sku="SKU-1",
            name="Товар 1",
            size="42",
            goods_type="Не обработанный",
            qty=50,
            available_qty=30,
            shipping_reserved_qty=20,
            pallet_code="PAL-1000",
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id="SO-TEST-2",
            sku_code="SKU-1",
            size="42",
            goods_type="Не обработанный",
            qty_reserved=20,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        result = refresh_materialized_stock_state_for_agency(self.agency)

        self.assertEqual(result["source"], "warehouse_core")
        self.assertEqual(result["snapshot_count"], 1)
        self.assertEqual(result["qty"], 50)
        self.assertEqual(result["available_qty"], 30)
        self.assertEqual(result["processing_reserved_qty"], 0)
        self.assertEqual(result["shipping_reserved_qty"], 20)
        self.assertEqual(result["reserve_count"], 1)

    def test_occupied_os_cells_deduplicates_coordinates(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="3000",
            sku="SKU-2",
            name="Товар 2",
            size="43",
            goods_type="Оптовый",
            qty=10,
            pallet_code="PAL-3000",
            zone="OS",
            row=1,
            section=2,
            tier=3,
            cell=4,
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="processing",
            order_id="3001",
            sku="SKU-3",
            name="Товар 3",
            size="44",
            goods_type="Готовый",
            qty=5,
            pallet_code="PAL-3001",
            zone="OS",
            row=1,
            section=2,
            tier=3,
            cell=4,
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="processing",
            order_id="3002",
            sku="SKU-4",
            name="Товар 4",
            size="45",
            goods_type="Готовый",
            qty=7,
            pallet_code="PAL-3002",
            zone="OS",
            row=1,
            section=2,
            tier=3,
            cell=5,
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
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="processing",
            order_id="4001",
            sku="SKU-5",
            name="Товар 5",
            size="46",
            goods_type="Готовый",
            qty=9,
            pallet_code="PAL-4001",
            zone="OS",
            row=2,
            section=1,
            tier=1,
            cell=1,
        )

        keys = StockAvailabilityService.occupied_os_cell_keys(
            exclude_order_type="processing",
            exclude_order_id="4001",
        )
        self.assertNotIn((2, 1, 1, 1), keys)

    def test_occupied_os_cells_include_active_putaway_destinations(self):
        destination = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OS",
            row_no=4,
            section_no=2,
            tier_no=1,
            cell_no=3,
        )
        WarehouseOperation.objects.create(
            agency=self.agency,
            operation_type=WarehouseOperation.TYPE_PUTAWAY,
            context_type="receiving",
            context_id="PUTAWAY-RESERVED-1",
            destination_location=destination,
            destination_zone_code="OS",
            status=WarehouseOperation.STATUS_PLANNED,
        )

        keys = StockAvailabilityService.occupied_os_cell_keys()
        sections = StockAvailabilityService.occupied_os_section_agencies()

        self.assertIn((4, 2, 1, 3), keys)
        self.assertEqual(sections[(4, 2)], {self.agency.id})

    def test_occupied_os_cells_excludes_pallet_code(self):
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="5001",
            sku="SKU-6",
            name="Товар 6",
            size="47",
            goods_type="Оптовый",
            qty=4,
            pallet_code="PAL-KEEP",
            zone="OS",
            row=3,
            section=1,
            tier=1,
            cell=1,
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="5002",
            sku="SKU-7",
            name="Товар 7",
            size="48",
            goods_type="Оптовый",
            qty=4,
            pallet_code="PAL-EXCLUDE",
            zone="OS",
            row=3,
            section=1,
            tier=1,
            cell=2,
        )

        keys = StockAvailabilityService.occupied_os_cell_keys(exclude_pallet_code="PAL-EXCLUDE")
        self.assertIn((3, 1, 1, 1), keys)
        self.assertNotIn((3, 1, 1, 2), keys)

    def test_suggest_os_cell_prefers_same_client_section(self):
        other_agency = Agency.objects.create(agn_name="Другой клиент")
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="6001",
            sku="SKU-A",
            name="Товар A",
            size="42",
            goods_type="Оптовый",
            qty=4,
            pallet_code="PAL-A",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=1,
        )
        create_warehouse_snapshot_row(
            agency=other_agency,
            order_type="receiving",
            order_id="6002",
            sku="SKU-B",
            name="Товар B",
            size="42",
            goods_type="Оптовый",
            qty=4,
            pallet_code="PAL-B",
            zone="OS",
            row=1,
            section=2,
            tier=1,
            cell=1,
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
        create_warehouse_snapshot_row(
            agency=other_agency,
            order_type="receiving",
            order_id="7001",
            sku="SKU-C",
            name="Товар C",
            size="42",
            goods_type="Оптовый",
            qty=4,
            pallet_code="PAL-C",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=1,
        )
        create_warehouse_snapshot_row(
            agency=other_agency,
            order_type="receiving",
            order_id="7002",
            sku="SKU-D",
            name="Товар D",
            size="42",
            goods_type="Оптовый",
            qty=4,
            pallet_code="PAL-D",
            zone="OS",
            row=1,
            section=3,
            tier=1,
            cell=1,
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
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_id="1001",
            sku="SKU-1",
            name="Джинсы",
            size="42",
            goods_type="Оптовый",
            qty=10,
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="2001",
            sku_code="SKU-1",
            size="42",
            goods_type="Оптовый",
            qty_reserved=2,
            status=WarehouseReserve.STATUS_ACTIVE,
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id="SO-000001",
            sku_code="SKU-1",
            size="42",
            goods_type="Оптовый",
            qty_reserved=3,
            status=WarehouseReserve.STATUS_ACTIVE,
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id="SO-000002",
            sku_code="SKU-1",
            size="42",
            goods_type="Оптовый",
            qty_reserved=9,
            status=WarehouseReserve.STATUS_CANCELED,
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
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_id="1002",
            sku="SKU-1",
            name="Джинсы",
            size="42",
            goods_type="Оптовый",
            qty=10,
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
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id="SO-000003",
            sku_code="SKU-2",
            size="44",
            goods_type="Готовый",
            qty_reserved=5,
            status=WarehouseReserve.STATUS_ACTIVE,
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
        storage_location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OS",
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=1,
        )
        processing_location = WarehouseWritePathService.ensure_location(warehouse_code="MSK", zone_code="OBR")
        operation = WarehouseOperation.objects.create(
            agency=self.agency,
            operation_type=WarehouseOperation.TYPE_PROCESSING,
            context_type="processing",
            context_id="2002",
            source_location=processing_location,
            destination_location=processing_location,
            source_zone_code=processing_location.zone_code,
            destination_zone_code=processing_location.zone_code,
            status=WarehouseOperation.STATUS_IN_PROGRESS,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="1002",
            sku_code="SKU-1",
            name="Джинсы",
            size="42",
            goods_type="Оптовый",
            qty=2,
            available_qty=2,
            location=storage_location,
            zone_code=storage_location.zone_code,
            zone_kind=storage_location.zone_kind,
            warehouse_state_code=WarehouseStateCode.STORED.value,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="legacy_stock",
            source_context_id="legacy-2002",
            sku_code="SKU-1",
            name="Джинсы",
            size="42",
            goods_type="Оптовый",
            qty=8,
            available_qty=0,
            processing_reserved_qty=8,
            active_operation=operation,
            active_operation_type=operation.operation_type,
            location=processing_location,
            zone_code=processing_location.zone_code,
            zone_kind=processing_location.zone_kind,
            warehouse_state_code=WarehouseStateCode.PROCESSING_IN_PROGRESS.value,
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="2002",
            sku_code="SKU-1",
            size="42",
            goods_type="Оптовый",
            qty_reserved=10,
            status=WarehouseReserve.STATUS_ACTIVE,
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
        create_warehouse_snapshot_row(
            agency=self.agency_a,
            order_id="A-1",
            sku="SKU-X",
            name="Товар X",
            size="42",
            goods_type="Оптовый",
            qty=4,
            box_code="BOX-A1",
            pallet_code="PAL-A1",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=1,
        )
        create_warehouse_snapshot_row(
            agency=self.agency_a,
            order_id="A-2",
            sku="SKU-X",
            name="Товар X",
            size="42",
            goods_type="Оптовый",
            qty=6,
            box_code="BOX-A2",
            pallet_code="PAL-A2",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=2,
        )
        create_warehouse_snapshot_row(
            agency=self.agency_b,
            order_id="B-1",
            sku="SKU-X",
            name="Товар X",
            size="42",
            goods_type="Оптовый",
            qty=5,
            box_code="BOX-B1",
            pallet_code="PAL-B1",
            zone="OS",
            row=2,
            section=1,
            tier=1,
            cell=1,
        )
        WarehouseReserve.objects.create(
            agency=self.agency_a,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="P-A",
            sku_code="SKU-X",
            size="42",
            goods_type="Оптовый",
            qty_reserved=3,
            status=WarehouseReserve.STATUS_ACTIVE,
        )
        WarehouseReserve.objects.create(
            agency=self.agency_b,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id="SO-009999",
            sku_code="SKU-X",
            size="42",
            goods_type="Оптовый",
            qty_reserved=2,
            status=WarehouseReserve.STATUS_ACTIVE,
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
        create_warehouse_snapshot_row(
            agency=self.client_agency,
            order_id="R-SVC-1",
            sku="SKU-SVC",
            name="Товар клиента",
            size="42",
            goods_type="Оптовый",
            qty=4,
        )
        create_warehouse_snapshot_row(
            agency=self.other_agency,
            order_id="R-SVC-2",
            sku="SKU-OTHER",
            name="Чужой товар",
            size="43",
            goods_type="Оптовый",
            qty=9,
            row=2,
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
        create_warehouse_snapshot_row(
            agency=self.client_agency,
            order_id="R-SVC-3",
            sku="SKU-A",
            name="Товар А",
            size="42",
            goods_type="Оптовый",
            qty=5,
        )
        create_warehouse_snapshot_row(
            agency=self.other_agency,
            order_id="R-SVC-4",
            sku="SKU-B",
            name="Товар Б",
            size="44",
            goods_type="Оптовый",
            qty=7,
            row=2,
        )
        request = self.factory.get("/sklad/journal/", {"client": str(self.other_agency.id)})
        request.user = self.staff_user

        page = build_inventory_journal_page(request=request)

        self.assertEqual(page["template_name"], "sklad/inventory_journal.html")
        self.assertEqual(page["context"]["client_agency"], self.other_agency)
        rows = page["context"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["agency_id"], self.other_agency.id)

    def test_build_inventory_journal_page_uses_common_order_and_location_display_for_staff(self):
        create_warehouse_snapshot_row(
            agency=self.other_agency,
            order_id="7",
            sku="SKU-B",
            name="Товар Б",
            size="44",
            goods_type="Оптовый",
            qty=7,
            box_code="BOX-TD-1",
            pallet_code="ТДТ-0405-289465",
            zone="OS",
            row=2,
            section=3,
            tier=1,
            cell=4,
        )
        request = self.factory.get("/sklad/journal/", {"client": str(self.other_agency.id), "q": "7_PR"})
        request.user = self.staff_user

        page = build_inventory_journal_page(request=request)

        rows = page["context"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["order_display"], "7_PR")
        self.assertEqual(rows[0]["location_short"], "OS-2/3-1-4")
        self.assertEqual(page["context"]["journal_summary"]["total_qty"], 7)
        self.assertEqual(page["context"]["journal_focus"]["order_label"], "7_PR")


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

    def test_replace_order_placement_restores_missing_barcode_from_nomenclature(self):
        OperationalStockService.replace_order_placement(
            self.agency,
            "receiving",
            "R-100",
            {
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

        snapshot = WarehouseStockSnapshot.objects.get(agency=self.agency, sku_code="SKU-BC-1", size="42")
        self.assertEqual(snapshot.barcode, "2000999000001")

    def test_replace_order_placement_keeps_explicit_payload_barcode(self):
        OperationalStockService.replace_order_placement(
            self.agency,
            "receiving",
            "R-101",
            {
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

        snapshot = WarehouseStockSnapshot.objects.get(agency=self.agency, sku_code="SKU-BC-1", size="42")
        self.assertEqual(snapshot.barcode, "5550001112223")


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

    def test_replace_order_placement_creates_separate_rows_per_marking_code(self):
        OperationalStockService.replace_order_placement(
            self.agency,
            "receiving",
            "R-CZ-200",
            {
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

        rows = list(
            WarehouseStockSnapshot.objects.filter(
                agency=self.agency,
                sku_code="SKU-CZ-2",
                size="43",
            ).order_by("marking_code")
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual([row.qty for row in rows], [1, 1])
        self.assertEqual([row.marking_code for row in rows], ["CZ-200-1", "CZ-200-2"])


class OperationalStockReadApiTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Operational Stock Read API")
        create_warehouse_snapshot_row(
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
        )
        create_warehouse_snapshot_row(
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
        )
        create_warehouse_snapshot_row(
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

    def test_receiving_sync_keeps_product_box_pallet_and_goods_type_truth(self):
        placement = WarehouseWritePathService.sync_receiving_placement(
            agency=self.agency,
            order_id="RCV-TRUTH-1",
            performed_by=self.user,
            placement_payload={
                "goods_type": "op",
                "act_boxes": [
                    {
                        "code": "BOX-TRUTH-1",
                        "items": [
                            {
                                "sku": self.sku.sku_code,
                                "name": self.sku.name,
                                "size": "42",
                                "qty": 5,
                            }
                        ],
                    }
                ],
                "act_pallets": [
                    {
                        "code": "PAL-TRUTH-1",
                        "boxes": ["BOX-TRUTH-1"],
                        "items": [],
                    }
                ],
            },
        )

        self.assertEqual(len(placement.snapshot_ids), 1)
        snapshot = WarehouseStockSnapshot.objects.select_related(
            "container",
            "parent_container",
        ).get(id=placement.snapshot_ids[0])
        self.assertEqual(snapshot.container_code, "BOX-TRUTH-1")
        self.assertEqual(snapshot.container.container_type, WarehouseContainer.TYPE_BOX)
        self.assertEqual(snapshot.parent_container.container_code, "PAL-TRUTH-1")
        self.assertEqual(snapshot.goods_type, "op")

        rows = snapshot_stock_rows(agency=self.agency)
        row = next(row for row in rows if row["order_id"] == "RCV-TRUTH-1")
        self.assertEqual(row["sku"], self.sku.sku_code)
        self.assertEqual(row["box_code"], "BOX-TRUTH-1")
        self.assertEqual(row["pallet_code"], "PAL-TRUTH-1")
        self.assertEqual(row["goods_type"], "op")
        self.assertEqual(row["zone"], "PR")

    def test_receiving_putaway_by_pallet_moves_child_boxes_and_container_location(self):
        placement = WarehouseWritePathService.create_receiving_placement(
            agency=self.agency,
            order_id="RCV-TRUTH-2",
            performed_by=self.user,
            items=[
                {
                    "sku_code": self.sku.sku_code,
                    "name": self.sku.name,
                    "size": "43",
                    "goods_type": "gv",
                    "qty": 7,
                    "pallet_code": "PAL-TRUTH-2",
                    "box_code": "BOX-TRUTH-2",
                }
            ],
        )
        snapshot = WarehouseStockSnapshot.objects.select_related(
            "container",
            "parent_container",
        ).get(id=placement.snapshot_ids[0])

        operation = WarehouseWritePathService.request_putaway_for_receiving(
            agency=self.agency,
            order_id="RCV-TRUTH-2",
            container_codes=["PAL-TRUTH-2"],
            destination_zone_code="OS",
            destination_row_no=3,
            destination_section_no=2,
            destination_tier_no=1,
            destination_cell_no=4,
            requested_by=self.user,
            requested_by_role="storekeeper",
        )

        task = operation.tasks.select_related("container").get()
        self.assertEqual(task.task_type, WarehouseOperationTask.TYPE_PALLET_MOVE)
        self.assertEqual(task.container.container_code, "PAL-TRUTH-2")
        self.assertEqual(task.qty_planned, 7)
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.active_operation_id, operation.id)

        WarehouseWritePathService.complete_putaway_operation(
            operation=operation,
            performed_by=self.user,
        )

        snapshot.refresh_from_db()
        snapshot.container.refresh_from_db()
        snapshot.parent_container.refresh_from_db()
        completed_event = WarehouseEvent.objects.filter(
            stock_context_type="receiving",
            stock_context_id="RCV-TRUTH-2",
            event_type=WarehouseEventType.PUTAWAY_COMPLETED.value,
        ).latest("id")
        self.assertEqual(snapshot.zone_code, "OS")
        self.assertEqual(snapshot.location.row_no, 3)
        self.assertEqual(snapshot.container.current_location_id, snapshot.location_id)
        self.assertEqual(snapshot.parent_container.current_location_id, snapshot.location_id)
        self.assertEqual(completed_event.container.container_code, "PAL-TRUTH-2")

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
