import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, SimpleTestCase, TestCase

from audit.models import OrderAuditEntry
from employees.models import Employee
from marking.models import MarkingCode
from shipping.models import ShippingOrder, ShippingOrderItem
from sklad.services import WarehouseStateCode
from sklad.test_utils import create_warehouse_snapshot_row
from sku.models import Agency, SKU
from todo.models import Task
from .services import (
    build_agency_form_context,
    build_dashboard_context,
    build_client_list_context,
    build_client_list_queryset,
    build_client_sku_duplicate_initial,
    build_client_sku_form_context,
    build_client_sku_list_context,
    build_client_sku_list_queryset,
    build_client_receiving_form_context,
    build_marking_tools_context,
    fetch_party_by_inn_response,
    resolve_client_order_redirect_response,
    submit_client_packing_order,
    submit_client_receiving_order,
    toggle_agency_archive_response,
)
from .views import (
    ClientSKUListView,
    _inventory_check_for_agency,
    _order_bucket,
    _order_status_label,
    _receiving_act_needs_client_attention,
)


class ClientCabinetShippingStatusTests(SimpleTestCase):
    def test_shipping_submitted_status_is_shown_as_manager_review(self):
        entry = SimpleNamespace(
            order_type="shipping",
            payload={"shipping_state": "submitted"},
        )

        self.assertEqual(_order_status_label(entry), "На проверке у менеджера")
        self.assertEqual(_order_bucket(entry), "manager")

    def test_shipping_reserved_status_moves_to_warehouse_bucket(self):
        entry = SimpleNamespace(
            order_type="shipping",
            payload={"shipping_state": "reserved"},
        )

        self.assertEqual(_order_status_label(entry), "Согласована и передана в работу кладовщику")
        self.assertEqual(_order_bucket(entry), "warehouse")

    def test_shipping_storekeeper_accepted_status_is_shown_in_warehouse_bucket(self):
        entry = SimpleNamespace(
            order_type="shipping",
            payload={"shipping_state": "storekeeper_accepted"},
        )

        self.assertEqual(_order_status_label(entry), "Принята в работу складом")
        self.assertEqual(_order_bucket(entry), "warehouse")

    @patch("client_cabinet.views._shipping_trip_status", return_value="departed")
    def test_shipping_departed_trip_is_shown_as_loaded_in_vehicle(self, _trip_status_mock):
        entry = SimpleNamespace(
            order_type="shipping",
            order_id="SO-000123",
            payload={"shipping_state": "packed"},
        )

        self.assertEqual(_order_status_label(entry), "Загружено в машину")
        self.assertEqual(_order_bucket(entry), "done")

    def test_receiving_act_card_is_hidden_after_client_confirmation(self):
        act_entry = SimpleNamespace(payload={"act_sent": "Акт приемки"})
        status_entry = SimpleNamespace(payload={"act_client_response": "confirmed"})

        self.assertFalse(_receiving_act_needs_client_attention(act_entry, status_entry))

    def test_receiving_act_card_is_hidden_after_client_view(self):
        act_entry = SimpleNamespace(payload={"act_sent": "Акт приемки"})
        status_entry = SimpleNamespace(payload={"act_viewed": True})

        self.assertFalse(_receiving_act_needs_client_attention(act_entry, status_entry))


class ClientSKUListViewTests(TestCase):
    def test_defaults_to_table_view(self):
        user = get_user_model().objects.create_user(username="sku_client", password="pwd")
        agency = Agency.objects.create(agn_name="Клиент SKU", portal_user=user)
        request = RequestFactory().get(f"/client/{agency.pk}/sku/")
        request.user = user

        view = ClientSKUListView()
        view.request = request
        view.agency = agency
        view.kwargs = {}
        view.object_list = view.get_queryset()
        context = view.get_context_data()

        self.assertEqual(context["view_mode"], "table")


