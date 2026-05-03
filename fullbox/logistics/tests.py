from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from agent.models import DeviceAgent
from audit.models import OrderAuditEntry
from employees.models import Employee
from head_manager.models import Carrier, OwnCompany
from shipping.models import ShippingOrder, ShippingOrderItem
from sklad.models import WarehouseContainer, WarehouseLocation, WarehouseReserve, WarehouseStockSnapshot
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sku.models import Agency
from todo.models import Task

from .models import LogisticsTrip, LogisticsTripOrder, next_draft_trip_number


class LogisticsDashboardTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.logistician_user = user_model.objects.create_user(username="logistician_test", password="pwd")
        self.logistician = Employee.objects.create(
            full_name="Логистов Сергей",
            role="logistician",
            user=self.logistician_user,
            is_active=True,
        )
        self.storekeeper_user = user_model.objects.create_user(username="storekeeper_test", password="pwd")
        self.storekeeper = Employee.objects.create(
            full_name="Кладовщик Петр",
            role="storekeeper",
            user=self.storekeeper_user,
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Индивидуальный предприниматель Талеев Денис Толибаевич")
        self.own_company = OwnCompany.objects.create(
            name='Общество с ограниченной ответственностью "ФуллБокс"',
            is_default=True,
            is_active=True,
        )
        self.carrier = Carrier.objects.create(
            name="ИП Касаев Абдурашид Метханович",
            is_active=True,
        )

    def _create_packed_order(self, number: str, *, pallet_count: int = 2, box_count: int = 16) -> ShippingOrder:
        order = ShippingOrder.objects.create(
            number=number,
            agency=self.agency,
            created_by=self.logistician_user,
            status=ShippingOrder.STATUS_PACKED,
            slot_date=date(2026, 4, 2),
            destination_warehouse="Склад WB Тюмень",
            expected_boxes=box_count,
        )
        OrderAuditEntry.objects.create(
            order_id=order.number,
            order_type="shipping",
            action="status",
            agency=self.agency,
            user=self.logistician_user,
            description="Паллетизация завершена",
            payload={
                "act": "shipping_packing",
                "pallet_count": pallet_count,
                "delivered_box_count": box_count,
            },
        )
        return order

    def _create_packed_order_with_pallets(self, number: str, pallets: list[dict]) -> ShippingOrder:
        order = ShippingOrder.objects.create(
            number=number,
            agency=self.agency,
            created_by=self.logistician_user,
            status=ShippingOrder.STATUS_PACKED,
            slot_date=date(2026, 4, 2),
            destination_warehouse=f"Склад {number}",
            expected_boxes=sum(len(list(pallet.get("boxes") or [])) for pallet in pallets),
        )
        act_boxes = []
        act_pallets = []
        total_boxes = 0
        for pallet in pallets:
            box_refs = []
            for index, box in enumerate(list(pallet.get("boxes") or []), start=1):
                code = str(box.get("code") or "").strip()
                if not code:
                    continue
                row_key = f"{code}::{index}"
                total_boxes += 1
                box_refs.append(row_key)
                act_boxes.append(
                    {
                        "row_key": row_key,
                        "code": code,
                        "qty": int(box.get("qty") or 1),
                        "items": [],
                        "barcode_preview": str(box.get("barcode_preview") or ""),
                        "pallet_label": str(pallet.get("label") or ""),
                    }
                )
            act_pallets.append(
                {
                    "label": str(pallet.get("label") or ""),
                    "code": str(pallet.get("code") or ""),
                    "boxes": box_refs,
                    "qty": sum(int(box.get("qty") or 1) for box in list(pallet.get("boxes") or [])),
                }
            )
        OrderAuditEntry.objects.create(
            order_id=order.number,
            order_type="shipping",
            action="status",
            agency=self.agency,
            user=self.logistician_user,
            description="Паллетизация завершена",
            payload={
                "act": "shipping_packing",
                "pallet_count": len(act_pallets),
                "delivered_box_count": total_boxes,
                "act_pallets": act_pallets,
                "act_boxes": act_boxes,
            },
        )
        return order

    def _create_packed_order_with_warehouse_pallet(self, number: str, *, pallet_code: str) -> ShippingOrder:
        order = ShippingOrder.objects.create(
            number=number,
            agency=self.agency,
            created_by=self.logistician_user,
            status=ShippingOrder.STATUS_PACKED,
            slot_date=date(2026, 4, 2),
            destination_warehouse=f"Склад {number}",
            expected_boxes=1,
        )
        ShippingOrderItem.objects.create(
            order=order,
            sku_code="SKU-LOAD-1",
            name="Товар погрузки",
            size="44",
            barcode="355500000001",
            goods_type="Готовый",
            qty_requested=10,
            qty_reserved=10,
        )
        location = WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="OTG",
            zone_kind=WarehouseLocation.ZONE_KIND_SHIPPING,
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="OTG-1-1-1-1",
            display_name="OTG · Зона отгрузки",
        )
        shipping_pallet = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_MIXED_PALLET,
            container_code=pallet_code,
            current_location=location,
            source_context_type="shipping",
            source_context_id=order.number,
            created_by=self.logistician_user,
        )
        box = WarehouseContainer.objects.create(
            agency=self.agency,
            container_type=WarehouseContainer.TYPE_BOX,
            container_code=f"{pallet_code}-BOX-1",
            parent_container=shipping_pallet,
            current_location=location,
            source_context_type="receiving",
            source_context_id="R-LOAD-1",
            created_by=self.logistician_user,
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id=order.number,
            sku_code="SKU-LOAD-1",
            size="44",
            barcode="355500000001",
            goods_type="Готовый",
            qty_reserved=10,
            qty_allocated=10,
            status=WarehouseReserve.STATUS_ALLOCATED,
            created_by=self.logistician_user,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="receiving",
            source_context_id="R-LOAD-1",
            sku_code="SKU-LOAD-1",
            name="Товар погрузки",
            size="44",
            barcode="355500000001",
            goods_type="Готовый",
            qty=10,
            available_qty=0,
            shipping_reserved_qty=10,
            container=box,
            container_code=box.container_code,
            parent_container=shipping_pallet,
            location=location,
            zone_code="OTG",
            zone_kind=location.zone_kind,
            warehouse_state_code="ready_for_loading",
        )
        OrderAuditEntry.objects.create(
            order_id=order.number,
            order_type="shipping",
            action="status",
            agency=self.agency,
            user=self.logistician_user,
            description="Паллетизация завершена",
            payload={
                "act": "shipping_packing",
                "pallet_count": 1,
                "delivered_box_count": 1,
                "act_pallets": [{"label": "Паллета 1", "code": pallet_code, "boxes": [f"{pallet_code}-BOX-1::1"], "qty": 10}],
                "act_boxes": [{"row_key": f"{pallet_code}-BOX-1::1", "code": f"{pallet_code}-BOX-1", "qty": 10, "items": [], "pallet_label": "Паллета 1", "pallet_code": pallet_code}],
            },
        )
        return order

    def test_logistician_can_open_dashboard(self):
        order = self._create_packed_order("SO-000001")
        self.client.force_login(self.logistician_user)

        response = self.client.get(reverse("logistics:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Логистический контур")
        self.assertContains(response, "1_OTG")
        self.assertNotContains(response, order.number)
        self.assertContains(response, "Готовы к погрузке")

    def test_dashboard_marks_trip_orders_as_preparing_for_trip(self):
        order = self._create_packed_order("SO-000003")
        trip = LogisticsTrip.objects.create(
            number="TRIP-000001",
            trip_date=date(2026, 4, 3),
            status=LogisticsTrip.STATUS_PLANNED,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        self.client.force_login(self.logistician_user)

        response = self.client.get(reverse("logistics:dashboard"))

        self.assertContains(response, ">Р<", html=False)
        self.assertContains(response, "Подготовка к рейсу")
        self.assertContains(response, f"/logistics/trips/{trip.pk}/")
        self.assertContains(response, "Открыть рейс")
        self.assertContains(response, "/logistics/trips/")

    def test_dashboard_marks_departed_trip_orders_as_loaded(self):
        order = self._create_packed_order("SO-000003A")
        trip = LogisticsTrip.objects.create(
            number="TRIP-000001A",
            trip_date=date(2026, 4, 3),
            status=LogisticsTrip.STATUS_DEPARTED,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        self.client.force_login(self.logistician_user)

        response = self.client.get(reverse("logistics:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Загружено в машину")
        self.assertNotContains(response, "Подготовка к рейсу")
        self.assertContains(response, 'data-stage="shipped"', html=False)
        self.assertContains(response, "Отгружено")
        self.assertContains(response, f"/logistics/trips/{trip.pk}/")

    def test_dashboard_groups_trip_orders_together_before_free_ready_orders(self):
        first_order = self._create_packed_order("SO-000011")
        second_order = self._create_packed_order("SO-000012")
        free_order = self._create_packed_order("SO-000013")
        first_order.slot_date = date(2026, 4, 7)
        first_order.save(update_fields=["slot_date", "updated_at"])
        second_order.slot_date = None
        second_order.save(update_fields=["slot_date", "updated_at"])
        free_order.slot_date = date(2026, 4, 5)
        free_order.save(update_fields=["slot_date", "updated_at"])
        trip = LogisticsTrip.objects.create(
            number="TRIP-000001B",
            trip_date=date(2026, 4, 3),
            status=LogisticsTrip.STATUS_DEPARTED,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=first_order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=second_order,
            loading_sequence=2,
            delivery_sequence=2,
        )
        self.client.force_login(self.logistician_user)

        response = self.client.get(reverse("logistics:dashboard"))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertLess(content.index("11_OTG"), content.index("12_OTG"))
        self.assertLess(content.index("12_OTG"), content.index("13_OTG"))

    def test_dashboard_sorts_ready_rows_by_latest_changes_while_keeping_trip_grouped(self):
        grouped_first_order = self._create_packed_order("SO-000021")
        grouped_second_order = self._create_packed_order("SO-000022")
        free_order = self._create_packed_order("SO-000023")
        grouped_trip = LogisticsTrip.objects.create(
            number="TRIP-000021A",
            trip_date=date(2026, 4, 3),
            status=LogisticsTrip.STATUS_DEPARTED,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=grouped_trip,
            shipping_order=grouped_first_order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        LogisticsTripOrder.objects.create(
            trip=grouped_trip,
            shipping_order=grouped_second_order,
            loading_sequence=2,
            delivery_sequence=2,
        )
        now = timezone.now()
        LogisticsTrip.objects.filter(pk=grouped_trip.pk).update(updated_at=now)
        ShippingOrder.objects.filter(pk=free_order.pk).update(updated_at=now - timedelta(minutes=5))
        self.client.force_login(self.logistician_user)

        response = self.client.get(reverse("logistics:dashboard"))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertLess(content.index("21_OTG"), content.index("22_OTG"))
        self.assertLess(content.index("22_OTG"), content.index("23_OTG"))

    def test_logistician_can_open_trip_list(self):
        order = self._create_packed_order("SO-TRIP-LIST", pallet_count=3, box_count=21)
        trip = LogisticsTrip.objects.create(
            number="TRIP-000111",
            trip_date=date(2026, 4, 7),
            status=LogisticsTrip.STATUS_LOADING,
            vehicle_type=LogisticsTrip.VEHICLE_FULFILLMENT,
            vehicle_name="Транспорт Fullbox",
            vehicle_number="А123ВС777",
            driver_name="Сидоров Сидор Сидорович",
            driver_phone="+7 900 222-11-00",
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        self.client.force_login(self.logistician_user)

        response = self.client.get(reverse("logistics:trip-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Все рейсы")
        self.assertContains(response, "111_RS")
        self.assertContains(response, "Погрузка")
        self.assertContains(response, "Транспорт Fullbox")
        self.assertContains(response, "А 123 ВС 777")
        self.assertContains(response, "Сидоров Сидор Сидорович")
        self.assertContains(response, "ИП Талеев Денис Толибаевич")
        self.assertContains(response, "Склад WB Тюмень")
        self.assertContains(response, f"/logistics/trips/{trip.pk}/")

    def test_logistician_can_create_trip_from_packed_order(self):
        order = self._create_packed_order("SO-000002")
        self.client.force_login(self.logistician_user)

        response = self.client.post(
            reverse("logistics:dashboard"),
            data={
                "action": "create_trip",
                "trip_date": "2026-04-03",
                "vehicle_type": LogisticsTrip.VEHICLE_FULFILLMENT,
                "vehicle_name": "Газель логистики",
                "vehicle_number": "A111BC72",
                "driver_name": "Сергей Водитель",
                "driver_phone": "+7 900 100-00-00",
                "route_comment": "Погрузить в первой волне",
                "order_ids": [str(order.id)],
            },
        )

        self.assertEqual(response.status_code, 302)
        trip = LogisticsTrip.objects.get()
        self.assertEqual(trip.status, LogisticsTrip.STATUS_DRAFT)
        self.assertEqual(trip.assigned_logistician, self.logistician)
        self.assertEqual(trip.vehicle_name, "Газель логистики")
        self.assertTrue(trip.number.startswith("DRAFT-"))
        self.assertLessEqual(len(trip.number), 32)
        self.assertEqual(trip.vehicle_number, "A111BC72")
        self.assertEqual(trip.driver_phone, "+7 900 100-00-00")
        link = LogisticsTripOrder.objects.get()
        self.assertEqual(link.trip, trip)
        self.assertEqual(link.shipping_order, order)
        detail_response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))
        self.assertContains(detail_response, "Заявки в рейсе")
        self.assertContains(detail_response, "Добавить готовые заявки")
        self.assertContains(detail_response, "Маршрут")
        self.assertNotContains(detail_response, "Погрузка в машину")
        self.assertContains(detail_response, "2_OTG")
        self.assertNotContains(detail_response, order.number)
        self.assertContains(detail_response, "ИП Талеев Денис Толибаевич")
        self.assertContains(detail_response, "Подготовка к рейсу")
        self.assertContains(detail_response, "Нет дополнительных готовых заявок.")
        self.assertContains(detail_response, "Параметры загрузки")
        self.assertContains(detail_response, "Сформировать рейс")
        self.assertContains(detail_response, "Черновик")
        self.assertContains(detail_response, "Количество паллет")
        self.assertContains(detail_response, "Количество коробов")
        self.assertContains(detail_response, ">2<", html=False)
        self.assertContains(detail_response, ">16<", html=False)

    def test_next_draft_trip_number_fits_database_field(self):
        value = next_draft_trip_number()
        self.assertTrue(value.startswith("DRAFT-"))
        self.assertLessEqual(len(value), 32)

    def test_trip_detail_updates_sequences(self):
        first_order = self._create_packed_order("SO-LOG-0003", pallet_count=1, box_count=8)
        second_order = self._create_packed_order("SO-LOG-0004", pallet_count=3, box_count=24)
        trip = LogisticsTrip.objects.create(
            number="TRIP-000001",
            trip_date=date(2026, 4, 3),
            status=LogisticsTrip.STATUS_PLANNED,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        first_link = LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=first_order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        second_link = LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=second_order,
            loading_sequence=2,
            delivery_sequence=2,
        )
        self.client.force_login(self.logistician_user)

        response = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={
                "action": "update_sequences",
                "ordered_trip_item_ids": [str(second_link.id), str(first_link.id)],
                f"comment_{first_link.id}": "Погрузить после второй заявки",
                f"comment_{second_link.id}": "Первая в погрузке",
            },
        )

        self.assertEqual(response.status_code, 302)
        first_link.refresh_from_db()
        second_link.refresh_from_db()
        self.assertEqual(first_link.loading_sequence, 2)
        self.assertEqual(first_link.delivery_sequence, 2)
        self.assertEqual(first_link.comment, "Погрузить после второй заявки")
        self.assertEqual(second_link.loading_sequence, 1)
        self.assertEqual(second_link.delivery_sequence, 1)
        self.assertEqual(second_link.comment, "Первая в погрузке")

    def test_trip_detail_updates_loading_params(self):
        order = self._create_packed_order("SO-LOAD-0001", pallet_count=1, box_count=8)
        trip = LogisticsTrip.objects.create(
            number="TRIP-000002",
            trip_date=date(2026, 4, 3),
            status=LogisticsTrip.STATUS_PLANNED,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        self.client.force_login(self.logistician_user)

        response = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={
                "action": "update_loading_params",
                "carrier_id": str(self.carrier.id),
                "vehicle_number": "a123bc777",
                "driver_name": "Иванов Иван Иванович",
                "driver_phone": "8 (900) 555-44-33",
            },
        )

        self.assertEqual(response.status_code, 302)
        trip.refresh_from_db()
        self.assertEqual(trip.carrier, self.carrier)
        self.assertEqual(trip.vehicle_number, "А123ВС777")
        self.assertEqual(trip.driver_name, "Иванов Иван Иванович")
        self.assertEqual(trip.driver_phone, "+7 900 555-44-33")

        detail_response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))
        self.assertContains(detail_response, "Параметры сохраняются автоматически после выхода из блока.")
        self.assertNotContains(detail_response, "Сохранить параметры")
        self.assertNotContains(detail_response, "Сохранить порядок")
        self.assertContains(detail_response, 'name="vehicle_number"', html=False)
        self.assertContains(detail_response, 'value="А123ВС777"', html=False)
        self.assertContains(detail_response, 'id="vehicle-number-editor"', html=False)
        self.assertContains(detail_response, 'name="carrier_id"', html=False)
        self.assertContains(detail_response, "ИП Касаев Абдурашид Метханович")
        self.assertContains(detail_response, "ФуллБокс")
        self.assertContains(detail_response, "ИП Талеев Денис Толибаевич")
        self.assertContains(detail_response, "Склад WB Тюмень")
        self.assertContains(detail_response, 'data-placeholder="А 123 АА 777"', html=False)
        self.assertContains(detail_response, 'id="carrier-selected-label"', html=False)
        self.assertContains(detail_response, "Иванов Иван Иванович")
        self.assertContains(detail_response, "+7 900 555-44-33")

    def test_trip_detail_update_trip_normalizes_vehicle_and_phone(self):
        trip = LogisticsTrip.objects.create(
            number="TRIP-000003",
            trip_date=date(2026, 4, 3),
            status=LogisticsTrip.STATUS_DRAFT,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        self.client.force_login(self.logistician_user)

        response = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={
                "action": "update_trip",
                "trip_date": "2026-04-05",
                "status": LogisticsTrip.STATUS_LOADING,
                "vehicle_type": LogisticsTrip.VEHICLE_FULFILLMENT,
                "vehicle_name": "Газель 2",
                "carrier_id": str(self.carrier.id),
                "vehicle_number": "a321bc777",
                "driver_name": "Петров Петр Петрович",
                "driver_phone": "8 999 222-33-44",
                "route_comment": "Маршрут обновлен",
                "loading_comment": "Грузить с задней рампы",
            },
        )

        self.assertEqual(response.status_code, 302)
        trip.refresh_from_db()
        self.assertEqual(trip.trip_date, date(2026, 4, 5))
        self.assertEqual(trip.status, LogisticsTrip.STATUS_LOADING)
        self.assertEqual(trip.vehicle_type, LogisticsTrip.VEHICLE_FULFILLMENT)
        self.assertEqual(trip.vehicle_name, "Газель 2")
        self.assertEqual(trip.carrier, self.carrier)
        self.assertEqual(trip.vehicle_number, "А321ВС777")
        self.assertEqual(trip.driver_name, "Петров Петр Петрович")
        self.assertEqual(trip.driver_phone, "+7 999 222-33-44")
        self.assertEqual(trip.route_comment, "Маршрут обновлен")
        self.assertEqual(trip.loading_comment, "Грузить с задней рампы")

    def test_finalize_trip_requires_driver_data_and_creates_storekeeper_task(self):
        order = self._create_packed_order("SO-FINALIZE-1", pallet_count=2, box_count=10)
        trip = LogisticsTrip.objects.create(
            number="TRIP-000001",
            trip_date=date(2026, 4, 3),
            status=LogisticsTrip.STATUS_PLANNED,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        self.client.force_login(self.logistician_user)

        response = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={
                "action": "finalize_trip",
                "carrier_id": str(self.carrier.id),
                "vehicle_number": "a123bc777",
                "driver_name": "Иванов Иван Иванович",
                "driver_phone": "8 900 555-44-33",
            },
        )

        self.assertEqual(response.status_code, 302)
        trip.refresh_from_db()
        self.assertEqual(trip.status, LogisticsTrip.STATUS_LOADING)
        self.assertEqual(trip.carrier, self.carrier)
        self.assertEqual(trip.vehicle_number, "А123ВС777")
        task = Task.objects.get(route=f"/logistics/trips/{trip.pk}/", assigned_to=self.storekeeper)
        self.assertEqual(task.title, "Рейс №1_RS")
        self.assertIn("Иванов Иван Иванович", task.description)
        self.assertIn("+7 900 555-44-33", task.description)

    def test_logistician_cannot_edit_trip_after_transfer_to_storekeeper(self):
        order = self._create_packed_order("SO-LOCK-1", pallet_count=1, box_count=6)
        trip = LogisticsTrip.objects.create(
            number="3_RS",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            vehicle_number="А123ВС777",
            driver_name="Иванов Иван Иванович",
            driver_phone="+7 900 555-44-33",
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        self.client.force_login(self.logistician_user)

        get_response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))
        self.assertEqual(get_response.status_code, 200)
        self.assertContains(get_response, "Рейс передан в складской контур и заблокирован для любых правок.")
        self.assertNotContains(get_response, "Сформировать рейс")
        self.assertNotContains(get_response, "Вернуть логисту на доработку")
        self.assertNotContains(get_response, "Добавить готовые заявки")

        post_response = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={
                "action": "update_loading_params",
                "carrier_id": str(self.carrier.id),
                "vehicle_number": "а999аа777",
                "driver_name": "Новый Водитель",
                "driver_phone": "8 900 777-66-55",
            },
        )

        self.assertEqual(post_response.status_code, 403)
        trip.refresh_from_db()
        self.assertEqual(trip.vehicle_number, "А123ВС777")
        self.assertEqual(trip.driver_name, "Иванов Иван Иванович")

    def test_storekeeper_can_return_trip_to_logistician_for_rework(self):
        order = self._create_packed_order("SO-RETURN-1", pallet_count=1, box_count=6)
        trip = LogisticsTrip.objects.create(
            number="3_RS",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            vehicle_number="А123ВС777",
            driver_name="Иванов Иван Иванович",
            driver_phone="+7 900 555-44-33",
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        storekeeper_task = Task.objects.create(
            title="Рейс №3_RS",
            description="Ожидает машину",
            route=f"/logistics/trips/{trip.pk}/",
            assigned_to=self.storekeeper,
            created_by=self.logistician_user,
        )
        self.client.force_login(self.storekeeper_user)

        get_response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))
        self.assertEqual(get_response.status_code, 200)
        self.assertContains(get_response, "Вернуть логисту на доработку")
        self.assertContains(
            get_response,
            "Если в составе или маршруте нужны изменения, сначала верни его логисту на доработку.",
        )

        post_response = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={"action": "return_to_logistic"},
        )

        self.assertEqual(post_response.status_code, 302)
        trip.refresh_from_db()
        storekeeper_task.refresh_from_db()
        self.assertEqual(trip.status, LogisticsTrip.STATUS_PLANNED)
        self.assertEqual(storekeeper_task.status, "done")

        logistician_task = Task.objects.exclude(pk=storekeeper_task.pk).get(
            route=f"/logistics/trips/{trip.pk}/",
            assigned_to=self.logistician,
        )
        self.assertEqual(logistician_task.status, "backlog")
        self.assertIn("вернул рейс 3_RS логисту на доработку", logistician_task.description)

        self.client.force_login(self.logistician_user)
        detail_response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))
        self.assertContains(detail_response, "Сформировать рейс")
        self.assertContains(detail_response, "Добавить готовые заявки")
        self.assertContains(detail_response, "<title>3_RS | Логистика | FULLBOX</title>", html=False)

    def test_storekeeper_can_open_trip_detail_readonly(self):
        order = self._create_packed_order("SO-FINALIZE-2", pallet_count=1, box_count=6)
        trip = LogisticsTrip.objects.create(
            number="TRIP-000002",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            vehicle_number="А123ВС777",
            driver_name="Иванов Иван Иванович",
            driver_phone="+7 900 555-44-33",
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        self.client.force_login(self.storekeeper_user)

        response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Вернуть логисту на доработку")
        self.assertNotContains(response, "Сформировать рейс")
        self.assertContains(response, 'href="/sklad/"', html=False)
        self.assertContains(response, "В кабинет")
        self.assertContains(response, f'/shipping/{order.pk}/documents/')
        self.assertNotContains(response, f'/shipping/{order.pk}/act/')

    def test_storekeeper_trip_detail_shows_loading_button(self):
        order = self._create_packed_order_with_pallets(
            "SO-LOAD-BTN-1",
            [
                {"label": "Паллета 1", "code": "SHIP-1-PAL-1", "boxes": [{"code": "BOX-1", "qty": 2}]},
            ],
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-000020",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        self.client.force_login(self.storekeeper_user)

        response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Приступить к погрузке")
        self.assertContains(response, reverse("logistics:trip-loading", args=[trip.pk]))

    def test_trip_loading_page_shows_orders_in_reverse_delivery_order(self):
        first_order = self._create_packed_order_with_pallets(
            "SO-LOAD-ORDER-1",
            [{"label": "Паллета 1", "code": "SO-LOAD-ORDER-1-PAL-1", "boxes": [{"code": "BOX-1", "qty": 1}]}],
        )
        second_order = self._create_packed_order_with_pallets(
            "SO-LOAD-ORDER-2",
            [{"label": "Паллета 1", "code": "SO-LOAD-ORDER-2-PAL-1", "boxes": [{"code": "BOX-2", "qty": 1}]}],
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-000021",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=first_order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=second_order,
            loading_sequence=2,
            delivery_sequence=2,
        )
        self.client.force_login(self.storekeeper_user)

        response = self.client.get(reverse("logistics:trip-loading", args=[trip.pk]))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("Погрузка рейса", content)
        self.assertLess(content.index("2_OTG"), content.index("1_OTG"))

    def test_storekeeper_start_loading_button_switches_trip_to_loading_and_redirects(self):
        order = self._create_packed_order_with_pallets(
            "SO-LOAD-START-1",
            [{"label": "Паллета 1", "code": "SO-LOAD-START-1-PAL-1", "boxes": [{"code": "BOX-1", "qty": 1}]}],
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-000021A",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_PLANNED,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        self.client.force_login(self.storekeeper_user)

        response = self.client.post(
            reverse("logistics:trip-detail", args=[trip.pk]),
            data={"action": "start_loading"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("logistics:trip-loading", args=[trip.pk]))
        trip.refresh_from_db()
        self.assertEqual(trip.status, LogisticsTrip.STATUS_LOADING)

    def test_trip_loading_scan_marks_wrong_order_red_and_correct_order_green(self):
        first_order = self._create_packed_order_with_pallets(
            "SO-LOAD-SCAN-1",
            [{"label": "Паллета 1", "code": "SO-LOAD-SCAN-1-PAL-1", "boxes": [{"code": "BOX-1", "qty": 1}]}],
        )
        second_order = self._create_packed_order_with_pallets(
            "SO-LOAD-SCAN-2",
            [{"label": "Паллета 1", "code": "SO-LOAD-SCAN-2-PAL-1", "boxes": [{"code": "BOX-2", "qty": 1}]}],
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-000022",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=first_order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=second_order,
            loading_sequence=2,
            delivery_sequence=2,
        )
        self.client.force_login(self.storekeeper_user)

        wrong_response = self.client.post(
            reverse("logistics:trip-loading", args=[trip.pk]),
            data={"scan_value": "SO-LOAD-SCAN-1::SO-LOAD-SCAN-1-PAL-1"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(wrong_response.status_code, 409)
        wrong_payload = wrong_response.json()
        self.assertFalse(wrong_payload["result"]["ok"])
        self.assertEqual(wrong_payload["result"]["tone"], "error")
        self.assertIn("Сейчас очередь заявки LOAD-SCAN-2_OTG", wrong_payload["result"]["message"])

        ok_response = self.client.post(
            reverse("logistics:trip-loading", args=[trip.pk]),
            data={"scan_value": "SO-LOAD-SCAN-2::SO-LOAD-SCAN-2-PAL-1"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(ok_response.status_code, 200)
        ok_payload = ok_response.json()
        self.assertTrue(ok_payload["result"]["ok"])
        self.assertEqual(ok_payload["result"]["tone"], "success")
        self.assertEqual(ok_payload["loaded_total"], 1)
        self.assertEqual(ok_payload["current_order_number"], "LOAD-SCAN-1_OTG")

        audit_entry = OrderAuditEntry.objects.filter(
            order_id=str(trip.pk),
            order_type="logistics_trip",
            payload__act="trip_loading_progress",
        ).first()
        self.assertIsNotNone(audit_entry)
        self.assertEqual(audit_entry.payload["loaded_pallet_keys"], ["SO-LOAD-SCAN-2-PAL-1"])

    def test_trip_loading_page_renders_scanner_agent_block(self):
        order = self._create_packed_order_with_pallets(
            "SO-LOAD-AGENT-1",
            [{"label": "Паллета 1", "code": "SO-LOAD-AGENT-1-PAL-1", "boxes": [{"code": "BOX-1", "qty": 1}]}],
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-000023",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        DeviceAgent.objects.create(
            agent_id="trip-loading-agent-1",
            name="Тестовый агент",
            host="workstation-1",
            version="1.0.0",
            last_seen=timezone.now(),
            meta={"com_status": {"connected": True, "enabled": True, "port": "COM7"}},
        )
        self.client.force_login(self.storekeeper_user)

        response = self.client.get(reverse("logistics:trip-loading", args=[trip.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Сканер")
        self.assertContains(response, "trip-loading-agent-1")
        self.assertContains(response, "Перехватить сканер")

    def test_trip_loading_page_shows_documents_and_finish_button_after_full_loading(self):
        order = self._create_packed_order_with_pallets(
            "SO-LOAD-DOCS-1",
            [{"label": "Паллета 1", "code": "SO-LOAD-DOCS-1-PAL-1", "boxes": [{"code": "BOX-1", "qty": 1}]}],
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-000024",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
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
                "loaded_pallet_keys": ["SO-LOAD-DOCS-1-PAL-1"],
                "last_loaded_key": "SO-LOAD-DOCS-1-PAL-1",
            },
        )
        self.client.force_login(self.storekeeper_user)

        response = self.client.get(reverse("logistics:trip-loading", args=[trip.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Погрузка завершена")
        self.assertContains(response, "Транспортная накладная")
        self.assertContains(response, "Акт возврата")
        self.assertContains(response, "Завершить погрузку")
        self.assertContains(response, f'/shipping/{order.pk}/transport-note/docx/')
        self.assertContains(response, f'/shipping/{order.pk}/return-act/doc/')

    def test_trip_detail_shows_loaded_status_for_fully_loaded_order(self):
        order = self._create_packed_order_with_pallets(
            "SO-LOAD-STATUS-1",
            [{"label": "Паллета 1", "code": "SO-LOAD-STATUS-1-PAL-1", "boxes": [{"code": "BOX-1", "qty": 1}]}],
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-000024A",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
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
                "loaded_pallet_keys": ["SO-LOAD-STATUS-1-PAL-1"],
                "last_loaded_key": "SO-LOAD-STATUS-1-PAL-1",
            },
        )
        self.client.force_login(self.storekeeper_user)

        response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Загружено в машину")
        self.assertNotContains(response, "Подготовка к рейсу")

    def test_trip_detail_shows_loaded_status_from_warehouse_without_audit_progress(self):
        pallet_code = "SO-LOAD-STATUS-WH-1-PAL-1"
        order = self._create_packed_order_with_warehouse_pallet("SO-LOAD-STATUS-WH-1", pallet_code=pallet_code)
        trip = LogisticsTrip.objects.create(
            number="TRIP-000024WH",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        WarehouseWritePathService.assign_to_trip(
            agency=self.agency,
            order_id=order.number,
            trip_id=trip.number,
            assigned_by=self.logistician_user,
        )
        loading = WarehouseWritePathService.start_loading(
            agency=self.agency,
            order_id=order.number,
            trip_id=trip.number,
            started_by=self.storekeeper_user,
        )
        WarehouseWritePathService.complete_loading(operation=loading, performed_by=self.storekeeper_user)
        self.client.force_login(self.storekeeper_user)

        response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Загружено в машину")
        self.assertNotContains(response, "Подготовка к рейсу")

    def test_departed_trip_detail_hides_loading_button_and_shows_view_banner(self):
        order = self._create_packed_order_with_pallets(
            "SO-LOAD-DEPARTED-1",
            [{"label": "Паллета 1", "code": "SO-LOAD-DEPARTED-1-PAL-1", "boxes": [{"code": "BOX-1", "qty": 1}]}],
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-000024B",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_DEPARTED,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        self.client.force_login(self.storekeeper_user)

        response = self.client.get(reverse("logistics:trip-detail", args=[trip.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Погрузка завершена")
        self.assertContains(response, "доступен только просмотр состава рейса")
        self.assertNotContains(response, "Приступить к погрузке")

    def test_storekeeper_can_finish_loading_and_trip_moves_to_departed(self):
        order = self._create_packed_order_with_pallets(
            "SO-LOAD-FINISH-1",
            [{"label": "Паллета 1", "code": "SO-LOAD-FINISH-1-PAL-1", "boxes": [{"code": "BOX-1", "qty": 1}]}],
        )
        trip = LogisticsTrip.objects.create(
            number="TRIP-000025",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        storekeeper_task = Task.objects.create(
            title="Рейс №25_RS",
            description="Погрузка рейса",
            route=f"/logistics/trips/{trip.pk}/",
            assigned_to=self.storekeeper,
            created_by=self.logistician_user,
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
                "loaded_pallet_keys": ["SO-LOAD-FINISH-1-PAL-1"],
                "last_loaded_key": "SO-LOAD-FINISH-1-PAL-1",
            },
        )
        self.client.force_login(self.storekeeper_user)

        response = self.client.post(
            reverse("logistics:trip-loading", args=[trip.pk]),
            data={"action": "finish_loading"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("logistics:trip-detail", args=[trip.pk]))
        trip.refresh_from_db()
        storekeeper_task.refresh_from_db()
        self.assertEqual(trip.status, LogisticsTrip.STATUS_DEPARTED)
        self.assertEqual(storekeeper_task.status, "done")

    def test_finish_loading_syncs_warehouse_snapshots_to_loaded_vehicle(self):
        pallet_code = "SO-LOAD-WH-1-PAL-1"
        order = self._create_packed_order_with_warehouse_pallet("SO-LOAD-WH-1", pallet_code=pallet_code)
        trip = LogisticsTrip.objects.create(
            number="TRIP-000026",
            trip_date=date(2026, 4, 4),
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=self.logistician,
            created_by=self.logistician_user,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        storekeeper_task = Task.objects.create(
            title="Рейс №26_RS",
            description="Погрузка рейса",
            route=f"/logistics/trips/{trip.pk}/",
            assigned_to=self.storekeeper,
            created_by=self.logistician_user,
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
                "loaded_pallet_keys": [pallet_code],
                "last_loaded_key": pallet_code,
            },
        )
        self.client.force_login(self.storekeeper_user)

        response = self.client.post(
            reverse("logistics:trip-loading", args=[trip.pk]),
            data={"action": "finish_loading"},
        )

        self.assertEqual(response.status_code, 302)
        trip.refresh_from_db()
        storekeeper_task.refresh_from_db()
        snapshot = WarehouseStockSnapshot.objects.get(
            agency=self.agency,
            parent_container__container_code=pallet_code,
        )
        self.assertEqual(trip.status, LogisticsTrip.STATUS_DEPARTED)
        self.assertEqual(storekeeper_task.status, "done")
        self.assertEqual(snapshot.current_trip_id, trip.number)
        self.assertEqual(snapshot.warehouse_state_code, "loaded_to_vehicle")
        self.assertTrue(snapshot.is_in_vehicle)
