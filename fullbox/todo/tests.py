from datetime import timedelta

from django.contrib.auth import get_user_model
from django.template import Context, RequestContext, Template
from django.test import RequestFactory
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from audit.models import OrderAuditEntry
from employees.models import Employee
from logistics.models import LogisticsTrip, LogisticsTripOrder
from shipping.models import ShippingOrder
from reachtruck.models import MoveRequest, MoveTask
from sku.models import Agency
from sklad.models import WarehouseReserve, WarehouseStockSnapshot
from sklad.services.warehouse_write_path import WarehouseWritePathService

from .models import Task
from .services import (
    build_task_list_queryset,
    build_trip_context,
    can_access_task,
    send_receiving_to_warehouse,
)


class TodoDisplayTitleTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_display_title_formats_shipping_order_number(self):
        agency = Agency.objects.create(agn_name="Клиент todo")
        order = ShippingOrder.objects.create(
            number="SO-000001",
            agency=agency,
        )
        task = Task.objects.create(
            title="Проверьте заявку на отгрузку №SO-000001",
            route=f"/shipping/{order.pk}/",
        )

        self.assertEqual(task.display_title(), "Заявка на отгрузку №1_OTG")

    def test_display_title_formats_storekeeper_shipping_title(self):
        agency = Agency.objects.create(agn_name="Клиент shipping sklad")
        order = ShippingOrder.objects.create(
            number="SO-000001",
            agency=agency,
        )
        task = Task.objects.create(
            title="Подготовьте заявку на отгрузку №SO-000001",
            route=f"/shipping/{order.pk}/",
        )

        self.assertEqual(task.display_title(), "Заявка на отгрузку №1_OTG")

    def test_display_title_formats_receiving_order_number(self):
        agency = Agency.objects.create(agn_name="Клиент приемки")
        OrderAuditEntry.objects.create(
            order_id="3",
            order_type="receiving",
            action="status",
            agency=agency,
            payload={"status": "warehouse", "items": [{"sku_code": "SKU-1", "qty": "10"}]},
        )
        task = Task.objects.create(
            title="Принять заявку на приемку товара №3",
            route="/orders/receiving/3/",
        )

        self.assertEqual(task.display_title(), "Заявка на приемку №3_PR")

    def test_display_title_formats_processing_order_number(self):
        task = Task.objects.create(
            title="Заявка на обработку №7",
            route="/orders/processing/7/",
        )

        self.assertEqual(task.display_title(), "Заявка на обработку №7_OBR")

    def test_task_panel_shows_client_badge_for_shipping_review(self):
        agency = Agency.objects.create(agn_name="Индивидуальный предприниматель Опра Сергей Николаевич")
        manager = Employee.objects.create(full_name="Менеджеров Сергей", role="manager")
        order = ShippingOrder.objects.create(
            number="SO-000001",
            agency=agency,
        )
        Task.objects.create(
            title="Проверьте заявку на отгрузку №SO-000001",
            route=f"/shipping/{order.pk}/",
            status="blocked",
            assigned_to=manager,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='manager' %}"
        ).render(Context({}))

        self.assertIn('<span class="task-meta-label">Клиент:</span>', html)
        self.assertIn("ИП Опра Сергей Николаевич", html)
        self.assertIn(f"onclick=\"window.location.href='/shipping/{order.pk}/'\"", html)
        self.assertIn(f'<a href="/shipping/{order.pk}/">Заявка на отгрузку №1_OTG</a>', html)
        self.assertNotIn("Постановщик:", html)
        self.assertNotIn('<span class="task-tag">Клиент: ИП Опра Сергей Николаевич</span>', html)

    def test_task_panel_shows_shipping_status_label(self):
        agency = Agency.objects.create(agn_name="Клиент со статусом")
        manager = Employee.objects.create(full_name="Менеджеров Сергей", role="manager")
        order = ShippingOrder.objects.create(
            number="SO-000002",
            agency=agency,
            status=ShippingOrder.STATUS_RESERVED,
        )
        Task.objects.create(
            title="Проверьте заявку на отгрузку №SO-000002",
            route=f"/shipping/{order.pk}/",
            status="done",
            assigned_to=manager,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='manager' %}"
        ).render(Context({}))

        self.assertIn("Статус заявки:", html)
        self.assertIn("Согласована и передана в работу кладовщику", html)

    def test_task_panel_shows_storekeeper_accepted_shipping_status_label(self):
        agency = Agency.objects.create(agn_name="Клиент склада")
        storekeeper = Employee.objects.create(full_name="Кладовщиков Алексей", role="storekeeper")
        order = ShippingOrder.objects.create(
            number="SO-000003",
            agency=agency,
            status=ShippingOrder.STATUS_STOREKEEPER_ACCEPTED,
        )
        Task.objects.create(
            title="Подготовьте заявку на отгрузку №SO-000003",
            route=f"/shipping/{order.pk}/",
            status="in_progress",
            assigned_to=storekeeper,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='storekeeper' %}"
        ).render(Context({}))

        self.assertIn("Статус заявки:", html)
        self.assertIn("Принята в работу складом", html)

    def test_task_panel_shows_preparing_for_trip_status_for_packed_shipping_order(self):
        agency = Agency.objects.create(agn_name="Клиент рейса")
        storekeeper = Employee.objects.create(full_name="Кладовщиков Алексей", role="storekeeper")
        logistician = Employee.objects.create(full_name="Логистов Сергей", role="logistician")
        order = ShippingOrder.objects.create(
            number="SO-000004",
            agency=agency,
            status=ShippingOrder.STATUS_PACKED,
        )
        trip = LogisticsTrip.objects.create(
            number="7_RS",
            status=LogisticsTrip.STATUS_PLANNED,
            assigned_logistician=logistician,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        Task.objects.create(
            title="Подготовьте заявку на отгрузку №SO-000004",
            route=f"/shipping/{order.pk}/",
            status="in_progress",
            assigned_to=storekeeper,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='storekeeper' %}"
        ).render(Context({}))

        self.assertIn("Подготовка к рейсу", html)
        self.assertNotIn("Короба на новых паллетах", html)

    def test_task_panel_shows_loaded_for_trip_status_for_departed_shipping_order(self):
        agency = Agency.objects.create(agn_name="Клиент в пути")
        storekeeper = Employee.objects.create(full_name="Кладовщиков Алексей", role="storekeeper")
        logistician = Employee.objects.create(full_name="Логистов Сергей", role="logistician")
        order = ShippingOrder.objects.create(
            number="SO-000004A",
            agency=agency,
            status=ShippingOrder.STATUS_PACKED,
        )
        trip = LogisticsTrip.objects.create(
            number="8_RS",
            status=LogisticsTrip.STATUS_DEPARTED,
            assigned_logistician=logistician,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        Task.objects.create(
            title="Подготовьте заявку на отгрузку №SO-000004A",
            route=f"/shipping/{order.pk}/",
            status="in_progress",
            assigned_to=storekeeper,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='storekeeper' %}"
        ).render(Context({}))

        self.assertIn("Загружено в машину", html)
        self.assertNotIn("Короба на новых паллетах", html)

    def test_task_panel_shows_otg_palletizing_status_when_boxes_delivered(self):
        agency = Agency.objects.create(agn_name="Клиент OTG")
        storekeeper = Employee.objects.create(full_name="Кладовщиков Алексей", role="storekeeper")
        order = ShippingOrder.objects.create(
            number="SO-000004B",
            agency=agency,
            status=ShippingOrder.STATUS_PICKING,
        )
        OrderAuditEntry.objects.create(
            agency=agency,
            order_type="receiving",
            order_id="R-TODO-1",
            action="placement",
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {
                        "code": "BX-TODO-1",
                        "qty": 12,
                        "items": [
                            {
                                "sku_code": "SKU-TODO-1",
                                "name": "Товар OTG",
                                "size": "42",
                                "barcode": "200000000401",
                                "goods_type": "Готовый",
                                "qty": 12,
                            }
                        ],
                    }
                ],
            },
        )
        move_request = MoveRequest.objects.create(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id="",
            agency=agency,
            destination_zone="OTG",
            status=MoveRequest.STATUS_DONE,
        )
        MoveTask.objects.create(
            request=move_request,
            pallet_code="PL-TODO-OTG",
            to_zone="OTG",
            status=MoveTask.STATUS_DONE,
            payload={
                "shipping_order_id": order.number,
                "shipping_order_pk": order.pk,
                "receiving_order_id": "R-TODO-1",
                "picked_boxes": ["BX-TODO-1"],
                "picked_rows": [{"box_code": "BX-TODO-1", "qty": 12}],
            },
        )
        Task.objects.create(
            title="Подготовьте заявку на отгрузку №SO-000004B",
            route=f"/shipping/{order.pk}/",
            status="in_progress",
            assigned_to=storekeeper,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='storekeeper' %}"
        ).render(Context({}))

        self.assertIn("Товар доставлен в OTG, ожидает паллетизации", html)
        self.assertNotIn("Доставка в зону отгрузки (ричтрак)", html)

    def test_task_panel_deduplicates_shipping_order_tasks_for_manager(self):
        agency = Agency.objects.create(agn_name="Клиент отгрузки")
        manager = Employee.objects.create(full_name="Менеджеров Сергей", role="manager")
        storekeeper = Employee.objects.create(full_name="Кладовщиков Алексей", role="storekeeper")
        order = ShippingOrder.objects.create(
            number="SO-000005",
            agency=agency,
            status=ShippingOrder.STATUS_PACKED,
        )
        Task.objects.create(
            title="Заявка на отгрузку №SO-000005",
            route=f"/shipping/{order.pk}/",
            status="done",
            assigned_to=manager,
        )
        Task.objects.create(
            title="Заявка на отгрузку №SO-000005",
            route=f"/shipping/{order.pk}/",
            status="done",
            assigned_to=storekeeper,
            observer=manager,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='manager' %}"
        ).render(Context({}))

        self.assertEqual(html.count("Заявка на отгрузку №5_OTG"), 1)
        self.assertEqual(html.count("Подготовлена складом, ожидает логиста"), 1)

    def test_task_panel_prefers_open_shipping_act_task_for_manager(self):
        agency = Agency.objects.create(agn_name="Клиент акта отгрузки")
        manager = Employee.objects.create(full_name="Менеджеров Сергей", role="manager")
        order = ShippingOrder.objects.create(
            number="SO-000006",
            agency=agency,
            status=ShippingOrder.STATUS_PACKED,
        )
        Task.objects.create(
            title="Заявка на отгрузку №SO-000006",
            route=f"/shipping/{order.pk}/",
            status="done",
            assigned_to=manager,
        )
        Task.objects.create(
            title="Подписать акт отгрузки №SO-000006",
            route=f"/shipping/{order.pk}/act/",
            status="in_progress",
            assigned_to=manager,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='manager' %}"
        ).render(Context({}))

        self.assertEqual(html.count("Заявка на отгрузку №6_OTG"), 1)
        self.assertIn(f'href="/shipping/{order.pk}/act/"', html)
        self.assertNotIn(f'href="/shipping/{order.pk}/">Заявка на отгрузку №6_OTG</a>', html)

    def test_task_panel_deduplicates_receiving_order_and_sign_task_for_manager(self):
        agency = Agency.objects.create(agn_name="Клиент приемки")
        manager = Employee.objects.create(full_name="Менеджеров Сергей", role="manager")
        OrderAuditEntry.objects.create(
            order_id="1",
            order_type="receiving",
            action="status",
            agency=agency,
            payload={"status": "warehouse", "items": [{"sku_code": "SKU-1", "qty": "10"}]},
        )
        Task.objects.create(
            title="Проверьте размещение по заявке на приемку товара №1",
            route="/orders/receiving/1/",
            status="backlog",
            assigned_to=manager,
        )
        Task.objects.create(
            title="Подписать акт приемки по заявке №1",
            route="/orders/receiving/1/act/print/",
            status="in_progress",
            assigned_to=manager,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='manager' %}"
        ).render(Context({}))

        self.assertEqual(html.count("Заявка на приемку №1_PR"), 1)
        self.assertIn('href="/orders/receiving/1/act/print/"', html)
        self.assertNotIn('href="/orders/receiving/1/"', html)

    def test_task_panel_filters_by_order_type(self):
        agency = Agency.objects.create(agn_name="Клиент фильтра типов")
        storekeeper = Employee.objects.create(full_name="Кладовщиков Алексей", role="storekeeper")
        shipping_order = ShippingOrder.objects.create(number="SO-000077", agency=agency)
        OrderAuditEntry.objects.create(
            order_id="11",
            order_type="receiving",
            action="status",
            agency=agency,
            payload={"status": "warehouse", "items": [{"sku_code": "SKU-11", "qty": "10"}]},
        )
        Task.objects.create(
            title="Приемка",
            route="/orders/receiving/11/",
            status="in_progress",
            assigned_to=storekeeper,
        )
        Task.objects.create(
            title="Отгрузка",
            route=f"/shipping/{shipping_order.pk}/",
            status="in_progress",
            assigned_to=storekeeper,
        )
        OrderAuditEntry.objects.create(
            order_id="22",
            order_type="processing",
            action="status",
            agency=agency,
            payload={"status": "in_progress", "status_label": "Взята в работу"},
        )
        Task.objects.create(
            title="Обработка",
            route="/orders/processing/22/",
            status="in_progress",
            assigned_to=storekeeper,
        )

        request = self.factory.get(
            "/team-storekeeper/",
            {"todo_filters_applied": "1", "todo_filter_type": "shipping"},
        )
        html = Template(
            "{% load todo_panel %}{% task_panel role='storekeeper' %}"
        ).render(RequestContext(request, {}))

        self.assertIn("Заявка на отгрузку", html)
        self.assertNotIn("Заявка на приемку №11_PR", html)
        self.assertNotIn("Заявка на обработку №22_OBR", html)
        self.assertNotIn('value="processing"', html)

    def test_task_panel_filters_by_client(self):
        client_a = Agency.objects.create(agn_name="Клиент А")
        client_b = Agency.objects.create(agn_name="Клиент Б")
        manager = Employee.objects.create(full_name="Менеджеров Сергей", role="manager")
        order_a = ShippingOrder.objects.create(number="SO-000010", agency=client_a)
        order_b = ShippingOrder.objects.create(number="SO-000011", agency=client_b)
        Task.objects.create(
            title="Отгрузка А",
            route=f"/shipping/{order_a.pk}/",
            status="in_progress",
            assigned_to=manager,
        )
        Task.objects.create(
            title="Отгрузка Б",
            route=f"/shipping/{order_b.pk}/",
            status="in_progress",
            assigned_to=manager,
        )

        request = self.factory.get(
            "/team-manager/",
            {
                "todo_filters_applied": "1",
                "todo_filter_type": "all",
                "todo_filter_client": str(client_b.id),
            },
        )
        html = Template(
            "{% load todo_panel %}{% task_panel role='manager' %}"
        ).render(RequestContext(request, {}))

        self.assertIn("Клиент Б", html)
        self.assertIn("Заявка на отгрузку №11_OTG", html)
        self.assertNotIn("Заявка на отгрузку №10_OTG", html)

    def test_processing_head_task_panel_routes_placement_completed_order_to_work_page(self):
        agency = Agency.objects.create(agn_name="Клиент обработки")
        processing_head = Employee.objects.create(full_name="Руководитель обработки", role="processing_head")
        OrderAuditEntry.objects.create(
            order_id="44",
            order_type="processing",
            action="update",
            agency=agency,
            payload={
                "act": "placement",
                "act_state": "closed",
                "flow_closed": True,
                "status": "processing_in_work",
            },
        )
        Task.objects.create(
            title="Заявка на обработку №44",
            route="/orders/processing/44/",
            status="in_progress",
            assigned_to=processing_head,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='processing_head' %}"
        ).render(Context({}))

        self.assertIn('onclick="window.location.href=\'/orders/processing/44/work/\'"', html)
        self.assertIn('<a href="/orders/processing/44/work/">Заявка на обработку №44_OBR</a>', html)
        self.assertIn("Размещение завершено", html)

    def test_processing_task_panel_prefers_warehouse_status_over_stale_payload(self):
        agency = Agency.objects.create(agn_name="Клиент обработки склад")
        processing_head = Employee.objects.create(full_name="Руководитель обработки", role="processing_head")
        processing_location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OBR",
        )
        OrderAuditEntry.objects.create(
            order_id="45",
            order_type="processing",
            action="status",
            agency=agency,
            payload={
                "status": "processing_head",
                "status_label": "Передано в обработку",
            },
        )
        snapshot = WarehouseStockSnapshot.objects.create(
            agency=agency,
            source_context_type="legacy_stock",
            source_context_id="legacy-45",
            sku_code="SKU-PROCESS-TODO",
            name="Товар обработки",
            size="42",
            barcode="200000001045",
            goods_type="gv",
            qty=10,
            available_qty=0,
            processing_reserved_qty=10,
            container_code="PAL-PROC-TODO-45",
            location=processing_location,
            zone_code=processing_location.zone_code,
            zone_kind=processing_location.zone_kind,
            warehouse_state_code="processing_in_progress",
        )
        WarehouseReserve.objects.create(
            agency=agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="45",
            sku_code=snapshot.sku_code,
            size=snapshot.size,
            barcode=snapshot.barcode,
            goods_type=snapshot.goods_type,
            qty_reserved=10,
            status=WarehouseReserve.STATUS_ACTIVE,
        )
        Task.objects.create(
            title="Заявка на обработку №45",
            route="/orders/processing/45/",
            status="in_progress",
            assigned_to=processing_head,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='processing_head' %}"
        ).render(Context({}))

        self.assertIn("Товар в обработке", html)

    def test_storekeeper_tabs_include_logistics_and_count_only_open_tasks(self):
        agency = Agency.objects.create(agn_name="Клиент рейсов")
        storekeeper = Employee.objects.create(full_name="Кладовщиков Алексей", role="storekeeper")
        logistician = Employee.objects.create(full_name="Логистов Сергей", role="logistician")
        shipping_order = ShippingOrder.objects.create(number="SO-000078", agency=agency)
        receiving_order_id = "31"
        OrderAuditEntry.objects.create(
            order_id=receiving_order_id,
            order_type="receiving",
            action="status",
            agency=agency,
            payload={"status": "warehouse", "items": [{"sku_code": "SKU-31", "qty": "10"}]},
        )
        Task.objects.create(
            title="Приемка",
            route=f"/orders/receiving/{receiving_order_id}/",
            status="in_progress",
            assigned_to=storekeeper,
        )
        Task.objects.create(
            title="Отгрузка",
            route=f"/shipping/{shipping_order.pk}/",
            status="done",
            assigned_to=storekeeper,
        )
        trip = LogisticsTrip.objects.create(
            number="3_RS",
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=logistician,
        )
        Task.objects.create(
            title="Рейс №3_RS",
            route=f"/logistics/trips/{trip.pk}/",
            status="backlog",
            assigned_to=storekeeper,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='storekeeper' %}"
        ).render(Context({}))

        self.assertRegex(html, r'(?s)name="todo_filter_type".*?value="logistics"')
        self.assertIn("Рейсы", html)
        self.assertRegex(html, r'(?s)value="all".*?task-filter-tab-count">\((2)\)</span>')
        self.assertRegex(html, r'(?s)value="receiving".*?task-filter-tab-count">\((1)\)</span>')
        self.assertRegex(html, r'(?s)value="shipping".*?task-filter-tab-count">\((0)\)</span>')
        self.assertRegex(html, r'(?s)value="logistics".*?task-filter-tab-count">\((1)\)</span>')

    def test_head_manager_tabs_include_logistics_and_filter_trip_tasks(self):
        agency = Agency.objects.create(agn_name="Клиент логистики ГМ")
        head_manager = Employee.objects.create(full_name="Главменеджеров Павел", role="head_manager")
        logistician = Employee.objects.create(full_name="Логистов Сергей", role="logistician")
        shipping_order = ShippingOrder.objects.create(number="SO-000079", agency=agency)
        OrderAuditEntry.objects.create(
            order_id="41",
            order_type="processing",
            action="status",
            agency=agency,
            payload={"status": "in_progress", "status_label": "Взята в работу"},
        )
        Task.objects.create(
            title="Обработка",
            route="/orders/processing/41/",
            status="in_progress",
            assigned_to=head_manager,
        )
        Task.objects.create(
            title="Отгрузка",
            route=f"/shipping/{shipping_order.pk}/",
            status="in_progress",
            assigned_to=head_manager,
        )
        trip = LogisticsTrip.objects.create(
            number="5_RS",
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=logistician,
        )
        Task.objects.create(
            title="Рейс №5_RS",
            route=f"/logistics/trips/{trip.pk}/",
            status="backlog",
            assigned_to=head_manager,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='head_manager' %}"
        ).render(Context({}))

        self.assertRegex(html, r'(?s)name="todo_filter_type".*?value="logistics"')
        self.assertIn("Рейсы", html)
        self.assertRegex(html, r'(?s)value="processing".*?task-filter-tab-count">\((1)\)</span>')
        self.assertRegex(html, r'(?s)value="shipping".*?task-filter-tab-count">\((1)\)</span>')
        self.assertRegex(html, r'(?s)value="logistics".*?task-filter-tab-count">\((1)\)</span>')

        request = self.factory.get(
            "/head-manager/",
            {"todo_filters_applied": "1", "todo_filter_type": "logistics"},
        )
        filtered_html = Template(
            "{% load todo_panel %}{% task_panel role='head_manager' %}"
        ).render(RequestContext(request, {}))

        self.assertIn("Рейс №5_RS", filtered_html)
        self.assertNotIn("Заявка на отгрузку №79_OTG", filtered_html)
        self.assertNotIn("Заявка на обработку №41_OBR", filtered_html)

    def test_task_panel_shows_logistics_status_label(self):
        storekeeper = Employee.objects.create(full_name="Кладовщиков Алексей", role="storekeeper")
        logistician = Employee.objects.create(full_name="Логистов Сергей", role="logistician")
        trip = LogisticsTrip.objects.create(
            number="3_RS",
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=logistician,
        )
        Task.objects.create(
            title="Рейс №3_RS",
            route=f"/logistics/trips/{trip.pk}/",
            status="backlog",
            assigned_to=storekeeper,
        )

        html = Template(
            "{% load todo_panel %}{% task_panel role='storekeeper' %}"
        ).render(Context({}))

        self.assertIn("Статус рейса:", html)
        self.assertIn("Погрузка", html)


class TodoReturnUrlTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="director_todo_return",
            password="pwd",
        )
        Employee.objects.create(
            user=self.user,
            full_name="Директор Иван",
            role="director",
        )
        self.client.force_login(self.user)

    def test_create_redirects_back_to_next_url(self):
        response = self.client.post(
            reverse("todo:create"),
            {
                "title": "Проверить возврат в кабинет",
                "description": "Тестовая задача",
                "assigned_to": "",
                "observer": "",
                "priority": "normal",
                "due_date": timezone.localtime(timezone.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
                "next": "/cabinet/director/",
            },
        )

        self.assertRedirects(response, "/cabinet/director/")
        task = Task.objects.get(title="Проверить возврат в кабинет")
        self.assertEqual(task.created_by, self.user)

    def test_create_form_uses_next_url_in_actions(self):
        response = self.client.get(reverse("todo:create"), {"next": "/cabinet/director/"})

        self.assertContains(response, 'href="/cabinet/director/"')
        self.assertContains(response, 'name="next" value="/cabinet/director/"', html=False)

    def test_cabinet_task_panel_create_link_keeps_origin(self):
        request = RequestFactory().get("/cabinet/director/?tab=tasks")
        request.user = self.user

        html = Template(
            "{% load todo_panel %}{% task_panel role='director' %}"
        ).render(RequestContext(request, {}))

        self.assertIn('href="/todo/new/?next=/cabinet/director/%3Ftab%3Dtasks"', html)


class TodoServiceLayerTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        user_model = get_user_model()
        self.manager_user = user_model.objects.create_user(
            username="todo_service_manager",
            password="pwd",
        )
        self.creator_user = user_model.objects.create_user(
            username="todo_service_creator",
            password="pwd",
        )
        self.manager = Employee.objects.create(
            user=self.manager_user,
            full_name="Менеджеров Сергей",
            role="manager",
            is_active=True,
        )
        self.observer = Employee.objects.create(
            full_name="Наблюдатель Мария",
            role="manager",
            is_active=True,
        )
        self.storekeeper = Employee.objects.create(
            full_name="Кладовщиков Алексей",
            role="storekeeper",
            is_active=True,
        )

    def test_build_task_list_queryset_returns_only_visible_tasks(self):
        assigned_task = Task.objects.create(
            title="Назначенная задача",
            assigned_to=self.manager,
        )
        observer_task = Task.objects.create(
            title="Наблюдаемая задача",
            observer=self.manager,
        )
        creator_task = Task.objects.create(
            title="Созданная мной задача",
            created_by=self.manager_user,
        )
        Task.objects.create(
            title="Чужая задача",
            assigned_to=self.storekeeper,
        )
        request = self.factory.get("/todo/")
        request.user = self.manager_user

        tasks = build_task_list_queryset(request=request, role="manager")

        self.assertCountEqual(
            list(tasks.values_list("pk", flat=True)),
            [assigned_task.pk, observer_task.pk, creator_task.pk],
        )

    def test_can_access_task_allows_creator_and_rejects_unrelated_user(self):
        task = Task.objects.create(
            title="Проверка доступа",
            assigned_to=self.storekeeper,
            created_by=self.creator_user,
        )
        creator_request = self.factory.get(f"/todo/{task.pk}/")
        creator_request.user = self.creator_user
        stranger_request = self.factory.get(f"/todo/{task.pk}/")
        stranger_request.user = self.manager_user

        self.assertTrue(can_access_task(request=creator_request, task=task, role="manager"))
        self.assertFalse(can_access_task(request=stranger_request, task=task, role="manager"))

    def test_send_receiving_to_warehouse_closes_manager_task_and_creates_storekeeper_task(self):
        agency = Agency.objects.create(agn_name="Клиент приемки todo service")
        OrderAuditEntry.objects.create(
            order_id="R-TODO-SVC-1",
            order_type="receiving",
            action="create",
            agency=agency,
            payload={"status": "draft", "status_label": "Черновик"},
        )
        manager_task = Task.objects.create(
            title="Проверьте заявку на приемку товара №R-TODO-SVC-1",
            route="/orders/receiving/R-TODO-SVC-1/",
            assigned_to=self.manager,
            created_by=self.manager_user,
        )
        request = self.factory.post("/todo/detail/")
        request.user = self.manager_user

        result = send_receiving_to_warehouse(manager_task, request)

        self.assertTrue(result)
        manager_task.refresh_from_db()
        self.assertEqual(manager_task.status, "done")
        self.assertTrue(
            OrderAuditEntry.objects.filter(
                order_id="R-TODO-SVC-1",
                order_type="receiving",
                action="status",
                payload__status="warehouse",
            ).exists()
        )
        follow_up = Task.objects.exclude(pk=manager_task.pk).get(route="/orders/receiving/R-TODO-SVC-1/")
        self.assertEqual(follow_up.assigned_to, self.storekeeper)
        self.assertEqual(follow_up.created_by, self.manager_user)

    def test_build_trip_context_aggregates_trip_participants_and_totals(self):
        logistician = Employee.objects.create(
            full_name="Логистов Сергей",
            role="logistician",
            is_active=True,
        )
        agency = Agency.objects.create(agn_name="Клиент рейса todo service")
        order = ShippingOrder.objects.create(
            number="SO-000101",
            agency=agency,
            expected_boxes=8,
            destination_warehouse="Казань РФЦ",
        )
        trip = LogisticsTrip.objects.create(
            number="12_RS",
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=logistician,
        )
        LogisticsTripOrder.objects.create(
            trip=trip,
            shipping_order=order,
            loading_sequence=1,
            delivery_sequence=1,
        )
        OrderAuditEntry.objects.create(
            order_type="shipping",
            order_id=order.number,
            action="packing",
            payload={
                "act": "shipping_packing",
                "pallet_count": 2,
                "delivered_box_count": 5,
            },
        )
        task = Task.objects.create(
            title="Рейс на погрузку",
            route=f"/logistics/trips/{trip.pk}/",
            assigned_to=self.storekeeper,
            created_by=self.manager_user,
        )

        context = build_trip_context(task)

        self.assertIsNotNone(context)
        self.assertEqual(context["total_pallets"], 2)
        self.assertEqual(context["total_boxes"], 5)
        self.assertIn("Логист: Логистов Сергей", context["participants"])
        self.assertIn("Кладовщик: Кладовщиков Алексей", context["participants"])


class TodoTaskDetailTripLayoutTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="todo_trip_storekeeper", password="pwd")
        self.storekeeper = Employee.objects.create(
            full_name="Кладовщиков Алексей",
            role="storekeeper",
            user=self.user,
            is_active=True,
        )
        self.logistician = Employee.objects.create(
            full_name="Логистов Сергей",
            role="logistician",
            is_active=True,
        )
        agency = Agency.objects.create(agn_name='Общество с ограниченной ответственностью "Кейзи"')
        self.order1 = ShippingOrder.objects.create(
            number="SO-000001",
            agency=agency,
            destination_warehouse="Санкт_Петербург_РФЦ",
            expected_boxes=9,
        )
        self.order2 = ShippingOrder.objects.create(
            number="SO-000002",
            agency=agency,
            destination_warehouse="Екатеринбург — ул. Испытателей, 14Г",
            expected_boxes=50,
        )
        self.trip = LogisticsTrip.objects.create(
            number="3_RS",
            status=LogisticsTrip.STATUS_LOADING,
            vehicle_name="Транспорт ФуллБокс",
            vehicle_number="Е777ЕК999",
            driver_name="Иванов Иван",
            driver_phone="+7 900 000-00-00",
            assigned_logistician=self.logistician,
        )
        LogisticsTripOrder.objects.create(
            trip=self.trip,
            shipping_order=self.order1,
            loading_sequence=1,
            delivery_sequence=1,
            comment="Первая точка",
        )
        LogisticsTripOrder.objects.create(
            trip=self.trip,
            shipping_order=self.order2,
            loading_sequence=2,
            delivery_sequence=2,
            comment="Вторая точка",
        )
        self.task = Task.objects.create(
            title="Рейс на погрузку",
            route=f"/logistics/trips/{self.trip.pk}/",
            assigned_to=self.storekeeper,
        )
        self.client.force_login(self.user)

    def test_trip_task_detail_uses_wide_table_layout_without_right_sidebar(self):
        response = self.client.get(reverse("todo:detail", args=[self.task.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Заявки, включенные в рейс")
        self.assertContains(response, "Маршрут")
        self.assertContains(response, "Склад назначения")
        self.assertContains(response, "Комментарий логиста")
        self.assertContains(response, "1_OTG")
        self.assertContains(response, "2_OTG")
        self.assertContains(response, "Первая точка")
        self.assertContains(response, "Вторая точка")
        self.assertNotContains(response, 'class="detail-chat"', html=False)

    def test_storekeeper_task_panel_uses_direct_trip_link_for_logistics_task(self):
        request = RequestFactory().get("/sklad/")
        request.user = self.user

        html = Template(
            "{% load todo_panel %}{% task_panel role='storekeeper' %}"
        ).render(RequestContext(request, {}))

        detail_url = reverse("todo:detail", args=[self.task.pk])
        self.assertIn(f"onclick=\"window.location.href='/logistics/trips/{self.trip.pk}/'\"", html)
        self.assertIn(f'<a href="/logistics/trips/{self.trip.pk}/">{self.task.display_title()}</a>', html)
        self.assertNotIn(f'href="{detail_url}"', html)


class TodoPanelFilterPersistenceTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="manager_todo_filters",
            password="pwd",
        )
        self.manager = Employee.objects.create(
            user=self.user,
            full_name="Менеджеров Сергей",
            role="manager",
        )
        self.client_a = Agency.objects.create(agn_name="Клиент А")
        self.client_b = Agency.objects.create(agn_name="Клиент Б")
        self.order_a = ShippingOrder.objects.create(number="SO-000020", agency=self.client_a)
        self.order_b = ShippingOrder.objects.create(number="SO-000021", agency=self.client_b)
        Task.objects.create(
            title="Отгрузка А",
            route=f"/shipping/{self.order_a.pk}/",
            status="in_progress",
            assigned_to=self.manager,
        )
        Task.objects.create(
            title="Отгрузка Б",
            route=f"/shipping/{self.order_b.pk}/",
            status="in_progress",
            assigned_to=self.manager,
        )
        self.session_key = "todo_panel_filters:/team-manager/"

    def test_task_panel_restores_saved_filters_from_session(self):
        request = self.factory.get(
            "/team-manager/",
            {
                "todo_filters_applied": "1",
                "todo_filter_type": "shipping",
                "todo_filter_client": str(self.client_b.id),
            },
        )
        request.user = self.user
        request.session = {}

        initial_html = Template(
            "{% load todo_panel %}{% task_panel role='manager' %}"
        ).render(RequestContext(request, {}))

        self.assertIn("Клиент Б", initial_html)
        self.assertNotIn("Заявка на отгрузку №20_OTG", initial_html)
        self.assertEqual(
            request.session[self.session_key],
            {
                "selected_type": "shipping",
                "selected_client": str(self.client_b.id),
            },
        )

        repeat_request = self.factory.get("/team-manager/")
        repeat_request.user = self.user
        repeat_request.session = request.session

        repeat_html = Template(
            "{% load todo_panel %}{% task_panel role='manager' %}"
        ).render(RequestContext(repeat_request, {}))

        self.assertIn("Клиент Б", repeat_html)
        self.assertNotIn("Заявка на отгрузку №20_OTG", repeat_html)
        self.assertIn('option value="%s" selected' % self.client_b.id, repeat_html)
        self.assertIn('name="todo_filter_type" value="shipping"', repeat_html)
        self.assertIn('class="task-filter-tab active"', repeat_html)

    def test_task_panel_reset_clears_saved_filters(self):
        session = {
            self.session_key: {
                "selected_type": "shipping",
                "selected_client": str(self.client_b.id),
            }
        }
        request = self.factory.get("/team-manager/", {"todo_filters_reset": "1"})
        request.user = self.user
        request.session = session

        html = Template(
            "{% load todo_panel %}{% task_panel role='manager' %}"
        ).render(RequestContext(request, {}))

        self.assertNotIn(self.session_key, request.session)
        self.assertIn("Клиент А", html)
        self.assertIn("Клиент Б", html)