class ClientListServiceTests(TestCase):
    def test_build_client_list_queryset_applies_search_and_sort(self):
        staff_user = get_user_model().objects.create_user(username="staff_list", password="pwd", is_staff=True)
        Agency.objects.create(agn_name="Бета", inn="2")
        alpha = Agency.objects.create(agn_name="Альфа", inn="1")
        request = RequestFactory().get("/client/?q=льф&sort=id&dir=desc")
        request.user = staff_user

        queryset = build_client_list_queryset(
            request=request,
            base_queryset=Agency.objects.all(),
            sort_fields={"id": "id", "name": "agn_name"},
            filter_fields={"agn_name": "agn_name"},
            default_sort="name",
        )

        self.assertEqual(list(queryset), [alpha])

    def test_build_client_list_context_marks_active_sort(self):
        staff_user = get_user_model().objects.create_user(username="staff_ctx", password="pwd", is_staff=True)
        request = RequestFactory().get("/client/?sort=name&dir=asc")
        request.user = staff_user

        context = build_client_list_context(
            request=request,
            items=[],
            view_modes=("table", "cards"),
            sort_fields={"name": "agn_name", "id": "id"},
            default_sort="name",
        )

        self.assertEqual(context["view_mode"], "table")
        self.assertTrue(context["sort_info"]["name"]["active"])
        self.assertEqual(context["sort_info"]["name"]["next_dir"], "desc")


class ClientSKUListServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="sku_service_user", password="pwd")
        self.agency = Agency.objects.create(agn_name="Клиент SKU services", portal_user=self.user)
        self.factory = RequestFactory()

    def test_build_client_sku_list_queryset_filters_by_search(self):
        target = SKU.objects.create(agency=self.agency, sku_code="SKU-TARGET", name="Целевой товар")
        target.barcodes.create(value="200000000777", is_primary=True)
        SKU.objects.create(agency=self.agency, sku_code="SKU-OTHER", name="Другой товар")
        request = self.factory.get("/client/1/sku/?q=TARGET")
        request.user = self.user

        queryset = build_client_sku_list_queryset(request=request, agency=self.agency)

        self.assertEqual(list(queryset), [target])

    def test_build_client_sku_list_context_builds_filter_options_and_size_map(self):
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-CTX",
            name="Контекстный товар",
            brand="Brand One",
            size="42",
        )
        sku.barcodes.create(value="200000000888", size="42", is_primary=True)
        request = self.factory.get("/client/1/sku/?filter_brand=Brand+One")
        request.user = self.user

        context = build_client_sku_list_context(
            request=request,
            agency=self.agency,
            items=[sku],
            view_modes=("table", "cards"),
        )

        self.assertEqual(context["filter_values"]["brand"], "Brand One")
        self.assertEqual(context["brand_options"], ["Brand One"])
        self.assertEqual(context["size_options"], ["42"])
        self.assertIn("200000000888", sku.size_map_json)


class ClientCabinetAdminServiceTests(TestCase):
    def setUp(self):
        self.staff_user = get_user_model().objects.create_user(
            username="client_admin_service",
            password="pwd",
            is_staff=True,
        )
        self.agency = Agency.objects.create(agn_name="Клиент админ сервис")
        self.factory = RequestFactory()

    def test_build_agency_form_context_sets_staff_cancel_url(self):
        request = self.factory.get("/client/new/")
        request.user = self.staff_user

        context = build_agency_form_context(
            request=request,
            mode="create",
            title="Создание клиента",
            submit_label="Создать",
        )

        self.assertEqual(context["cancel_url"], "/client/")
        self.assertTrue(context["staff_view"])

    def test_toggle_agency_archive_response_toggles_flag(self):
        request = self.factory.get(f"/client/{self.agency.pk}/archive/")
        request.user = self.staff_user

        response = toggle_agency_archive_response(request=request, pk=self.agency.pk)

        self.assertEqual(response.status_code, 302)
        self.agency.refresh_from_db()
        self.assertTrue(self.agency.archived)

    @patch("client_cabinet.services.fetch_party_by_inn", return_value={"inn": "123", "agn_name": "ООО Тест"})
    def test_fetch_party_by_inn_response_returns_payload(self, fetch_mock):
        request = self.factory.get("/client/fetch-by-inn/?inn=123")
        request.user = self.staff_user

        response = fetch_party_by_inn_response(request=request)

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content.decode("utf-8"))
        self.assertEqual(payload["data"]["agn_name"], "ООО Тест")
        fetch_mock.assert_called_once_with("123")

    def test_build_client_sku_duplicate_initial_generates_copy_code(self):
        sku = SKU.objects.create(agency=self.agency, sku_code="SKU-DUP", name="Оригинал")
        SKU.objects.create(agency=self.agency, sku_code="SKU-DUP-copy", name="Копия")

        initial = build_client_sku_duplicate_initial(agency=self.agency, sku_id=sku.id)

        self.assertEqual(initial["agency"], self.agency.id)
        self.assertEqual(initial["sku_code"], "SKU-DUP-copy1")

    def test_build_client_sku_form_context_sets_client_view(self):
        context = build_client_sku_form_context(agency=self.agency)

        self.assertEqual(context["agency"], self.agency)
        self.assertTrue(context["client_view"])


