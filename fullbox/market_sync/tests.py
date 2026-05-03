import json
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from sku.models import Agency, Market, MarketCredential, MarketplaceBinding, SKU

from .models import MarketSyncReport
from .services import (
    build_dashboard_context,
    build_report_detail_response,
    prepare_ozon_settings_page,
    prepare_wb_settings_page,
    submit_ozon_settings,
    submit_wb_settings,
)
from .sync_services import run_ozon_sync_request, run_wb_sync_request


class MarketSyncServiceTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Клиент market sync")
        self.wb_market = Market.objects.create(id=1, name="WB")
        self.ozon_market = Market.objects.create(id=2, name="OZON")

    def test_build_dashboard_context_marks_configured_marketplaces(self):
        MarketCredential.objects.create(
            id=1,
            agency=self.agency,
            market=self.wb_market,
            market_key="wb-token",
        )
        MarketCredential.objects.create(
            id=2,
            agency=self.agency,
            market=self.ozon_market,
            market_key="ozon-token",
            client_id="12345",
        )
        wb_report = MarketSyncReport.objects.create(
            agency=self.agency,
            marketplace="WB",
            status="ok",
            started_at=timezone.now(),
            finished_at=timezone.now(),
            duration_sec=3,
            processed=10,
            created=4,
            updated=6,
            barcodes_created=2,
            errors=[],
        )

        context = build_dashboard_context(client_id=self.agency.id)

        self.assertEqual(context["selected_client"], self.agency)
        self.assertTrue(context["wb_configured"])
        self.assertTrue(context["ozon_configured"])
        self.assertEqual(context["wb_report"], wb_report)
        self.assertEqual(context["marketplaces"][0]["settings_url"], f"/market-sync/wb/?client={self.agency.id}")

    def test_prepare_wb_settings_page_returns_missing_market_context(self):
        Market.objects.filter(pk=self.wb_market.pk).delete()

        response, context = prepare_wb_settings_page(client_id=self.agency.id)

        self.assertIsNone(response)
        self.assertTrue(context["market_missing"])
        self.assertEqual(context["selected_client"], self.agency)

    def test_submit_wb_settings_creates_credential_and_redirects(self):
        response, context = submit_wb_settings(
            client_id=self.agency.id,
            post_data={"client": str(self.agency.id), "market_key": " new-token "},
        )

        self.assertIsNone(context)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"/market-sync/?client={self.agency.id}")
        credential = MarketCredential.objects.get(agency=self.agency, market=self.wb_market)
        self.assertEqual(credential.market_key, "new-token")

    def test_submit_ozon_settings_returns_form_errors_for_invalid_client_id(self):
        response, context = submit_ozon_settings(
            client_id=self.agency.id,
            post_data={
                "client": str(self.agency.id),
                "client_id": "abc",
                "market_key": "token",
            },
        )

        self.assertIsNone(response)
        self.assertFalse(context["market_missing"])
        self.assertIn("client_id", context["form"].errors)

    def test_build_report_detail_response_serializes_report(self):
        report = MarketSyncReport.objects.create(
            agency=self.agency,
            marketplace="OZON",
            status="error",
            started_at=timezone.now(),
            finished_at=timezone.now(),
            duration_sec=7,
            processed=15,
            created=5,
            updated=10,
            barcodes_created=1,
            errors=["Ошибка API"],
        )

        response = build_report_detail_response(report_id=report.id)
        payload = json.loads(response.content)

        self.assertEqual(payload["marketplace"], "OZON")
        self.assertEqual(payload["agency"]["id"], self.agency.id)
        self.assertEqual(payload["errors"], ["Ошибка API"])

    def test_run_wb_sync_request_requires_client(self):
        response = run_wb_sync_request(body=b"{}")
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(payload["ok"])
        self.assertIn("Не указан клиент.", payload["errors"])

    def test_run_wb_sync_request_requires_token(self):
        response = run_wb_sync_request(body=json.dumps({"client": self.agency.id}).encode("utf-8"))
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(payload["ok"])
        self.assertIn("Не указан токен WB.", payload["errors"])

    @patch("market_sync.sync_services.requests.post")
    def test_run_wb_sync_request_reads_weight_fields_from_wb_card(self, post_mock):
        MarketCredential.objects.create(
            id=10,
            agency=self.agency,
            market=self.wb_market,
            market_key="wb-token",
        )

        first_response = Mock()
        first_response.status_code = 200
        first_response.json.return_value = {
            "cards": [
                {
                    "vendorCode": "SKU-WB-WEIGHT",
                    "nmID": 123456,
                    "title": "WB товар с весом",
                    "brand": "Brand WB",
                    "dimensions": {
                        "length": 10,
                        "width": 20,
                        "height": 30,
                        "weightBrutto": "1.75 кг",
                    },
                    "weightNetto": "1.20 кг",
                }
            ],
            "cursor": {},
        }
        second_response = Mock()
        second_response.status_code = 200
        second_response.json.return_value = {"cards": [], "cursor": {}}
        post_mock.side_effect = [first_response, second_response]

        response = run_wb_sync_request(body=json.dumps({"client": self.agency.id}).encode("utf-8"))
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        sku = SKU.objects.get(agency=self.agency, sku_code="SKU-WB-WEIGHT")
        self.assertEqual(str(sku.weight_kg), "1.750")
        self.assertEqual(str(sku.weight_gross_kg), "1.750")
        self.assertEqual(str(sku.weight_net_kg), "1.200")

    @patch("market_sync.sync_services.requests.post")
    def test_run_wb_sync_request_preserves_manual_sku_fields_by_default(self, post_mock):
        MarketCredential.objects.create(
            id=11,
            agency=self.agency,
            market=self.wb_market,
            market_key="wb-token",
        )
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-WB-MANUAL",
            name="Ручное имя",
            brand="Ручной бренд",
            weight_kg="0.300",
            source="manual",
        )

        first_response = Mock()
        first_response.status_code = 200
        first_response.json.return_value = {
            "cards": [
                {
                    "vendorCode": "SKU-WB-MANUAL",
                    "nmID": 9001,
                    "title": "Имя из WB",
                    "brand": "Brand WB",
                    "dimensions": {"weightBrutto": "1.75 кг"},
                }
            ],
            "cursor": {},
        }
        second_response = Mock()
        second_response.status_code = 200
        second_response.json.return_value = {"cards": [], "cursor": {}}
        post_mock.side_effect = [first_response, second_response]

        response = run_wb_sync_request(body=json.dumps({"client": self.agency.id}).encode("utf-8"))
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        sku.refresh_from_db()
        self.assertEqual(sku.name, "Ручное имя")
        self.assertEqual(sku.brand, "Ручной бренд")
        self.assertEqual(str(sku.weight_kg), "0.300")
        self.assertEqual(sku.source, "manual")
        binding = MarketplaceBinding.objects.get(marketplace="WB", external_id="9001")
        self.assertEqual(binding.sku_id, sku.id)
        self.assertEqual(binding.sync_mode, "readonly")

    @patch("market_sync.sync_services.requests.post")
    def test_run_wb_sync_request_overwrites_fields_when_binding_mode_is_overwrite(self, post_mock):
        MarketCredential.objects.create(
            id=12,
            agency=self.agency,
            market=self.wb_market,
            market_key="wb-token",
        )
        sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-WB-OVERWRITE",
            name="Старое имя",
            brand="Старый бренд",
            weight_kg="0.300",
            source="marketplace",
            market=self.wb_market,
        )
        MarketplaceBinding.objects.create(
            sku=sku,
            marketplace="WB",
            external_id="9002",
            sync_mode="overwrite",
        )

        first_response = Mock()
        first_response.status_code = 200
        first_response.json.return_value = {
            "cards": [
                {
                    "vendorCode": "SKU-WB-OVERWRITE",
                    "nmID": 9002,
                    "title": "Новое имя из WB",
                    "brand": "Новый бренд",
                    "dimensions": {"weightBrutto": "1.75 кг"},
                }
            ],
            "cursor": {},
        }
        second_response = Mock()
        second_response.status_code = 200
        second_response.json.return_value = {"cards": [], "cursor": {}}
        post_mock.side_effect = [first_response, second_response]

        response = run_wb_sync_request(body=json.dumps({"client": self.agency.id}).encode("utf-8"))
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        sku.refresh_from_db()
        self.assertEqual(sku.name, "Новое имя из WB")
        self.assertEqual(sku.brand, "Новый бренд")
        self.assertEqual(str(sku.weight_kg), "1.750")

    def test_run_ozon_sync_request_rejects_invalid_client_id_format(self):
        MarketCredential.objects.create(
            id=3,
            agency=self.agency,
            market=self.ozon_market,
            market_key="ozon-token",
            client_id="abc",
        )

        response = run_ozon_sync_request(body=json.dumps({"client": self.agency.id}).encode("utf-8"))
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(payload["ok"])
        self.assertIn("Client ID Ozon должен быть положительным числом.", payload["errors"])
