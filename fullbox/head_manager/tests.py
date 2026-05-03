import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory
from django.test import TestCase

from employees.models import Employee
from logistics.models import LogisticsTrip
from shipping.models import ShippingOrder
from sku.models import Agency
from todo.models import Task

from .models import Carrier, OwnCompany
from .services import (
    build_marketplace_warehouses_context,
    build_reference_form_context,
    build_reference_list_context,
)
from .views import _normalize_marketplace_warehouse_rows


class HeadManagerDirectoryTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="hm_test", password="pwd")
        Employee.objects.create(full_name="Главный менеджер", role="head_manager", user=self.user, is_active=True)
        self.client.force_login(self.user)

    def test_dashboard_groups_reference_links(self):
        response = self.client.get("/head-manager/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Справочники")
        self.assertContains(response, "/head-manager/own-companies/")
        self.assertContains(response, "/head-manager/carriers/")

    def test_dashboard_task_panel_shows_logistics_tab_for_head_manager(self):
        agency = Agency.objects.create(agn_name="Клиент рейса ГМ")
        logistician = Employee.objects.create(full_name="Логистов Сергей", role="logistician", is_active=True)
        storekeeper = Employee.objects.create(full_name="Кладовщиков Алексей", role="storekeeper", is_active=True)
        order = ShippingOrder.objects.create(number="SO-000150", agency=agency)
        trip = LogisticsTrip.objects.create(
            number="7_RS",
            status=LogisticsTrip.STATUS_LOADING,
            assigned_logistician=logistician,
        )
        Task.objects.create(
            title="Рейс №7_RS",
            route=f"/logistics/trips/{trip.pk}/",
            status="backlog",
            assigned_to=storekeeper,
        )
        Task.objects.create(
            title="Отгрузка",
            route=f"/shipping/{order.pk}/",
            status="backlog",
            assigned_to=logistician,
        )

        response = self.client.get("/head-manager/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="logistics"', html=False)
        self.assertContains(response, "Рейсы")
        self.assertContains(response, "Рейс №7_RS")
        self.assertContains(response, "Заявка на отгрузку №150_OTG")

    def test_create_own_company_generates_short_name(self):
        response = self.client.post(
            "/head-manager/own-companies/new/",
            data={
                "name": 'Общество с ограниченной ответственностью "ФуллБокс Логистика"',
                "inn": "7701234567",
                "kpp": "770101001",
                "ogrn": "1234567890123",
                "address": "Москва, тестовый адрес",
                "postal_address": "Москва, а/я 1",
                "phone": "+7 900 000-00-00",
                "email": "test@example.com",
                "director_name": "Петров П.П.",
                "director_basis": "Устав",
                "bank_name": "АО Тест Банк",
                "bank_bik": "044525000",
                "settlement_account": "40702810900000000001",
                "correspondent_account": "30101810400000000000",
                "bank_address": "Москва, банковский адрес",
                "edo_operator": "СБИС",
                "edo_id": "2BM-TEST",
                "is_default": "on",
                "is_active": "on",
                "comment": "Основная компания",
            },
        )

        self.assertRedirects(response, "/head-manager/own-companies/")
        company = OwnCompany.objects.get()
        self.assertEqual(company.short_name, 'ООО "ФуллБокс Логистика"')
        self.assertTrue(company.is_default)
        self.assertEqual(company.postal_address, "Москва, а/я 1")
        self.assertEqual(company.director_name, "Петров П.П.")
        self.assertEqual(company.bank_name, "АО Тест Банк")
        self.assertEqual(company.edo_operator, "СБИС")

    def test_create_carrier_generates_short_name(self):
        response = self.client.post(
            "/head-manager/carriers/new/",
            data={
                "name": "Индивидуальный предприниматель Иванов Иван Иванович",
                "inn": "770123456789",
                "kpp": "",
                "ogrn": "123456789012345",
                "address": "Тула, тестовый адрес",
                "postal_address": "Тула, а/я 1",
                "phone": "+7 900 111-22-33",
                "email": "carrier@example.com",
                "contact_person": "Иванов И.И.",
                "bank_name": "АО Банк",
                "bank_bik": "044525999",
                "settlement_account": "40802810900000000001",
                "correspondent_account": "30101810400000000001",
                "bank_address": "Москва",
                "is_active": "on",
                "comment": "Пул внешних перевозчиков",
            },
        )

        self.assertRedirects(response, "/head-manager/carriers/")
        carrier = Carrier.objects.get()
        self.assertEqual(carrier.short_name, "ИП Иванов Иван Иванович")
        self.assertEqual(carrier.bank_name, "АО Банк")
        self.assertEqual(carrier.postal_address, "Тула, а/я 1")

    def test_own_companies_list_has_fullscreen_company_columns(self):
        OwnCompany.objects.create(
            name='Общество с ограниченной ответственностью "ФуллБокс"',
            inn="5001149130",
            kpp="503101001",
            ogrn="1225000116604",
            address="Юридический адрес",
            postal_address="Почтовый адрес",
            bank_name="Банк ВТБ",
            bank_bik="044525411",
            settlement_account="40702810700250001553",
            correspondent_account="30101810145250000411",
            email="info@fullbox.ru",
            phone="+7 977 808-83-86",
            edo_operator="Контур Диадок",
            edo_id="2BM-test",
            is_default=True,
            is_active=True,
        )

        response = self.client.get("/head-manager/own-companies/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="wrap fullscreen"', html=False)
        self.assertContains(response, "Юр. адрес")
        self.assertContains(response, "Почтовый адрес")
        self.assertContains(response, "Банковские реквизиты")

    def test_reference_services_build_expected_context(self):
        list_context = build_reference_list_context(
            title="Компании",
            subtitle="Описание",
            create_url="/new/",
            edit_base_url="/edit",
            back_url="/back/",
            type_label="компания",
            directory_mode="own_companies",
        )
        form_context = build_reference_form_context(
            title="Новая компания",
            subtitle="Форма",
            back_url="/back/",
            submit_label="Сохранить",
        )

        self.assertEqual(list_context["directory_mode"], "own_companies")
        self.assertEqual(list_context["create_url"], "/new/")
        self.assertEqual(form_context["submit_label"], "Сохранить")

    def test_carriers_list_has_fullscreen_carrier_columns(self):
        Carrier.objects.create(
            name="Индивидуальный предприниматель Касаев Абдурашид Метханович",
            inn="052903630023",
            address="Юридический адрес перевозчика",
            postal_address="Почтовый адрес перевозчика",
            bank_name='ООО "Банк Точка"',
            bank_bik="044525104",
            settlement_account="40802810201500324670",
            phone="+7 (927) 157-22-42",
            email="kasaev1968@mail.ru",
            is_active=True,
        )

        response = self.client.get("/head-manager/carriers/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="wrap fullscreen"', html=False)
        self.assertContains(response, "Юр. адрес")
        self.assertContains(response, "Почтовый адрес")
        self.assertContains(response, "Банковские реквизиты")


class MarketplaceWarehouseDirectoryTests(TestCase):
    def test_normalizes_legacy_line_into_structured_row(self):
        rows = _normalize_marketplace_warehouse_rows(["Транзитный / ППП · Гольёво — Московская область, Красногорск"])

        self.assertEqual(
            rows,
            [
                {
                    "type": "Транзитный",
                    "name": "Транзитный / ППП · Гольёво",
                    "address": "Московская область, Красногорск",
                }
            ],
        )

    def test_shipping_loader_reads_structured_json(self):
        from shipping import marketplace_warehouses as shipping_marketplaces

        with TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "marketplace_warehouses.json"
            path.write_text(
                json.dumps(
                    {
                        "wb": [
                            {"type": "Транзитный", "name": "Гольёво", "address": "МО, Красногорск"},
                            {"type": "Обычный", "name": "Коледино", "address": "МО, Подольск"},
                        ],
                        "ozon": [],
                        "yandex": [],
                        "sber": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with patch.object(shipping_marketplaces, "marketplace_warehouses_path", return_value=path):
                data = shipping_marketplaces.load_marketplace_warehouses()

        self.assertEqual(
            data["wb"],
            [
                "Транзитный · Гольёво — МО, Красногорск",
                "Обычный · Коледино — МО, Подольск",
            ],
        )

    def test_marketplace_warehouse_context_reads_session_flags(self):
        user = get_user_model().objects.create_user(username="hm_service_ctx", password="pwd")
        Employee.objects.create(full_name="Главный менеджер", role="head_manager", user=user, is_active=True)
        request = RequestFactory().get("/head-manager/marketplace-warehouses/")
        request.user = user
        session = self.client.session
        session["marketplace_sync_info"] = "sync ok"
        session["marketplace_sync_errors"] = ["warn"]
        session["marketplace_sync_agency"] = "Agency"
        session.save()
        request.session = session

        context = build_marketplace_warehouses_context(request=request, saved=True, error="")

        self.assertTrue(context["saved"])
        self.assertEqual(context["sync_info"], "sync ok")
        self.assertEqual(context["sync_errors"], ["warn"])
        self.assertEqual(context["sync_agency"], "Agency")