class ClientInventoryCheckTests(TestCase):
    def test_inventory_check_matches_received_minus_shipped_to_stock(self):
        agency = Agency.objects.create(agn_name="Клиент 1")
        OrderAuditEntry.objects.create(
            order_id="rcv-1",
            order_type="receiving",
            action="status",
            agency=agency,
            payload={
                "act": "receiving",
                "act_items": [
                    {"sku_code": "SKU-1", "actual_qty": 10},
                    {"sku_code": "SKU-2", "actual_qty": 5},
                ],
            },
        )
        order = ShippingOrder.objects.create(
            number="SO-000001",
            agency=agency,
            status=ShippingOrder.STATUS_SHIPPED,
        )
        ShippingOrderItem.objects.create(
            order=order,
            sku_code="SKU-1",
            name="Товар",
            qty_requested=4,
            qty_shipped=4,
        )
        create_warehouse_snapshot_row(
            agency=agency,
            order_type="receiving",
            order_id="rcv-1",
            sku="SKU-1",
            name="Товар",
            goods_type="Оптовый",
            qty=11,
        )

        result = _inventory_check_for_agency(agency)

        self.assertIsNotNone(result)
        self.assertEqual(result["received_total"], 15)
        self.assertEqual(result["shipped_total"], 4)
        self.assertEqual(result["expected_stock"], 11)
        self.assertEqual(result["stock_total"], 11)
        self.assertEqual(result["processing_in_progress_total"], 0)
        self.assertEqual(result["owned_total"], 11)
        self.assertEqual(result["discrepancy"], 0)
        self.assertEqual(result["status_tone"], "ok")

    def test_inventory_check_marks_discrepancy(self):
        agency = Agency.objects.create(agn_name="Клиент 2")
        OrderAuditEntry.objects.create(
            order_id="rcv-2",
            order_type="receiving",
            action="status",
            agency=agency,
            payload={
                "act": "receiving",
                "act_items": [
                    {"sku_code": "SKU-1", "actual_qty": 12},
                ],
            },
        )
        create_warehouse_snapshot_row(
            agency=agency,
            order_type="receiving",
            order_id="rcv-2",
            sku="SKU-1",
            name="Товар",
            goods_type="Оптовый",
            qty=9,
        )

        result = _inventory_check_for_agency(agency)

        self.assertIsNotNone(result)
        self.assertEqual(result["expected_stock"], 12)
        self.assertEqual(result["stock_total"], 9)
        self.assertEqual(result["processing_in_progress_total"], 0)
        self.assertEqual(result["owned_total"], 9)
        self.assertEqual(result["discrepancy"], -3)
        self.assertEqual(result["status_tone"], "warn")

    def test_inventory_check_counts_goods_in_processing_as_ours(self):
        agency = Agency.objects.create(agn_name="Клиент 3")
        OrderAuditEntry.objects.create(
            order_id="rcv-3",
            order_type="receiving",
            action="status",
            agency=agency,
            payload={
                "act": "receiving",
                "act_items": [
                    {"sku_code": "SKU-1", "actual_qty": 10},
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id="proc-3",
            order_type="processing",
            action="status",
            agency=agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "stock_rows": [
                    {"sku": "SKU-1", "size": "", "goods_type": "", "qty": 10},
                ],
            },
        )
        create_warehouse_snapshot_row(
            agency=agency,
            order_type="receiving",
            order_id="rcv-3",
            sku="SKU-1",
            name="Товар",
            goods_type="Оптовый",
            qty=2,
        )
        create_warehouse_snapshot_row(
            agency=agency,
            order_type="processing",
            order_id="proc-3",
            sku="SKU-1",
            name="Товар",
            goods_type="Оптовый",
            qty=8,
            available_qty=0,
            processing_reserved_qty=8,
            zone="OBR",
            row=0,
            section=0,
            tier=0,
            cell=0,
            warehouse_state_code=WarehouseStateCode.PROCESSING_IN_PROGRESS.value,
        )

        result = _inventory_check_for_agency(agency)

        self.assertIsNotNone(result)
        self.assertEqual(result["expected_stock"], 10)
        self.assertEqual(result["stock_total"], 2)
        self.assertEqual(result["processing_in_progress_total"], 8)
        self.assertEqual(result["owned_total"], 10)
        self.assertEqual(result["discrepancy"], 0)
        self.assertEqual(result["status_tone"], "ok")

    def test_inventory_check_does_not_count_goods_already_moved_to_warehouse_as_processing(self):
        agency = Agency.objects.create(agn_name="Клиент 4")
        OrderAuditEntry.objects.create(
            order_id="rcv-4",
            order_type="receiving",
            action="status",
            agency=agency,
            payload={
                "act": "receiving",
                "act_items": [
                    {"sku_code": "SKU-1", "actual_qty": 400},
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id="proc-4",
            order_type="processing",
            action="status",
            agency=agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "stock_rows": [
                    {"sku": "SKU-1", "size": "", "goods_type": "", "qty": 150},
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id="proc-4",
            order_type="processing",
            action="status",
            agency=agency,
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {
                        "code": "BOX-1",
                        "items": [{"sku": "SKU-1", "qty": 150}],
                    }
                ],
                "act_pallets": [
                    {
                        "code": "PAL-1",
                        "boxes": ["BOX-1"],
                        "location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
                    }
                ],
            },
        )
        create_warehouse_snapshot_row(
            agency=agency,
            order_type="receiving",
            order_id="rcv-4",
            sku="SKU-1",
            name="Товар",
            goods_type="Оптовый",
            qty=400,
        )

        result = _inventory_check_for_agency(agency)

        self.assertIsNotNone(result)
        self.assertEqual(result["expected_stock"], 400)
        self.assertEqual(result["stock_total"], 400)
        self.assertEqual(result["processing_in_progress_total"], 0)
        self.assertEqual(result["owned_total"], 400)
        self.assertEqual(result["discrepancy"], 0)
        self.assertEqual(result["status_tone"], "ok")

    def test_inventory_check_does_not_rebuild_stock_from_audit_when_stock_empty(self):
        agency = Agency.objects.create(agn_name="Клиент 5")
        OrderAuditEntry.objects.create(
            order_id="rcv-5",
            order_type="receiving",
            action="status",
            agency=agency,
            payload={
                "act": "receiving",
                "act_items": [
                    {"sku_code": "SKU-1", "actual_qty": 12},
                ],
            },
        )

        result = _inventory_check_for_agency(agency)

        self.assertIsNotNone(result)
        self.assertEqual(result["expected_stock"], 12)
        self.assertEqual(result["stock_total"], 0)
        self.assertEqual(result["processing_in_progress_total"], 0)
        self.assertEqual(result["owned_total"], 0)
        self.assertEqual(result["discrepancy"], -12)


class ClientCabinetServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="cabinet_service_client", password="pwd")
        self.agency = Agency.objects.create(agn_name="Клиент сервисов", portal_user=self.user)
        self.factory = RequestFactory()

    def test_build_dashboard_context_adds_client_act_attention_card(self):
        OrderAuditEntry.objects.create(
            order_id="rcv-act-1",
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Акт отправлен клиенту",
            payload={"act_sent": "Акт приемки"},
        )
        request = self.factory.get(f"/client/dashboard/?client={self.agency.id}")
        request.user = self.user

        context = build_dashboard_context(
            request=request,
            selected_client=self.agency,
            client_view=True,
            run_inventory_check=False,
        )

        client_column = next(column for column in context["orders_panel_columns"] if column["status"] == "client")
        self.assertTrue(any(order.get("attention") for order in client_column["orders"]))
        attention_card = next(order for order in client_column["orders"] if order.get("attention"))
        self.assertIn("/orders/receiving/rcv-act-1/act/", attention_card["detail_url"])

    def test_build_marking_tools_context_groups_codes_by_pallet(self):
        MarkingCode.objects.create(
            agency=self.agency,
            order_type="processing",
            order_id="proc-1",
            sku_code="SKU-1",
            barcode="200000000001",
            box_barcode="BOX-1",
            code="CZ-1",
        )
        MarkingCode.objects.create(
            agency=self.agency,
            order_type="processing",
            order_id="proc-1",
            sku_code="SKU-1",
            barcode="200000000001",
            box_barcode="BOX-1",
            code="CZ-2",
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="processing",
            order_id="proc-1",
            sku="SKU-1",
            name="Товар",
            barcode="200000000001",
            goods_type="gv",
            qty=2,
            box_code="BOX-1",
            pallet_code="PAL-1",
            zone="OS",
            row=1,
            section=1,
            tier=1,
            cell=1,
        )

        context = build_marking_tools_context(selected_client=self.agency)

        self.assertEqual(context["total_count"], 2)
        self.assertEqual(len(context["pallet_rows"]), 1)
        self.assertEqual(context["pallet_rows"][0]["pallet_code"], "PAL-1")
        self.assertEqual(context["pallet_rows"][0]["cz_count"], 2)

    def test_resolve_client_order_redirect_response_returns_target_for_owned_agency(self):
        request = self.factory.get(f"/client/{self.agency.pk}/receiving/new/")
        request.user = self.user

        response = resolve_client_order_redirect_response(
            request=request,
            pk=self.agency.pk,
            destination="receiving",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"/orders/receiving/?client={self.agency.pk}")

    def test_submit_client_receiving_order_creates_manager_task_for_non_draft(self):
        request = self.factory.post(
            f"/client/{self.agency.pk}/receiving/new/",
            data={
                "submit_action": "send",
                "eta_at": "2026-04-23T12:00",
                "expected_boxes": "3",
                "sku_code[]": ["SKU-1"],
                "sku_id[]": [""],
                "item_name[]": ["Товар 1"],
                "qty[]": ["7"],
                "position_comment[]": [""],
            },
        )
        request.user = self.user
        Employee.objects.create(full_name="Менеджер Клиента", role="manager", is_active=True)

        result = submit_client_receiving_order(request=request, agency=self.agency)

        self.assertTrue(result["redirect_to_dashboard"])
        entry = OrderAuditEntry.objects.get(order_type="receiving")
        self.assertEqual(entry.payload["status"], "sent_unconfirmed")
        self.assertEqual(Task.objects.filter(route=f"/orders/receiving/{entry.order_id}/").count(), 1)

    def test_submit_client_packing_order_logs_uploaded_file_names(self):
        request = self.factory.post(
            f"/client/{self.agency.pk}/packing/new/",
            data={
                "email": "client@example.com",
                "fio": "Иванов И.И.",
                "org": "Клиент",
            },
        )
        request.user = self.user

        result = submit_client_packing_order(request=request, agency=self.agency)

        self.assertTrue(result["submitted"])
        entry = OrderAuditEntry.objects.get(order_id=result["order_id"], order_type="packing")
        self.assertEqual(entry.payload["email"], "client@example.com")
        self.assertEqual(entry.payload["files_report"], [])

    def test_build_client_receiving_form_context_contains_sku_options(self):
        sku = SKU.objects.create(agency=self.agency, sku_code="SKU-CTX-1", name="Товар контекст")
        sku.barcodes.create(value="200000000123", is_primary=True)

        context = build_client_receiving_form_context(agency=self.agency, submitted=False)

        self.assertEqual(context["agency"], self.agency)
        self.assertEqual(context["sku_options"][0]["code"], "SKU-CTX-1")
        self.assertEqual(context["sku_options"][0]["barcodes_joined"], "200000000123")
