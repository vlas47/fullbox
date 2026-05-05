import json
from unittest import mock
from pathlib import Path
from urllib.parse import quote

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from audit.models import AuditEntry, OrderAuditEntry
from employees.models import Employee
from marking.models import MarkingCode
from reachtruck.models import MoveTask
from sklad.models import WarehouseOperation, WarehouseReserve, WarehouseStockSnapshot
from sklad.services import WarehouseStateCode
from sklad.services.stock_availability import StockAvailabilityService
from sklad.services.warehouse_write_path import WarehouseWritePathService
from sklad.test_utils import create_warehouse_snapshot_row
from sku.models import Agency
from todo.models import Task

from .models import ProcessingFlowSession, ProcessingPrintJob
from .services import ProcessingWorkflowService
from .views import (
    _create_processing_warehouse_moves,
    _processing_discrepancy_rows,
    _processing_placement_entry,
    _processing_warehouse_move_progress,
    _processing_reserve_rows_for_order,
    _expected_processing_results,
    _inventory_items_for_agency,
    _processing_work_payload_from_entries,
    _processing_results_are_ready,
    _replace_processing_reserves,
    ProcessingWorkView,
)


class ProcessingResultsReadinessTests(SimpleTestCase):
    def test_expected_results_fallback_from_cards(self):
        payload = {
            "cards": [
                {
                    "id": "card-a",
                    "article": "SKU-ONE",
                    "rows": [
                        {"size": "42", "qty": "10"},
                        {"size": "43", "qty": "5"},
                    ],
                }
            ]
        }
        expected = _expected_processing_results(payload)
        self.assertEqual(
            expected,
            [
                ("card-a", "sku-one", "42", "-"),
                ("card-a", "sku-one", "43", "-"),
            ],
        )

    def test_expected_results_keeps_rows_of_same_article_for_different_cards(self):
        payload = {
            "cards": [
                {"id": "card-a", "article": "SKU-ONE", "rows": [{"size": "42", "qty": "10"}]},
                {"id": "card-b", "article": "SKU-ONE", "rows": [{"size": "42", "qty": "10"}]},
            ]
        }
        expected = _expected_processing_results(payload)
        self.assertCountEqual(
            expected,
            [
                ("card-a", "sku-one", "42", "-"),
                ("card-b", "sku-one", "42", "-"),
            ],
        )

    def test_results_ready_requires_all_fields_from_requirements(self):
        payload = {
            "cards": [
                {"article": "SKU-ONE", "rows": [{"size": "42", "qty": "10"}]},
            ],
            "defect_percent": "10%",
            "marking_5840_qty": "1",
            "tag_owner": "Фуллбокс",
            "processing_results": [
                {
                    "article": "SKU-ONE",
                    "size": "42",
                    "destination": "-",
                    "processed": "10",
                    "defect": "0",
                    "shortage": "0",
                    "labels_printed": "10",
                    "labels_unboxed": "10",
                    "tags_replaced": "0",
                }
            ],
        }
        self.assertTrue(_processing_results_are_ready(payload, include_shipping=False))

        payload["processing_results"][0].pop("tags_replaced")
        self.assertTrue(_processing_results_are_ready(payload, include_shipping=False))

    def test_expected_results_aggregates_direction_plan_rows(self):
        payload = {
            "direction_addresses_json": ["Москва"],
            "direction_plan_json": {
                "directions": ["Москва"],
                "rows": [
                    {"article": "SKU-ONE", "size": "42", "quantities": [10]},
                ],
            },
        }
        self.assertEqual(_expected_processing_results(payload), [("", "sku-one", "42", "-")])

    def test_include_shipping_flag_does_not_require_shipping_on_processing_card(self):
        payload = {
            "direction_addresses_json": ["Москва"],
            "direction_plan_json": {
                "directions": ["Москва"],
                "rows": [
                    {"article": "SKU-ONE", "size": "42", "quantities": [10]},
                ],
            },
            "processing_results": [
                {
                    "article": "SKU-ONE",
                    "size": "42",
                    "destination": "-",
                    "processed": "10",
                }
            ],
        }
        self.assertTrue(_processing_results_are_ready(payload, include_shipping=False))
        self.assertTrue(_processing_results_are_ready(payload, include_shipping=True))

    def test_discrepancy_rows_compare_expected_payload_to_factual_placement_payload(self):
        expected_payload = {
            "cards": [
                {"article": "SKU-ONE", "rows": [{"size": "42", "qty": "10"}]},
            ],
        }
        factual_payload = {
            "act_boxes": [
                {
                    "code": "BOX-1",
                    "items": [
                        {"sku": "SKU-ONE", "sku_code": "SKU-ONE", "name": "Item", "size": "42", "qty": 10},
                    ],
                }
            ],
            "act_pallets": [],
        }

        self.assertEqual(_processing_discrepancy_rows(expected_payload, factual_payload), [])

    def test_processing_work_payload_backfills_cards_and_results_from_history(self):
        agency = Agency(agn_name="Backfill Agency")
        entries = [
            OrderAuditEntry(
                order_id="1",
                order_type="processing",
                action="status",
                agency=agency,
                payload={
                    "status": "processing_in_work",
                    "status_label": "Взята в работу",
                    "cards": [{"article": "SKU-ONE", "rows": [{"size": "42", "qty": "10"}]}],
                    "processing_results": [
                        {"article": "SKU-ONE", "size": "42", "destination": "-", "processed": "10"},
                    ],
                },
            ),
            OrderAuditEntry(
                order_id="1",
                order_type="processing",
                action="status",
                agency=agency,
                payload={
                    "status": "processing_in_work",
                    "status_label": "Разногласия — на утверждении руководителя обработки",
                    "act": "placement",
                    "act_state": "closed",
                    "discrepancy_status": "reported",
                },
            ),
        ]

        payload = _processing_work_payload_from_entries(entries)

        self.assertEqual(payload.get("status"), "processing_in_work")
        self.assertEqual(len(payload.get("cards") or []), 1)
        self.assertEqual(len(payload.get("processing_results") or []), 1)

    def test_processing_placement_entry_prefers_closed_flow_snapshot_over_partial_updates(self):
        agency = Agency(agn_name="Placement Agency")
        entries = [
            OrderAuditEntry(
                order_id="1",
                order_type="processing",
                action="update",
                agency=agency,
                payload={
                    "act": "placement",
                    "flow_closed": True,
                    "act_pallets": [{"code": "PAL-1"}, {"code": "PAL-2"}],
                },
            ),
            OrderAuditEntry(
                order_id="1",
                order_type="processing",
                action="status",
                agency=agency,
                payload={
                    "act": "placement",
                    "act_pallets": [{"code": "PAL-1"}],
                },
            ),
        ]

        placement_entry = _processing_placement_entry(entries)

        self.assertIsNotNone(placement_entry)
        self.assertEqual(
            [item.get("code") for item in ((placement_entry.payload or {}).get("act_pallets") or [])],
            ["PAL-1", "PAL-2"],
        )


class ProcessingWorkTemplateRegressionTests(SimpleTestCase):
    def test_work_card_treats_pallet_full_move_as_delivery_to_obr(self):
        template_path = (
            Path(__file__).resolve().parent
            / "templates"
            / "processing"
            / "processing_work.html"
        )
        template_source = template_path.read_text(encoding="utf-8")
        self.assertIn("let hasPalletFullDone = false;", template_source)
        self.assertIn("if (mode === MOVE_MODE_PALLET_FULL)", template_source)
        self.assertIn(
            "const completedByFullTransfer = !hasActive && (hasBoxFullDone || hasPalletFullDone);",
            template_source,
        )


class ProcessingCardSaveMergeTests(TestCase):
    def setUp(self):
        self.order_id = "500"
        self.user = get_user_model().objects.create_user(username="ph_user", password="x")
        Employee.objects.create(
            full_name="Processing Head",
            user=self.user,
            role="processing_head",
            is_active=True,
        )
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Test Agency")
        self.base_payload = {
            "status": "processing_in_work",
            "status_label": "Взята в работу",
            "cards": [
                {
                    "id": "card-a",
                    "article": "IP/AA5532черный",
                    "rows": [{"size": "27", "barcode": "2000508267118", "qty": "100"}],
                },
                {
                    "id": "card-b",
                    "article": "IP/AA5",
                    "rows": [{"size": "27", "barcode": "2000508269068", "qty": "100"}],
                },
            ],
        }
        OrderAuditEntry.objects.create(
            order_id=self.order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload=self.base_payload,
        )

    def _post_card_result(self, card_id: str, article: str, size: str, barcode: str, processed: str):
        url = f"/orders/processing/{self.order_id}/card/{card_id}/?article={quote(article)}"
        data = {
            "action": "save_results",
            "return": f"/orders/processing/{self.order_id}/work/",
            "return_to": f"/orders/processing/{self.order_id}/work/",
            "card_id": card_id,
            "result_article[]": [article],
            "result_size[]": [size],
            "result_destination[]": ["-"],
            "result_received[]": ["100"],
            "result_processed[]": [processed],
            "result_defect[]": ["0"],
            "result_shortage[]": ["0"],
            "result_labels_printed[]": ["100"],
            "result_labels_unboxed[]": ["100"],
            "result_shipped_qty[]": ["0"],
            "result_tags_replaced[]": ["0"],
        }
        return self.client.post(url, data)

    @staticmethod
    def _results_map(payload: dict) -> dict[tuple[str, str, str, str], dict]:
        result = {}
        for item in payload.get("processing_results") or []:
            if not isinstance(item, dict):
                continue
            key = (
                str(item.get("card_id") or "").strip().lower(),
                str(item.get("article") or "").strip().lower(),
                str(item.get("size") or "").strip().lower(),
                str(item.get("destination") or "").strip().lower() or "-",
            )
            result[key] = item
        return result

    def test_save_results_merges_rows_between_cards(self):
        first = self._post_card_result(
            card_id="card-a",
            article="IP/AA5532черный",
            size="27",
            barcode="2000508267118",
            processed="100",
        )
        self.assertEqual(first.status_code, 302)

        second = self._post_card_result(
            card_id="card-b",
            article="IP/AA5",
            size="27",
            barcode="2000508269068",
            processed="90",
        )
        self.assertEqual(second.status_code, 302)

        latest = (
            OrderAuditEntry.objects.filter(order_id=self.order_id, order_type="processing")
            .order_by("-id")
            .first()
        )
        self.assertIsNotNone(latest)
        results_map = self._results_map(latest.payload or {})
        self.assertEqual(len(results_map), 2)
        self.assertEqual(
            results_map[("card-a", "ip/aa5532черный", "27", "-")].get("processed"),
            "100",
        )
        self.assertEqual(
            results_map[("card-b", "ip/aa5", "27", "-")].get("processed"),
            "90",
        )

    def test_save_results_updates_same_row_without_duplication(self):
        self._post_card_result(
            card_id="card-a",
            article="IP/AA5532черный",
            size="27",
            barcode="2000508267118",
            processed="100",
        )
        self._post_card_result(
            card_id="card-b",
            article="IP/AA5",
            size="27",
            barcode="2000508269068",
            processed="90",
        )
        self._post_card_result(
            card_id="card-a",
            article="IP/AA5532черный",
            size="27",
            barcode="2000508267118",
            processed="95",
        )

        latest = (
            OrderAuditEntry.objects.filter(order_id=self.order_id, order_type="processing")
            .order_by("-id")
            .first()
        )
        self.assertIsNotNone(latest)
        results_map = self._results_map(latest.payload or {})
        self.assertEqual(len(results_map), 2)
        self.assertEqual(
            results_map[("card-a", "ip/aa5532черный", "27", "-")].get("processed"),
            "95",
        )

    def test_save_results_replaces_legacy_direction_rows_for_same_card(self):
        seeded_payload = dict(self.base_payload)
        seeded_payload["processing_results"] = [
            {
                "card_id": "card-a",
                "article": "IP/AA5532черный",
                "size": "27",
                "destination": "Москва",
                "processed": "40",
            },
            {
                "card_id": "card-a",
                "article": "IP/AA5532черный",
                "size": "27",
                "destination": "Казань",
                "processed": "60",
            },
        ]
        OrderAuditEntry.objects.create(
            order_id=self.order_id,
            order_type="processing",
            action="result",
            agency=self.agency,
            payload=seeded_payload,
        )

        response = self._post_card_result(
            card_id="card-a",
            article="IP/AA5532черный",
            size="27",
            barcode="2000508267118",
            processed="100",
        )
        self.assertEqual(response.status_code, 302)

        latest = (
            OrderAuditEntry.objects.filter(order_id=self.order_id, order_type="processing")
            .order_by("-id")
            .first()
        )
        self.assertIsNotNone(latest)
        results_map = self._results_map(latest.payload or {})
        self.assertIn(("card-a", "ip/aa5532черный", "27", "-"), results_map)
        self.assertNotIn(("card-a", "ip/aa5532черный", "27", "москва"), results_map)
        self.assertNotIn(("card-a", "ip/aa5532черный", "27", "казань"), results_map)

    def test_return_to_processing_saves_results_and_marks_card(self):
        url = f"/orders/processing/{self.order_id}/card/card-a/?article={quote('IP/AA5532черный')}"
        response = self.client.post(
            url,
            {
                "action": "return_to_processing",
                "return": f"/orders/processing/{self.order_id}/work/",
                "return_to": f"/orders/processing/{self.order_id}/work/",
                "card_id": "card-a",
                "result_article[]": ["IP/AA5532черный"],
                "result_size[]": ["27"],
                "result_destination[]": ["-"],
                "result_received[]": ["100"],
                "result_processed[]": ["88"],
                "result_defect[]": ["0"],
                "result_shortage[]": ["0"],
                "result_labels_printed[]": ["100"],
                "result_labels_unboxed[]": ["100"],
                "result_shipped_qty[]": ["0"],
                "result_tags_replaced[]": ["0"],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"/orders/processing/{self.order_id}/work/")

        latest = (
            OrderAuditEntry.objects.filter(order_id=self.order_id, order_type="processing")
            .order_by("-id")
            .first()
        )
        self.assertIsNotNone(latest)
        payload = latest.payload or {}
        results_map = self._results_map(payload)
        self.assertEqual(
            results_map[("card-a", "ip/aa5532черный", "27", "-")].get("processed"),
            "88",
        )
        self.assertIn("card-a", payload.get("processed_cards") or [])
        cards = payload.get("cards") or []
        card_a = next((card for card in cards if isinstance(card, dict) and card.get("id") == "card-a"), None)
        self.assertIsNotNone(card_a)
        self.assertTrue(bool(card_a.get("processed_done")))

    def test_save_results_marks_card_when_results_are_ready(self):
        url = f"/orders/processing/{self.order_id}/card/card-a/?article={quote('IP/AA5532черный')}"
        response = self.client.post(
            url,
            {
                "action": "save_results",
                "return": f"/orders/processing/{self.order_id}/work/",
                "return_to": f"/orders/processing/{self.order_id}/work/",
                "card_id": "card-a",
                "result_article[]": ["IP/AA5532черный"],
                "result_size[]": ["27"],
                "result_destination[]": ["-"],
                "result_received[]": ["100"],
                "result_processed[]": ["88"],
                "result_defect[]": ["0"],
                "result_shortage[]": ["0"],
                "result_labels_printed[]": ["100"],
                "result_labels_unboxed[]": ["100"],
                "result_shipped_qty[]": ["0"],
                "result_tags_replaced[]": ["0"],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"/orders/processing/{self.order_id}/work/")

        latest = (
            OrderAuditEntry.objects.filter(order_id=self.order_id, order_type="processing")
            .order_by("-id")
            .first()
        )
        self.assertIsNotNone(latest)
        payload = latest.payload or {}
        self.assertIn("card-a", payload.get("processed_cards") or [])
        cards = payload.get("cards") or []
        card_a = next((card for card in cards if isinstance(card, dict) and card.get("id") == "card-a"), None)
        self.assertIsNotNone(card_a)
        self.assertTrue(bool(card_a.get("processed_done")))


class ProcessingCardSaveSameArticleTests(TestCase):
    def setUp(self):
        self.order_id = "501"
        self.user = get_user_model().objects.create_user(username="ph_user_same", password="x")
        Employee.objects.create(
            full_name="Processing Head Same",
            user=self.user,
            role="processing_head",
            is_active=True,
        )
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Same Article Agency")
        OrderAuditEntry.objects.create(
            order_id=self.order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "cards": [
                    {
                        "id": "card-a",
                        "article": "IP/AA5",
                        "rows": [{"size": "27", "barcode": "2000508267118", "qty": "100"}],
                    },
                    {
                        "id": "card-b",
                        "article": "IP/AA5",
                        "rows": [{"size": "27", "barcode": "2000508269068", "qty": "100"}],
                    },
                ],
            },
        )

    def _post_result(self, card_id: str, processed: str):
        return self.client.post(
            f"/orders/processing/{self.order_id}/card/{card_id}/?article={quote('IP/AA5')}",
            {
                "action": "save_results",
                "return": f"/orders/processing/{self.order_id}/work/",
                "return_to": f"/orders/processing/{self.order_id}/work/",
                "card_id": card_id,
                "result_article[]": ["IP/AA5"],
                "result_size[]": ["27"],
                "result_destination[]": ["-"],
                "result_received[]": ["100"],
                "result_processed[]": [processed],
                "result_defect[]": ["0"],
                "result_shortage[]": ["0"],
                "result_labels_printed[]": ["100"],
                "result_labels_unboxed[]": ["100"],
                "result_shipped_qty[]": ["0"],
                "result_tags_replaced[]": ["0"],
            },
        )

    def test_same_article_size_from_different_cards_do_not_overwrite(self):
        self.assertEqual(self._post_result("card-a", "100").status_code, 302)
        self.assertEqual(self._post_result("card-b", "90").status_code, 302)

        latest = (
            OrderAuditEntry.objects.filter(order_id=self.order_id, order_type="processing")
            .order_by("-id")
            .first()
        )
        self.assertIsNotNone(latest)
        rows = [row for row in (latest.payload or {}).get("processing_results") or [] if isinstance(row, dict)]
        self.assertEqual(len(rows), 2)
        by_card = {str(row.get("card_id") or "").strip().lower(): row for row in rows}
        self.assertEqual(by_card["card-a"].get("processed"), "100")
        self.assertEqual(by_card["card-b"].get("processed"), "90")


class ProcessingWorkFlowConditionsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="ph_user_work", password="x")
        Employee.objects.create(
            full_name="Processing Head Work",
            user=self.user,
            role="processing_head",
            is_active=True,
        )
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Work Conditions Agency")

    def _create_order(self, order_id: str, payload: dict):
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload=payload,
        )

    def test_work_page_blocks_when_cards_are_not_fully_completed(self):
        order_id = "610"
        payload = {
            "status": "processing_in_work",
            "status_label": "Взята в работу",
            "cards": [
                {"id": "card-a", "article": "SKU-A", "rows": [{"size": "42", "qty": "10"}]},
                {"id": "card-b", "article": "SKU-B", "rows": [{"size": "43", "qty": "10"}]},
            ],
            "processed_cards": ["card-a"],
            "processing_results": [
                {"card_id": "card-a", "article": "SKU-A", "size": "42", "destination": "-", "processed": "10"},
                {"card_id": "card-b", "article": "SKU-B", "size": "43", "destination": "-", "processed": "10"},
            ],
        }
        self._create_order(order_id, payload)

        response = self.client.get(f"/orders/processing/{order_id}/work/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(bool(response.context["processing_all_cards_processed"]))
        self.assertTrue(bool(response.context["processing_results_ready_for_flow"]))
        self.assertFalse(bool(response.context["can_open_processing_flow"]))

    def test_work_page_blocks_when_results_are_not_filled(self):
        order_id = "611"
        payload = {
            "status": "processing_in_work",
            "status_label": "Взята в работу",
            "cards": [
                {"id": "card-a", "article": "SKU-A", "rows": [{"size": "42", "qty": "10"}]},
                {"id": "card-b", "article": "SKU-B", "rows": [{"size": "43", "qty": "10"}]},
            ],
            "processed_cards": ["card-a", "card-b"],
            "processing_results": [],
        }
        self._create_order(order_id, payload)

        response = self.client.get(f"/orders/processing/{order_id}/work/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(bool(response.context["processing_all_cards_processed"]))
        self.assertFalse(bool(response.context["processing_results_ready_for_flow"]))
        self.assertFalse(bool(response.context["can_open_processing_flow"]))

    def test_work_page_allows_flow_when_cards_and_results_are_ready(self):
        order_id = "612"
        payload = {
            "status": "processing_in_work",
            "status_label": "Взята в работу",
            "cards": [
                {"id": "card-a", "article": "SKU-A", "rows": [{"size": "42", "qty": "10"}]},
                {"id": "card-b", "article": "SKU-B", "rows": [{"size": "43", "qty": "10"}]},
            ],
            "processed_cards": ["card-a", "card-b"],
            "processing_results": [
                {"card_id": "card-a", "article": "SKU-A", "size": "42", "destination": "-", "processed": "10"},
                {"card_id": "card-b", "article": "SKU-B", "size": "43", "destination": "-", "processed": "10"},
            ],
        }
        self._create_order(order_id, payload)

        response = self.client.get(f"/orders/processing/{order_id}/work/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(bool(response.context["processing_all_cards_processed"]))
        self.assertTrue(bool(response.context["processing_results_ready_for_flow"]))
        self.assertTrue(bool(response.context["can_open_processing_flow"]))

    def test_work_page_allows_access_after_placement_is_closed(self):
        order_id = "613"
        self._create_order(
            order_id,
            {
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "cards": [
                    {"id": "card-a", "article": "SKU-A", "rows": [{"size": "42", "qty": "10"}]},
                ],
                "processed_cards": ["card-a"],
                "processing_results": [
                    {"card_id": "card-a", "article": "SKU-A", "size": "42", "destination": "-", "processed": "10"},
                ],
            },
        )
        self._create_order(
            order_id,
            {
                "act": "placement",
                "act_state": "closed",
                "flow_closed": True,
            },
        )

        response = self.client.get(f"/orders/processing/{order_id}/work/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(bool(response.context["placement_completed"]))


class ProcessingWarehouseMoveCreationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="ph_user_wh_moves", password="x")
        Employee.objects.create(
            full_name="Processing Head Warehouse",
            user=self.user,
            role="processing_head",
            is_active=True,
        )
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Warehouse Move Agency")
        self.request_factory = RequestFactory()

    def test_send_to_warehouse_uses_selected_destinations(self):
        order_id = "615"
        first_entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "cards": [
                    {"id": "card-a", "article": "SKU-A", "rows": [{"size": "42", "qty": "10"}]},
                ],
                "processed_cards": ["card-a"],
                "processing_results": [
                    {"card_id": "card-a", "article": "SKU-A", "size": "42", "destination": "-", "processed": "10"},
                ],
            },
        )
        second_entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {"code": "BOX-WH-1", "items": [{"sku": "SKU-A", "qty": 10}]},
                ],
                "act_pallets": [
                    {
                        "code": "PAL-WH-1",
                        "boxes": ["BOX-WH-1"],
                        "items": [],
                        "location": {"zone": "OBR", "row": "", "section": "", "tier": "", "cell": ""},
                    }
                ],
            },
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            data={"action": "send_to_warehouse"},
        )
        request.user = self.user
        created, skipped_existing, skipped_missing = _create_processing_warehouse_moves(
            order_id,
            [first_entry, second_entry],
            request,
            destinations_by_pallet={
                "PAL-WH-1": {"zone": "OS", "row": 2, "section": 3, "tier": 1, "cell": 4},
            },
        )

        self.assertEqual((created, skipped_existing, skipped_missing), (1, 0, 0))
        task = MoveTask.objects.get()
        self.assertEqual(task.pallet_code, "PAL-WH-1")
        self.assertEqual(task.to_zone, "OS")
        self.assertEqual(task.to_row, 2)
        self.assertEqual(task.to_section, 3)
        self.assertEqual(task.to_tier, 1)
        self.assertEqual(task.to_cell, 4)

    def test_send_to_warehouse_skips_pallets_without_confirmed_destination(self):
        order_id = "616"
        first_entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
            },
        )
        second_entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [],
                "act_pallets": [
                    {
                        "code": "PAL-WH-2",
                        "boxes": [],
                        "items": [{"sku": "SKU-A", "qty": 5}],
                        "location": {"zone": "OBR", "row": "", "section": "", "tier": "", "cell": ""},
                    },
                    {
                        "code": "PAL-WH-3",
                        "boxes": [],
                        "items": [{"sku": "SKU-A", "qty": 5}],
                        "location": {"zone": "OBR", "row": "", "section": "", "tier": "", "cell": ""},
                    },
                ],
            },
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            data={"action": "send_to_warehouse"},
        )
        request.user = self.user

        created, skipped_existing, skipped_missing = _create_processing_warehouse_moves(
            order_id,
            [first_entry, second_entry],
            request,
            destinations_by_pallet={
                "PAL-WH-2": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 2},
                "PAL-WH-3": {"zone": "PR", "row": "", "section": "", "tier": "", "cell": ""},
            },
        )

        self.assertEqual((created, skipped_existing, skipped_missing), (1, 0, 1))
        self.assertEqual(MoveTask.objects.count(), 1)

    def test_work_page_prefills_suggested_destination_for_warehouse_move_modal(self):
        order_id = "617"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "cards": [
                    {"id": "card-a", "article": "SKU-A", "rows": [{"size": "42", "qty": "10"}]},
                ],
                "processed_cards": ["card-a"],
                "processing_results": [
                    {"card_id": "card-a", "article": "SKU-A", "size": "42", "destination": "-", "processed": "10"},
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {"code": "BOX-WH-SUGGEST-1", "items": [{"sku": "SKU-A", "qty": 10}]},
                ],
                "act_pallets": [
                    {
                        "code": "PAL-WH-SUGGEST-1",
                        "boxes": ["BOX-WH-SUGGEST-1"],
                        "items": [],
                        "location": {"zone": "OBR", "row": "", "section": "", "tier": "", "cell": ""},
                    }
                ],
            },
        )

        response = self.client.get(f"/orders/processing/{order_id}/work/")

        self.assertEqual(response.status_code, 200)
        rows = response.context["warehouse_move_rows"]
        self.assertEqual(len(rows), 1)
        destination = rows[0]["destination"]
        self.assertEqual(destination["zone"], "OS")
        self.assertEqual(destination["row"], 1)
        self.assertEqual(destination["section"], 1)
        self.assertEqual(destination["tier"], 1)
        self.assertEqual(destination["cell"], 1)
        self.assertIn("Сервис предложил место хранения", rows[0]["destination_note"])

    def test_processing_warehouse_move_progress_uses_warehouse_location_when_audit_missing(self):
        order_id = "617-WH-PROGRESS"
        storage_location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OS",
            row_no=2,
            section_no=3,
            tier_no=1,
            cell_no=4,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="legacy_stock",
            source_context_id=f"legacy-{order_id}",
            sku_code="SKU-A",
            name="Processing Item",
            size="42",
            barcode=f"BC-{order_id}",
            goods_type="gv",
            qty=10,
            available_qty=10,
            container_code="PAL-WH-PROG-1",
            location=storage_location,
            zone_code=storage_location.zone_code,
            zone_kind=storage_location.zone_kind,
            warehouse_state_code="stored",
        )

        progress = _processing_warehouse_move_progress(
            order_id,
            placement_pallets=[
                {
                    "code": "PAL-WH-PROG-1",
                    "location": {"zone": "OBR", "row": "", "section": "", "tier": "", "cell": ""},
                }
            ],
            agency=self.agency,
        )

        self.assertEqual(progress["done_count"], 1)
        self.assertEqual(progress["not_created_count"], 0)
        self.assertIn("PAL-WH-PROG-1", progress["implicit_done_codes"])
        self.assertEqual(progress["moves_by_pallet"]["PAL-WH-PROG-1"]["to_zone"], "OS")

    def test_send_to_warehouse_skips_pallet_already_moved_in_warehouse_without_audit(self):
        order_id = "617-WH-SKIP"
        first_entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
            },
        )
        second_entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [],
                "act_pallets": [
                    {
                        "code": "PAL-WH-SKIP-1",
                        "boxes": [],
                        "items": [{"sku": "SKU-A", "qty": 5}],
                        "location": {"zone": "OBR", "row": "", "section": "", "tier": "", "cell": ""},
                    }
                ],
            },
        )
        storage_location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OS",
            row_no=1,
            section_no=2,
            tier_no=1,
            cell_no=3,
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="legacy_stock",
            source_context_id=f"legacy-{order_id}",
            sku_code="SKU-A",
            name="Processing Item",
            size="42",
            barcode=f"BC-{order_id}",
            goods_type="gv",
            qty=5,
            available_qty=5,
            container_code="PAL-WH-SKIP-1",
            location=storage_location,
            zone_code=storage_location.zone_code,
            zone_kind=storage_location.zone_kind,
            warehouse_state_code="stored",
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            data={"action": "send_to_warehouse"},
        )
        request.user = self.user

        created, skipped_existing, skipped_missing = _create_processing_warehouse_moves(
            order_id,
            [first_entry, second_entry],
            request,
            destinations_by_pallet={
                "PAL-WH-SKIP-1": {"zone": "OS", "row": 1, "section": 2, "tier": 1, "cell": 3},
            },
        )

        self.assertEqual((created, skipped_existing, skipped_missing), (0, 1, 0))
        self.assertEqual(MoveTask.objects.count(), 0)


class ProcessingWorkflowServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="proc_service_user", password="x")
        Employee.objects.create(
            full_name="Processing Service User",
            user=self.user,
            role="processing_head",
            is_active=True,
        )
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Processing Service Agency")
        self.request_factory = RequestFactory()

    def _create_processing_snapshot(self, *, order_id: str, state_code: str = "processing_in_progress"):
        location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OBR",
        )
        snapshot = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="legacy_stock",
            source_context_id=f"legacy-{order_id}",
            sku_code="SKU-PROC",
            name="Processing Item",
            size="42",
            barcode=f"BC-{order_id}",
            goods_type="gv",
            qty=10,
            available_qty=0,
            processing_reserved_qty=10,
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
            source_document_type="processing_order",
            source_document_id=order_id,
            created_by=self.user,
        )
        return snapshot

    def _create_ready_processing_entries(self, order_id: str):
        first_entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "cards": [
                    {"id": "card-a", "article": "SKU-A", "rows": [{"size": "42", "qty": "10"}]},
                ],
                "processed_cards": ["card-a"],
                "processing_results": [
                    {"card_id": "card-a", "article": "SKU-A", "size": "42", "destination": "-", "processed": "10"},
                ],
            },
        )
        second_entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [
                    {"code": "BOX-SERVICE-1", "items": [{"sku": "SKU-A", "qty": 10}]},
                ],
                "act_pallets": [
                    {
                        "code": "PAL-SERVICE-1",
                        "boxes": ["BOX-SERVICE-1"],
                        "items": [],
                        "location": {"zone": "OBR", "row": "", "section": "", "tier": "", "cell": ""},
                    }
                ],
            },
        )
        return [first_entry, second_entry]

    def test_send_processing_to_warehouse_rejects_invalid_destination_json(self):
        order_id = "618"
        entries = self._create_ready_processing_entries(order_id)
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            data={"warehouse_destinations_json": "{bad json"},
        )
        request.user = self.user

        result = ProcessingWorkflowService.send_processing_to_warehouse(
            order_id=order_id,
            entries=entries,
            request=request,
        )

        self.assertEqual(result.status, "invalid_destination")
        self.assertIn("Некорректный JSON", result.error_message)

    def test_cancel_processing_warehouse_moves_returns_canceled_status(self):
        order_id = "619"
        entries = self._create_ready_processing_entries(order_id)
        create_request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            data={"action": "send_to_warehouse"},
        )
        create_request.user = self.user
        _create_processing_warehouse_moves(
            order_id,
            entries,
            create_request,
            destinations_by_pallet={
                "PAL-SERVICE-1": {"zone": "OS", "row": 1, "section": 2, "tier": 1, "cell": 3},
            },
        )
        cancel_request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            data={"action": "cancel_warehouse_moves"},
        )
        cancel_request.user = self.user

        result = ProcessingWorkflowService.cancel_processing_warehouse_moves(
            order_id=order_id,
            entries=entries,
            request=cancel_request,
        )

        self.assertEqual(result.status, "canceled")
        self.assertEqual(result.canceled_count, 1)
        task = MoveTask.objects.get()
        self.assertEqual(task.status, "canceled")

    def test_build_processing_work_page_context_prefers_existing_move_destination(self):
        order_id = "620"
        entries = self._create_ready_processing_entries(order_id)
        create_request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            data={"action": "send_to_warehouse"},
        )
        create_request.user = self.user
        _create_processing_warehouse_moves(
            order_id,
            entries,
            create_request,
            destinations_by_pallet={
                "PAL-SERVICE-1": {"zone": "MR", "row": 4, "section": "", "tier": "", "cell": ""},
            },
        )
        request = self.request_factory.get(f"/orders/processing/{order_id}/work/")
        request.user = self.user

        context = ProcessingWorkflowService.build_processing_work_page_context(
            order_id=order_id,
            entries=entries,
            payload=_processing_work_payload_from_entries(entries),
            agency=self.agency,
            request=request,
        )

        self.assertEqual(context["warehouse_destination_summary"], "MR · Между рядами · Ряд 4")
        self.assertEqual(len(context["warehouse_move_rows"]), 1)
        self.assertEqual(context["warehouse_move_rows"][0]["destination"]["zone"], "MR")
        self.assertEqual(context["warehouse_move_rows"][0]["destination"]["row"], 4)

    def test_finish_processing_reports_discrepancy_for_processing_head(self):
        order_id = "621"
        entries = self._create_ready_processing_entries(order_id)
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            data={"action": "finish_processing"},
        )
        request.user = self.user

        with mock.patch(
            "processing_app.views._processing_finish_checks",
            return_value={
                "blockers": [],
                "placement_payload": {"act_boxes": [{"code": "BOX-SERVICE-1"}], "act_pallets": [{"code": "PAL-SERVICE-1"}]},
                "placement_closed": True,
                "has_boxes": True,
                "has_pallets": True,
                "warehouse_move_created": True,
                "warehouse_move_completed": True,
                "warehouse_move_progress": {"total_pallets": 1, "done_count": 1},
            },
        ), mock.patch(
            "processing_app.views._processing_discrepancy_rows",
            return_value=[{"sku": "SKU-A", "expected_qty": 10, "actual_qty": 9}],
        ):
            result = ProcessingWorkflowService.finish_processing(
                order_id=order_id,
                entries=entries,
                request=request,
                role="processing_head",
            )

        self.assertEqual(result.status, "discrepancy_reported_head")
        status_entry = None
        for entry in OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing").order_by("-id"):
            payload = entry.payload or {}
            if isinstance(payload, dict) and payload.get("discrepancy_status"):
                status_entry = entry
                break
        self.assertIsNotNone(status_entry)
        self.assertEqual((status_entry.payload or {}).get("discrepancy_status"), "reported")
        self.assertFalse(
            Task.objects.filter(
                route=f"/orders/processing/{order_id}/",
                assigned_to__role="processing_head",
                title=f"Разногласие по обработке №{order_id}",
            ).exclude(status="done").exists()
        )

    def test_finish_processing_returns_done_and_closes_processing_tasks(self):
        order_id = "622"
        entries = self._create_ready_processing_entries(order_id)
        Task.objects.create(
            title=f"Processing order #{order_id}",
            route=f"/orders/processing/{order_id}/",
            assigned_to=Employee.objects.get(user=self.user),
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            data={"action": "finish_processing"},
        )
        request.user = self.user

        with mock.patch(
            "processing_app.views._processing_finish_checks",
            return_value={
                "blockers": [],
                "placement_payload": {"act_boxes": [{"code": "BOX-SERVICE-1"}], "act_pallets": [{"code": "PAL-SERVICE-1"}]},
                "placement_closed": True,
                "has_boxes": True,
                "has_pallets": True,
                "warehouse_move_created": True,
                "warehouse_move_completed": True,
                "warehouse_move_progress": {"total_pallets": 1, "done_count": 1},
            },
        ), mock.patch("processing_app.views._processing_discrepancy_rows", return_value=[]):
            result = ProcessingWorkflowService.finish_processing(
                order_id=order_id,
                entries=entries,
                request=request,
                role="processing_head",
            )

        self.assertEqual(result.status, "done")
        latest = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
            .order_by("-id")
            .first()
        )
        self.assertIsNotNone(latest)
        self.assertEqual((latest.payload or {}).get("status"), "done")
        self.assertFalse(
            Task.objects.filter(route=f"/orders/processing/{order_id}/").exclude(status="done").exists()
        )

    def test_finish_processing_releases_warehouse_reserve_when_processing_closes(self):
        order_id = "622-WH-REFRESH"
        entries = self._create_ready_processing_entries(order_id)
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
            sku_code="SKU-A",
            size="42",
            goods_type="gv",
            qty_reserved=10,
            status=WarehouseReserve.STATUS_ACTIVE,
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            data={"action": "finish_processing"},
        )
        request.user = self.user

        with mock.patch(
            "processing_app.views._processing_finish_checks",
            return_value={
                "blockers": [],
                "placement_payload": {"act_boxes": [{"code": "BOX-SERVICE-1"}], "act_pallets": [{"code": "PAL-SERVICE-1"}]},
                "placement_closed": True,
                "has_boxes": True,
                "has_pallets": True,
                "warehouse_move_created": True,
                "warehouse_move_completed": True,
                "warehouse_move_progress": {"total_pallets": 1, "done_count": 1},
            },
        ), mock.patch("processing_app.views._processing_discrepancy_rows", return_value=[]):
            result = ProcessingWorkflowService.finish_processing(
                order_id=order_id,
                entries=entries,
                request=request,
                role="processing_head",
            )

        self.assertEqual(result.status, "done")
        reserve = WarehouseReserve.objects.get(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
        )
        self.assertEqual(reserve.status, WarehouseReserve.STATUS_RELEASED)

    def test_build_processing_card_page_context_returns_selected_card_context(self):
        order_id = "623"
        entries = self._create_ready_processing_entries(order_id)
        request = self.request_factory.get(
            f"/orders/processing/{order_id}/card/card-a/",
            data={"return": f"/orders/processing/{order_id}/work/"},
        )
        request.user = self.user

        context = ProcessingWorkflowService.build_processing_card_page_context(
            order_id=order_id,
            card_id="card-a",
            request=request,
            payload=entries[0].payload or {},
            agency=self.agency,
        )

        self.assertEqual(context["card"]["article"], "SKU-A")
        self.assertEqual(context["return_url"], f"/orders/processing/{order_id}/work/")
        self.assertTrue(bool(context["card_processed"]))
        self.assertIn(f"/orders/processing/{order_id}/card/card-a/technical/", context["technical_card_url"])

    def test_handle_processing_card_action_marks_card_done(self):
        order_id = "624"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "cards": [{"id": "card-a", "article": "SKU-A", "rows": [{"size": "42", "qty": "10"}]}],
            },
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/card/card-a/",
            data={"action": "finish_card", "card_id": "card-a"},
        )
        request.user = self.user

        result = ProcessingWorkflowService.handle_processing_card_action(
            order_id=order_id,
            request=request,
            action="finish_card",
        )

        self.assertEqual(result.status, "ok")
        latest = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
            .order_by("-id")
            .first()
        )
        self.assertIsNotNone(latest)
        payload = latest.payload or {}
        self.assertIn("card-a", payload.get("processed_cards") or [])
        card = next(
            (
                item
                for item in (payload.get("cards") or [])
                if isinstance(item, dict) and item.get("id") == "card-a"
            ),
            None,
        )
        self.assertIsNotNone(card)
        self.assertTrue(bool(card.get("processed_done")))

    def test_scan_processing_flow_marking_returns_not_required_when_cz_disabled(self):
        order_id = "625"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "cards": [{"id": "card-a", "article": "SKU-A", "rows": [{"size": "42", "qty": "10"}]}],
            },
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/marking-scan/",
            data=json.dumps({"code": "CZ-1", "box_barcode": "BOX-1", "agent_id": "agent-1"}),
            content_type="application/json",
        )
        request.user = self.user

        result = ProcessingWorkflowService.scan_processing_flow_marking(
            order_id=order_id,
            request=request,
        )

        self.assertEqual(result.status, "not_required")
        self.assertEqual(result.http_status, 400)
        self.assertEqual(result.payload.get("error"), "ЧЗ не требуется.")

    def test_build_processing_technical_card_page_context_builds_checks(self):
        order_id = "626"
        entries = self._create_ready_processing_entries(order_id)
        payload = dict(entries[0].payload or {})
        payload.update(
            {
                "measure_needed": "Да",
                "measure_weight": "100",
                "marking_5840_qty": "2",
                "bubble_wrap_supply": "Клиент",
            }
        )
        request = self.request_factory.get(f"/orders/processing/{order_id}/card/card-a/technical/")
        request.user = self.user
        base_ctx = ProcessingWorkflowService.build_processing_card_page_context(
            order_id=order_id,
            card_id="card-a",
            request=request,
            payload=payload,
            agency=self.agency,
        )

        context = ProcessingWorkflowService.build_processing_technical_card_page_context(
            ctx=base_ctx,
            order_id=order_id,
            card_id="card-a",
            request=request,
            payload=payload,
        )

        self.assertTrue(bool(context["tech_checks"]["measure_yes"]))
        self.assertTrue(bool(context["tech_checks"]["marking_sticker_2"]))
        self.assertTrue(bool(context["tech_checks"]["bubble_wrap_supply_client"]))
        self.assertEqual(context["tech"]["measure_weight"], "100")
        self.assertIn(f"/orders/processing/{order_id}/card/card-a/", context["card_page_url"])

    def test_build_processing_label_print_page_context_enriches_rows_and_status(self):
        order_id = "627"
        payload = {
            "status": "processing_in_work",
            "status_label": "Взята в работу",
            "marking_5840_qty": "2",
            "marking_5840_each_qty": "1",
            "cards": [
                {
                    "id": "card-a",
                    "article": "SKU-A",
                    "rows": [{"size": "42", "barcode": "BAR-42", "qty": "10"}],
                }
            ],
        }
        request = self.request_factory.get(f"/orders/processing/{order_id}/card/card-a/labels/")
        request.user = self.user
        base_ctx = ProcessingWorkflowService.build_processing_card_page_context(
            order_id=order_id,
            card_id="card-a",
            request=request,
            payload=payload,
            agency=self.agency,
        )
        MarkingCode.objects.create(
            agency=self.agency,
            code="CZ-CODE-1",
            barcode="BAR-42",
            size="42",
            order_type="processing",
            order_id=order_id,
            sku_code="SKU-A",
        )
        ProcessingPrintJob.objects.create(
            order_id=order_id,
            card_id="card-a",
            article="SKU-A",
            barcode="BAR-42",
            size="42",
            printer_name="Printer",
            label_png_base64="xxx",
            label_width_mm=58,
            label_height_mm=40,
            status=ProcessingPrintJob.STATUS_PENDING,
        )

        context = ProcessingWorkflowService.build_processing_label_print_page_context(
            ctx=base_ctx,
            order_id=order_id,
            card_id="card-a",
            request=request,
            payload=payload,
            agency=self.agency,
        )

        self.assertEqual(context["label_rows"][0]["print_qty_no_cz"], 20)
        self.assertEqual(context["label_rows"][0]["print_qty_cz"], 10)
        self.assertIn("CZ-CODE-1", context["label_rows"][0]["cz_codes"])
        self.assertEqual(context["print_queue_pending"], 1)
        self.assertIn(f"/orders/processing/{order_id}/card/card-a/", context["processing_card_url"])

    def test_build_processing_detail_page_context_prefers_warehouse_status_label(self):
        order_id = "628"
        entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_head",
                "status_label": "Передано в обработку",
                "article": "SKU-PROC",
                "product_name": "Товар обработки",
            },
        )
        processing_location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OBR",
        )
        WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="legacy_stock",
            source_context_id=f"legacy-{order_id}",
            sku_code="SKU-PROC",
            name="Processing Item",
            size="42",
            barcode="BC-PROC-42",
            goods_type="gv",
            qty=10,
            available_qty=0,
            processing_reserved_qty=10,
            container_code=f"PAL-{order_id}",
            location=processing_location,
            zone_code=processing_location.zone_code,
            zone_kind=processing_location.zone_kind,
            warehouse_state_code="processing_in_progress",
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
            sku_code="SKU-PROC",
            size="42",
            barcode="BC-PROC-42",
            goods_type="gv",
            qty_reserved=10,
            status=WarehouseReserve.STATUS_ACTIVE,
            source_document_type="processing_order",
            source_document_id=order_id,
            created_by=self.user,
        )
        request = self.request_factory.get(f"/orders/processing/{order_id}/")
        request.user = self.user
        base_ctx = {"agency": self.agency, "client_view": False}

        context = ProcessingWorkflowService.build_processing_detail_page_context(
            ctx=base_ctx,
            order_id=order_id,
            entries_list=[entry],
            request=request,
            payload_from_entries=lambda entries: entries[-1].payload or {},
        )

        self.assertEqual(context["status_label"], "Товар в обработке")
        self.assertIn("/orders/processing/628/work/", context["processing_work_url"])

    def test_handle_processing_detail_action_take_processing_redirects_to_work(self):
        order_id = "629"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_head",
                "status_label": "Передано в обработку",
                "article": "SKU-PROC",
            },
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/",
            data={"action": "take_processing"},
        )
        request.user = self.user

        result = ProcessingWorkflowService.handle_processing_detail_action(
            order_id=order_id,
            request=request,
            order_type="processing",
            payload_from_entries=lambda entries: entries[-1].payload or {},
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.redirect_to, f"/orders/processing/{order_id}/work/")

    def test_build_processing_detail_page_context_hides_manager_actions_when_warehouse_already_started(self):
        order_id = "629-WH"
        entry = OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "submitted",
                "status_label": "На подтверждении",
                "article": "SKU-PROC",
            },
        )
        self._create_processing_snapshot(order_id=order_id, state_code="processing_in_progress")
        manager_user = get_user_model().objects.create_user(username="proc_manager_ctx", password="x")
        Employee.objects.create(
            full_name="Processing Manager",
            user=manager_user,
            role="manager",
            is_active=True,
        )
        request = self.request_factory.get(f"/orders/processing/{order_id}/")
        request.user = manager_user
        base_ctx = {"agency": self.agency, "client_view": False}

        context = ProcessingWorkflowService.build_processing_detail_page_context(
            ctx=base_ctx,
            order_id=order_id,
            entries_list=[entry],
            request=request,
            payload_from_entries=lambda entries: entries[-1].payload or {},
        )

        self.assertEqual(context["status_label"], "Товар в обработке")
        self.assertFalse(context["can_approve_processing"])
        self.assertFalse(context["can_edit_processing"])

    def test_take_processing_redirects_to_work_when_payload_is_stale_but_warehouse_in_progress(self):
        order_id = "629-WH-TAKE"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "submitted",
                "status_label": "На подтверждении",
                "article": "SKU-PROC",
            },
        )
        self._create_processing_snapshot(order_id=order_id, state_code="processing_in_progress")

        request = self.request_factory.post(
            f"/orders/processing/{order_id}/",
            data={"action": "take_processing"},
        )
        request.user = self.user

        result = ProcessingWorkflowService.handle_processing_detail_action(
            order_id=order_id,
            request=request,
            order_type="processing",
            payload_from_entries=lambda entries: entries[-1].payload or {},
        )

        self.assertEqual(result.status, "already_in_progress")
        self.assertEqual(result.redirect_to, f"/orders/processing/{order_id}/work/")

    def test_approve_processing_is_noop_when_warehouse_already_started(self):
        order_id = "629-WH-APPROVE"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "submitted",
                "status_label": "На подтверждении",
                "article": "SKU-PROC",
            },
        )
        self._create_processing_snapshot(order_id=order_id, state_code="reserved_for_processing")
        manager_user = get_user_model().objects.create_user(username="proc_manager_approve", password="x")
        Employee.objects.create(
            full_name="Processing Approver",
            user=manager_user,
            role="manager",
            is_active=True,
        )
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/",
            data={"action": "approve_processing"},
        )
        request.user = manager_user

        result = ProcessingWorkflowService.handle_processing_detail_action(
            order_id=order_id,
            request=request,
            order_type="processing",
            payload_from_entries=lambda entries: entries[-1].payload or {},
        )

        self.assertEqual(result.status, "done")
        self.assertEqual(result.redirect_to, f"/orders/processing/{order_id}/")
        self.assertEqual(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing", payload__status="processing_head").count(),
            0,
        )

    def test_processing_marking_availability_counts_reserved_and_free_codes(self):
        manager_user = get_user_model().objects.create_user(username="proc_marking_manager", password="x")
        Employee.objects.create(
            full_name="Processing Marking Manager",
            user=manager_user,
            role="manager",
            is_active=True,
        )
        request = self.request_factory.post("/orders/processing/marking-availability/")
        request.user = manager_user
        MarkingCode.objects.create(
            agency=self.agency,
            code="MC-1",
            barcode="BAR-1",
            order_type="processing",
            order_id="ORD-1",
        )
        MarkingCode.objects.create(
            agency=self.agency,
            code="MC-2",
            barcode="BAR-1",
            order_type="processing",
            order_id="",
        )

        result = ProcessingWorkflowService.processing_marking_availability(
            request=request,
            data={
                "order_id": "ORD-1",
                "agency_id": self.agency.id,
                "items": [{"barcode": "BAR-1", "qty": 3}, {"qty": 2}],
            },
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.payload["required"], 3)
        self.assertEqual(result.payload["available"], 2)
        self.assertEqual(result.payload["free"], 1)
        self.assertEqual(result.payload["missing"], 1)
        self.assertEqual(result.payload["missing_barcodes"], 2)

    def test_processing_marking_import_returns_success_payload(self):
        manager_user = get_user_model().objects.create_user(username="proc_import_manager", password="x")
        Employee.objects.create(
            full_name="Processing Import Manager",
            user=manager_user,
            role="manager",
            is_active=True,
        )
        request = self.request_factory.post(
            "/orders/processing/marking-import/",
            data={"agency_id": str(self.agency.id)},
        )
        request.user = manager_user
        upload = SimpleUploadedFile("cz.xlsx", b"fake-xlsx", content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        with mock.patch("processing_app.views._import_marking_codes", return_value=(True, {"created": 2, "updated": 1})):
            result = ProcessingWorkflowService.processing_marking_import(
                request=request,
                cz_file=upload,
                cards_payload=[{"id": "card-a"}],
                order_id="ORD-2",
            )

        self.assertEqual(result.status, "ok")
        self.assertTrue(result.payload["ok"])
        self.assertEqual(result.payload["created"], 2)

    def test_enqueue_processing_print_job_creates_job(self):
        request = self.request_factory.post("/orders/processing/print-jobs/enqueue/")
        request.user = self.user

        result = ProcessingWorkflowService.enqueue_processing_print_job(
            request=request,
            data={
                "order_id": "ORD-3",
                "card_id": "card-a",
                "article": "SKU-A",
                "barcode": "BAR-42",
                "size": "42",
                "printer_name": "Printer",
                "label_png_base64": "abc123",
            },
        )

        self.assertEqual(result.status, "ok")
        self.assertTrue(ProcessingPrintJob.objects.filter(pk=result.payload["job_id"]).exists())

    def test_processing_print_jobs_next_moves_pending_job_to_printing(self):
        ProcessingPrintJob.objects.create(
            order_id="ORD-4",
            card_id="card-a",
            article="SKU-A",
            barcode="BAR-42",
            size="42",
            printer_name="Printer",
            label_png_base64="abc123",
            label_width_mm=58,
            label_height_mm=40,
            status=ProcessingPrintJob.STATUS_PENDING,
        )

        result = ProcessingWorkflowService.processing_print_jobs_next(agent_name="agent-1")

        self.assertEqual(result.status, "ok")
        self.assertTrue(result.payload["has_job"])
        job = ProcessingPrintJob.objects.get()
        self.assertEqual(job.status, ProcessingPrintJob.STATUS_PRINTING)
        self.assertEqual(job.agent, "agent-1")

    def test_processing_print_jobs_complete_updates_job_status(self):
        job = ProcessingPrintJob.objects.create(
            order_id="ORD-5",
            card_id="card-a",
            article="SKU-A",
            barcode="BAR-42",
            size="42",
            printer_name="Printer",
            label_png_base64="abc123",
            label_width_mm=58,
            label_height_mm=40,
            status=ProcessingPrintJob.STATUS_PRINTING,
        )

        result = ProcessingWorkflowService.processing_print_jobs_complete(
            data={"job_id": job.id, "status": "failed", "error": "boom"},
        )

        self.assertEqual(result.status, "ok")
        job.refresh_from_db()
        self.assertEqual(job.status, ProcessingPrintJob.STATUS_FAILED)
        self.assertEqual(job.error, "boom")

    def test_processing_print_jobs_reset_rejects_invalid_mode(self):
        request = self.request_factory.post("/orders/processing/print-jobs/reset/")
        request.user = self.user

        result = ProcessingWorkflowService.processing_print_jobs_reset(
            request=request,
            data={"mode": "weird"},
        )

        self.assertEqual(result.status, "invalid_mode")
        self.assertEqual(result.http_status, 400)

    def test_build_processing_home_page_context_exposes_draft_payload(self):
        order_id = "630"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="create",
            agency=self.agency,
            payload={
                "status": "draft",
                "status_label": "Черновик",
                "submit_action": "draft",
                "product_name": "Draft product",
            },
        )
        request = self.request_factory.get(
            "/orders/processing/",
            data={"client": self.agency.id, "order": order_id},
        )
        request.user = self.user

        context = ProcessingWorkflowService.build_processing_home_page_context(
            request=request,
            submitted=False,
            draft_saved=False,
            error="",
        )

        self.assertEqual(context["draft_order_id"], order_id)
        self.assertEqual(context["status_label"], "Черновик")
        self.assertEqual(context["draft_payload"]["product_name"], "Draft product")

    def test_build_processing_home_page_context_prefers_warehouse_status_for_edit_order(self):
        order_id = "630-WH"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "submitted",
                "status_label": "На подтверждении",
                "product_name": "Warehouse product",
            },
        )
        self._create_processing_snapshot(order_id=order_id, state_code="processing_in_progress")
        manager_user = get_user_model().objects.create_user(username="proc_home_manager", password="x")
        Employee.objects.create(
            full_name="Processing Home Manager",
            user=manager_user,
            role="manager",
            is_active=True,
        )
        request = self.request_factory.get(
            "/orders/processing/",
            data={"agency": self.agency.id, "order": order_id, "edit": "1"},
        )
        request.user = manager_user

        context = ProcessingWorkflowService.build_processing_home_page_context(
            request=request,
            submitted=False,
            draft_saved=False,
            error="",
        )

        self.assertEqual(context["edit_order_id"], order_id)
        self.assertEqual(context["status_label"], "Товар в обработке")

    def test_build_processing_directions_page_context_keeps_return_url(self):
        request = self.request_factory.get(
            "/orders/processing/directions/",
            data={"return": "/orders/processing/123/work/"},
        )
        request.user = self.user

        context = ProcessingWorkflowService.build_processing_directions_page_context(
            request=request,
        )

        self.assertEqual(context["return_url"], "/orders/processing/123/work/")
        self.assertEqual(context["return_url_json"], "\"/orders/processing/123/work/\"")

    def test_build_processing_stock_picker_page_context_uses_referer_order(self):
        order_id = "631"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={"status": "processing_in_work", "status_label": "Взята в работу"},
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="500",
            sku="SKU-RES",
            name="Reserved item",
            size="42",
            goods_type="Не обработанный",
            qty=50,
            available_qty=0,
            processing_reserved_qty=50,
            pallet_code="PAL-RES-631",
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
            sku_code="SKU-RES",
            size="42",
            goods_type="Не обработанный",
            qty_reserved=50,
            status=WarehouseReserve.STATUS_ACTIVE,
        )
        request = self.request_factory.get(
            "/orders/processing/stock/",
            data={"client": self.agency.id},
            HTTP_REFERER=f"http://testserver/orders/processing/{order_id}/work/",
        )
        request.user = self.user

        context = ProcessingWorkflowService.build_processing_stock_picker_page_context(
            request=request,
        )

        items = json.loads(context["inventory_items_json"] or "[]")
        qty = sum(int(item.get("qty") or 0) for item in items if (item.get("sku") or "") == "SKU-RES")
        self.assertEqual(qty, 50)
        self.assertEqual(context["return_url"], f"/orders/processing/?client={self.agency.id}")

    def test_delete_processing_draft_removes_entries_and_releases_marking_codes(self):
        order_id = "draft-delete-1"
        portal_user = get_user_model().objects.create_user(username="processing_portal_user", password="x")
        self.agency.portal_user = portal_user
        self.agency.save(update_fields=["portal_user"])
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="create",
            agency=self.agency,
            payload={"status": "draft", "status_label": "Черновик", "submit_action": "draft"},
        )
        marking_code = MarkingCode.objects.create(
            agency=self.agency,
            code="CZ-DRAFT-1",
            barcode="BAR-DRAFT",
            order_type="processing",
            order_id=order_id,
        )
        request = self.request_factory.post(f"/orders/processing/drafts/{order_id}/delete/")
        request.user = portal_user

        response = ProcessingWorkflowService.delete_processing_draft(
            request=request,
            order_id=order_id,
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing").exists())
        marking_code.refresh_from_db()
        self.assertEqual(marking_code.order_id, "")

    def test_submit_processing_autosaves_draft(self):
        request = self.request_factory.post(
            "/orders/processing/",
            data={
                "agency_id": str(self.agency.id),
                "submit_action": "draft",
                "draft_autosave": "1",
                "product_name": "Draft processing product",
            },
        )
        request.user = self.user

        response = ProcessingWorkflowService.submit_processing(request=request)

        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content.decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertTrue(str(data["draft_order_id"]).startswith("draft-"))
        latest = OrderAuditEntry.objects.filter(order_id=data["order_id"], order_type="processing").order_by("-id").first()
        self.assertIsNotNone(latest)
        self.assertEqual((latest.payload or {}).get("status"), "draft")

    def test_submit_processing_client_submission_creates_manager_task(self):
        manager_user = get_user_model().objects.create_user(username="proc_home_manager", password="x")
        manager_employee = Employee.objects.create(
            full_name="Processing Home Manager",
            user=manager_user,
            role="manager",
            is_active=True,
        )
        request = self.request_factory.post(
            "/orders/processing/",
            data={
                "submit_action": "send",
                "product_name": "Processing order product",
                "article": "SKU-HOME-1",
                "stock_article[]": ["SKU-HOME-1"],
                "stock_size[]": ["42"],
                "stock_barcode[]": ["BAR-HOME-1"],
                "stock_qty[]": ["0"],
            },
        )
        request.user = self.user
        request._client_agency = self.agency

        response = ProcessingWorkflowService.submit_processing(request=request)

        self.assertEqual(response.status_code, 302)
        latest = OrderAuditEntry.objects.filter(order_type="processing", agency=self.agency).order_by("-id").first()
        self.assertIsNotNone(latest)
        order_id = latest.order_id
        self.assertTrue(
            Task.objects.filter(
                route=f"/orders/processing/{order_id}/",
                assigned_to=manager_employee,
            ).exclude(status="done").exists()
        )

    def test_submit_processing_rejects_edit_when_warehouse_already_started(self):
        order_id = "630-EDIT-WH"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "submitted",
                "status_label": "На подтверждении",
                "product_name": "Warehouse processing product",
            },
        )
        self._create_processing_snapshot(order_id=order_id, state_code="processing_in_progress")
        manager_user = get_user_model().objects.create_user(username="proc_edit_manager", password="x")
        Employee.objects.create(
            full_name="Processing Edit Manager",
            user=manager_user,
            role="manager",
            is_active=True,
        )
        request = self.request_factory.post(
            "/orders/processing/",
            data={
                "agency_id": str(self.agency.id),
                "edit_order_id": order_id,
                "draft_autosave": "1",
                "product_name": "Updated processing product",
            },
        )
        request.user = manager_user

        response = ProcessingWorkflowService.submit_processing(request=request)

        self.assertEqual(response.status_code, 400)
        data = json.loads(response.content.decode("utf-8"))
        self.assertFalse(data["ok"])
        self.assertIn("недоступна для редактирования", data["error"])
        self.assertEqual(
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing").count(),
            1,
        )


class ProcessingFlowServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="proc_flow_service_user", password="x")
        Employee.objects.create(
            full_name="Processing Flow Service User",
            user=self.user,
            role="processing_head",
            is_active=True,
        )
        self.request_factory = RequestFactory()
        self.agency = Agency.objects.create(agn_name="Processing Flow Service Agency")

    def _create_entries(self, order_id: str, *, flow_closed: bool = False):
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "cards": [{"id": "card-a", "article": "SKU-A", "rows": [{"size": "42", "qty": "10"}]}],
                "processed_cards": ["card-a"],
                "processing_results": [
                    {"card_id": "card-a", "article": "SKU-A", "size": "42", "destination": "-", "processed": "10"},
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="update",
            agency=self.agency,
            payload={
                "act": "placement",
                "flow_closed": flow_closed,
                "act_boxes": [{"code": "BOX-FLOW-1", "items": [{"sku": "SKU-A", "qty": 10}]}],
                "act_pallets": [{"code": "PAL-FLOW-1", "boxes": ["BOX-FLOW-1"], "items": []}],
                "flow_closed_at": timezone.localtime().isoformat() if flow_closed else "",
            },
        )
        return list(OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing").order_by("created_at"))

    @staticmethod
    def _normalize_flow_state(boxes, pallets, active_box, active_pallet):
        return {
            "boxes": boxes,
            "pallets": pallets,
            "activeBox": active_box,
            "activePallet": active_pallet,
        }

    def test_save_processing_flow_draft_creates_session(self):
        order_id = "623"
        entries = self._create_entries(order_id)
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/",
            data={
                "boxes_json": '[{"code":"BOX-1","items":[{"sku":"SKU-A","qty":10}]}]',
                "pallets_json": '[{"code":"PAL-1","boxes":["BOX-1"],"items":[]}]',
                "active_box": "BOX-1",
                "active_pallet": "PAL-1",
                "agent_id": "agent-1",
            },
        )
        request.user = self.user

        with mock.patch("processing_app.views.ProcessingFlowView._can_start", return_value=True):
            result = ProcessingWorkflowService.save_processing_flow_draft(
                order_id=order_id,
                entries=entries,
                request=request,
                normalize_flow_state=self._normalize_flow_state,
                can_reassign_boxes=True,
            )

        self.assertEqual(result.status, "ok")
        session = ProcessingFlowSession.objects.get(order_id=order_id, agent_id="agent-1")
        self.assertEqual(session.flow_state.get("activeBox"), "BOX-1")
        self.assertEqual(session.flow_state.get("activePallet"), "PAL-1")

    def test_reopen_processing_flow_creates_reopen_snapshot_session(self):
        order_id = "624"
        entries = self._create_entries(order_id, flow_closed=True)
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/",
            data={"flow_action": "reopen"},
        )
        request.user = self.user

        result = ProcessingWorkflowService.reopen_processing_flow(
            order_id=order_id,
            entries=entries,
            request=request,
            normalize_flow_state=self._normalize_flow_state,
        )

        self.assertEqual(result.status, "ok")
        seed_session = ProcessingFlowSession.objects.get(order_id=order_id, agent_id="__reopen_snapshot__")
        self.assertEqual(seed_session.status, ProcessingFlowSession.STATUS_OPEN)
        self.assertEqual(len(seed_session.flow_state.get("boxes") or []), 1)

    def test_get_processing_flow_shared_state_uses_reopened_placement_without_open_sessions(self):
        order_id = "625"
        self._create_entries(order_id, flow_closed=True)
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="update",
            agency=self.agency,
            payload={"flow_reopened": True},
        )

        result = ProcessingWorkflowService.get_processing_flow_shared_state(order_id=order_id)

        self.assertEqual(result.status, "ok")
        self.assertEqual(len(result.flow_state["boxes"]), 1)
        self.assertEqual(result.flow_state["boxes"][0]["code"], "BOX-FLOW-1")

    def test_log_processing_flow_box_action_writes_staff_overaction(self):
        order_id = "626"
        self._create_entries(order_id)
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/box-action/",
            data=json.dumps(
                {
                    "action": "edit",
                    "box_code": "BOX-FLOW-1",
                    "pallet_index": 1,
                    "box_index": 1,
                    "total_qty": 10,
                }
            ),
            content_type="application/json",
        )
        request.user = self.user

        result = ProcessingWorkflowService.log_processing_flow_box_action(
            order_id=order_id,
            request=request,
        )

        self.assertEqual(result.status, "ok")
        overaction = AuditEntry.objects.filter(journal__code="staff_overactions").latest("id")
        self.assertEqual(overaction.snapshot.get("box_code"), "BOX-FLOW-1")

    def test_complete_processing_flow_rejects_unassigned_boxes(self):
        order_id = "627"
        entries = self._create_entries(order_id)
        request = self.request_factory.post(
            f"/orders/processing/{order_id}/flow/",
            data={
                "boxes_json": '[{"code":"BOX-1","items":[{"sku":"SKU-A","qty":10}]}]',
                "pallets_json": '[{"code":"PAL-1","boxes":[],"items":[{"sku":"SKU-X","qty":1}]}]',
            },
        )
        request.user = self.user

        with mock.patch("processing_app.views._state_has_open_box_with_items", return_value=False):
            result = ProcessingWorkflowService.complete_processing_flow(
                order_id=order_id,
                entries=entries,
                request=request,
                normalize_flow_state=self._normalize_flow_state,
                items_from_placement_act=lambda _entries: [{"sku_code": "SKU-A", "name": "Item", "size": "42", "actual_qty": 10}],
                can_start=True,
                can_finish_with_mismatch=True,
                can_reassign_boxes=True,
            )

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_code, "unassigned_boxes")

    def test_build_processing_flow_page_context_uses_locked_placement_state(self):
        order_id = "628"
        entries = self._create_entries(order_id, flow_closed=True)
        request = self.request_factory.get(f"/orders/processing/{order_id}/flow/")
        request.user = self.user

        context = ProcessingWorkflowService.build_processing_flow_page_context(
            order_id=order_id,
            entries=entries,
            request=request,
            can_finish_flow=True,
            can_finish_flow_mismatch=True,
            can_reassign_boxes=True,
            is_directional_unboxing_payload=lambda payload: False,
            items_from_placement_act=lambda _entries: [{"sku_code": "SKU-A", "name": "Item", "size": "42", "actual_qty": 10}],
            normalize_flow_state=self._normalize_flow_state,
            find_flow_state=lambda _entries: {},
            placement_act_entry=lambda _entries: _entries[-1],
            ok=False,
            error="",
        )

        self.assertTrue(context["flow_locked"])
        self.assertEqual(len(context["flow_state"]["boxes"]), 1)
        self.assertEqual(context["act_print_url"], f"/orders/processing/{order_id}/placement/?return=/orders/processing/{order_id}/flow/")


class ProcessingFlowTemplateSelectionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="ph_user_flow_tpl", password="x")
        Employee.objects.create(
            full_name="Processing Head Flow Template",
            user=self.user,
            role="processing_head",
            is_active=True,
        )
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Flow Template Agency")

    def _create_order(self, order_id: str, payload: dict):
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload=payload,
        )

    @staticmethod
    def _base_ready_payload() -> dict:
        return {
            "status": "processing_in_work",
            "status_label": "Взята в работу",
            "cards": [
                {"id": "card-a", "article": "SKU-A", "rows": [{"size": "42", "qty": "10"}]},
            ],
            "processed_cards": ["card-a"],
            "processing_results": [
                {"card_id": "card-a", "article": "SKU-A", "size": "42", "destination": "-", "processed": "10"},
            ],
        }

    def test_flow_uses_directional_template_when_direction_distribution_exists(self):
        order_id = "811"
        payload = self._base_ready_payload()
        payload["direction_addresses_json"] = ["Москва", "Томск"]
        payload["direction_plan_json"] = {
            "directions": ["Москва", "Томск"],
            "rows": [{"article": "SKU-A", "size": "42", "quantities": [4, 6]}],
        }
        self._create_order(order_id, payload)

        response = self.client.get(f"/orders/processing/{order_id}/flow/")
        self.assertEqual(response.status_code, 200)
        template_names = {t.name for t in response.templates if getattr(t, "name", "")}
        self.assertIn("processing/processing_flow_directions.html", template_names)

    def test_flow_uses_standard_template_without_direction_distribution(self):
        order_id = "812"
        payload = self._base_ready_payload()
        self._create_order(order_id, payload)

        response = self.client.get(f"/orders/processing/{order_id}/flow/")
        self.assertEqual(response.status_code, 200)
        template_names = {t.name for t in response.templates if getattr(t, "name", "")}
        self.assertIn("processing/processing_flow.html", template_names)
        self.assertNotIn("processing/processing_flow_directions.html", template_names)


class ProcessingFlowOperationalStockTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="ph_user_flow_stock", password="x")
        Employee.objects.create(
            full_name="Processing Head Flow Stock",
            user=self.user,
            role="processing_head",
            is_active=True,
        )
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Flow Stock Agency")

    def test_flow_close_writes_finished_goods_to_operational_stock(self):
        order_id = "813"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "goods_type": "gv",
                "goods_type_label": "Готовый",
                "cards": [
                    {
                        "id": "card-a",
                        "article": "SKU-FLOW",
                        "rows": [{"size": "42", "qty": "10", "barcode": "FLOW-BAR-1"}],
                    },
                ],
                "processed_cards": ["card-a"],
                "processing_results": [
                    {
                        "card_id": "card-a",
                        "article": "SKU-FLOW",
                        "name": "Flow Item",
                        "size": "42",
                        "barcode": "FLOW-BAR-1",
                        "destination": "-",
                        "processed": "10",
                    },
                ],
            },
        )

        response = self.client.post(
            f"/orders/processing/{order_id}/flow/",
            {
                "boxes_json": (
                    '[{"code":"BOX-FLOW-1","items":[{"sku":"SKU-FLOW","sku_code":"SKU-FLOW",'
                    '"name":"Flow Item","size":"42","barcode":"FLOW-BAR-1","qty":10}],'
                    '"sealed":true}]'
                ),
                "pallets_json": (
                    '[{"code":"PAL-FLOW-1","boxes":["BOX-FLOW-1"],"items":[],"sealed":true,'
                    '"location":{"zone":"OS","row":3,"section":2,"tier":1,"cell":4}}]'
                ),
            },
        )

        self.assertEqual(response.status_code, 302)
        snapshot = WarehouseStockSnapshot.objects.get(
            agency=self.agency,
            source_context_type="processing",
            source_context_id=order_id,
        )
        self.assertEqual(snapshot.sku_code, "SKU-FLOW")
        self.assertEqual(snapshot.container.container_code, "BOX-FLOW-1")
        self.assertEqual(snapshot.parent_container.container_code, "PAL-FLOW-1")
        self.assertEqual(snapshot.zone_code, "OBR")
        self.assertEqual(snapshot.goods_type, "Готовый")
        self.assertEqual(snapshot.warehouse_state_code, WarehouseStateCode.IN_PROCESSING_ZONE.value)
        self.assertEqual(snapshot.available_qty, 10)


class ProcessingPackagingAssignmentFlowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="ph_user_packaging", password="x")
        Employee.objects.create(
            full_name="Processing Head Packaging",
            user=self.user,
            role="processing_head",
            is_active=True,
        )
        self.worker = Employee.objects.create(
            full_name="Worker One",
            role="processing_worker",
            is_active=True,
        )
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Packaging Flow Agency")

    def _create_order(self, order_id: str, payload: dict):
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload=payload,
        )

    @staticmethod
    def _base_payload() -> dict:
        return {
            "status": "processing_in_work",
            "status_label": "Взята в работу",
            "cards": [
                {"id": "card-a", "article": "SKU-A", "rows": [{"size": "42", "qty": "10"}]},
                {"id": "card-b", "article": "SKU-B", "rows": [{"size": "43", "qty": "10"}]},
            ],
        }

    def test_assign_packaging_deferred_until_flow_conditions_are_ready(self):
        order_id = "701"
        payload = self._base_payload()
        payload["processed_cards"] = ["card-a"]
        payload["processing_results"] = [
            {"card_id": "card-a", "article": "SKU-A", "size": "42", "destination": "-", "processed": "10"},
            {"card_id": "card-b", "article": "SKU-B", "size": "43", "destination": "-", "processed": "10"},
        ]
        self._create_order(order_id, payload)

        response = self.client.post(
            f"/orders/processing/{order_id}/assign-packaging/",
            {"assignee_id": str(self.worker.id)},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("assign=deferred", response.url)
        self.assertFalse(
            Task.objects.filter(route=f"/orders/processing/{order_id}/flow/", assigned_to=self.worker)
            .exclude(status="done")
            .exists()
        )
        latest = (
            OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing")
            .order_by("-id")
            .first()
        )
        self.assertIsNotNone(latest)
        self.assertEqual(
            str((latest.payload or {}).get("packing_assignment_state") or "").strip().lower(),
            "pending",
        )

    def test_pending_packaging_task_auto_dispatches_when_work_page_is_ready(self):
        order_id = "702"
        payload = self._base_payload()
        payload["processed_cards"] = ["card-a"]
        payload["processing_results"] = [
            {"card_id": "card-a", "article": "SKU-A", "size": "42", "destination": "-", "processed": "10"},
            {"card_id": "card-b", "article": "SKU-B", "size": "43", "destination": "-", "processed": "10"},
        ]
        self._create_order(order_id, payload)
        self.client.post(
            f"/orders/processing/{order_id}/assign-packaging/",
            {"assignee_id": str(self.worker.id)},
        )
        ready_payload = self._base_payload()
        ready_payload["processed_cards"] = ["card-a", "card-b"]
        ready_payload["processing_results"] = [
            {"card_id": "card-a", "article": "SKU-A", "size": "42", "destination": "-", "processed": "10"},
            {"card_id": "card-b", "article": "SKU-B", "size": "43", "destination": "-", "processed": "10"},
        ]
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload=ready_payload,
        )

        response = self.client.get(f"/orders/processing/{order_id}/work/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context.get("assign_status"), "auto_dispatched")
        self.assertTrue(
            Task.objects.filter(route=f"/orders/processing/{order_id}/flow/", assigned_to=self.worker)
            .exclude(status="done")
            .exists()
        )
        latest_assignment_state_entry = None
        for entry in OrderAuditEntry.objects.filter(order_id=order_id, order_type="processing").order_by("-id"):
            payload = entry.payload or {}
            if isinstance(payload, dict) and "packing_assignment_state" in payload:
                latest_assignment_state_entry = entry
                break
        self.assertIsNotNone(latest_assignment_state_entry)
        self.assertEqual(
            str((latest_assignment_state_entry.payload or {}).get("packing_assignment_state") or "").strip().lower(),
            "dispatched",
        )

    def test_assign_packaging_creates_task_immediately_when_ready(self):
        order_id = "703"
        ready_payload = self._base_payload()
        ready_payload["processed_cards"] = ["card-a", "card-b"]
        ready_payload["processing_results"] = [
            {"card_id": "card-a", "article": "SKU-A", "size": "42", "destination": "-", "processed": "10"},
            {"card_id": "card-b", "article": "SKU-B", "size": "43", "destination": "-", "processed": "10"},
        ]
        self._create_order(order_id, ready_payload)

        response = self.client.post(
            f"/orders/processing/{order_id}/assign-packaging/",
            {"assignee_id": str(self.worker.id)},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("assign=ok", response.url)
        self.assertTrue(
            Task.objects.filter(route=f"/orders/processing/{order_id}/flow/", assigned_to=self.worker)
            .exclude(status="done")
            .exists()
        )


class ProcessingStockPickerReserveExclusionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="manager_stock_picker", password="x")
        Employee.objects.create(
            full_name="Manager Stock Picker",
            user=self.user,
            role="manager",
            is_active=True,
        )
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Stock Picker Agency")
        self.order_id = "9901"
        OrderAuditEntry.objects.create(
            order_id=self.order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={"status": "processing_in_work", "status_label": "Взята в работу"},
        )
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="500",
            sku="SKU-RES",
            name="Reserved item",
            size="42",
            goods_type="Не обработанный",
            qty=50,
            available_qty=0,
            processing_reserved_qty=50,
            pallet_code="PAL-RES-9901",
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id=self.order_id,
            sku_code="SKU-RES",
            size="42",
            goods_type="Не обработанный",
            qty_reserved=50,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

    def _stock_picker_qty(self, response) -> int:
        self.assertEqual(response.status_code, 200)
        items = json.loads(response.context["inventory_items_json"] or "[]")
        return sum(int(item.get("qty") or 0) for item in items if (item.get("sku") or "") == "SKU-RES")

    def test_stock_picker_without_order_context_keeps_item_unavailable(self):
        response = self.client.get(f"/orders/processing/stock/?client={self.agency.id}")
        self.assertEqual(self._stock_picker_qty(response), 0)

    def test_stock_picker_uses_referer_processing_order_for_reserve_exclusion(self):
        response = self.client.get(
            f"/orders/processing/stock/?client={self.agency.id}",
            HTTP_REFERER=f"http://testserver/orders/processing/{self.order_id}/work/",
        )
        self.assertEqual(self._stock_picker_qty(response), 50)

    def test_stock_picker_uses_card_referer_processing_order_for_reserve_exclusion(self):
        response = self.client.get(
            f"/orders/processing/stock/?client={self.agency.id}",
            HTTP_REFERER=(
                "http://testserver/orders/processing/"
                f"{self.order_id}/card/card_1772333219271_0/?article=SKU-RES"
            ),
        )
        self.assertEqual(self._stock_picker_qty(response), 50)

    def test_stock_picker_hides_stock_reserved_for_shipping(self):
        WarehouseReserve.objects.filter(agency=self.agency).delete()
        WarehouseStockSnapshot.objects.filter(agency=self.agency, sku_code="SKU-RES").update(
            available_qty=0,
            processing_reserved_qty=0,
            shipping_reserved_qty=50,
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_SHIPPING,
            context_type="shipping",
            context_id="SO-RES-1",
            sku_code="SKU-RES",
            size="42",
            goods_type="Не обработанный",
            qty_reserved=50,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

        response = self.client.get(f"/orders/processing/stock/?client={self.agency.id}")

        self.assertEqual(self._stock_picker_qty(response), 0)


class ProcessingStockAvailabilityContractTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Contract Agency")
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="901",
            sku="SKU-CONTRACT",
            name="Contract Item",
            size="44",
            goods_type="Не обработанный",
            qty=70,
            available_qty=10,
            processing_reserved_qty=60,
            pallet_code="PAL-CONTRACT",
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="5001",
            sku_code="SKU-CONTRACT",
            size="44",
            goods_type="Не обработанный",
            qty_reserved=50,
            status=WarehouseReserve.STATUS_ACTIVE,
        )
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="5002",
            sku_code="SKU-CONTRACT",
            size="44",
            goods_type="Не обработанный",
            qty_reserved=10,
            status=WarehouseReserve.STATUS_ACTIVE,
        )

    @staticmethod
    def _qty(items: list[dict]) -> int:
        return sum(
            int(item.get("qty") or 0)
            for item in items
            if (item.get("sku") or "").strip() == "SKU-CONTRACT"
        )

    def test_processing_app_wrapper_matches_stock_service_default(self):
        service_items = StockAvailabilityService.inventory_items_for_agency(self.agency)
        processing_items = _inventory_items_for_agency(self.agency)
        self.assertEqual(processing_items, service_items)
        self.assertEqual(self._qty(service_items), 10)

    def test_processing_app_wrapper_matches_stock_service_with_exclusion(self):
        service_items = StockAvailabilityService.inventory_items_for_agency(
            self.agency,
            exclude_processing_order_id="5001",
        )
        processing_items = _inventory_items_for_agency(self.agency, exclude_order_id="5001")
        self.assertEqual(processing_items, service_items)
        self.assertEqual(self._qty(service_items), 60)


class ProcessingReserveMaterializedAvailabilityTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="Reserve Refresh Agency")
        create_warehouse_snapshot_row(
            agency=self.agency,
            order_type="receiving",
            order_id="910",
            sku="SKU-RESERVE",
            name="Reserve Item",
            size="44",
            goods_type="Не обработанный",
            qty=70,
            available_qty=70,
            pallet_code="PAL-RESERVE-1",
            row=1,
            section=1,
            tier=1,
            cell=2,
        )

    def test_replace_processing_reserves_updates_available_qty_for_affected_key(self):
        _replace_processing_reserves(
            "P-100",
            self.agency,
            [
                {
                    "sku": "SKU-RESERVE",
                    "size": "44",
                    "goods_type": "Не обработанный",
                    "qty": 50,
                }
            ],
        )

        snapshot = WarehouseStockSnapshot.objects.get(agency=self.agency, sku_code="SKU-RESERVE", size="44")
        self.assertEqual(snapshot.processing_reserved_qty, 50)
        self.assertEqual(snapshot.shipping_reserved_qty, 0)
        self.assertEqual(snapshot.available_qty, 20)

    def test_replace_processing_reserves_syncs_warehouse_reserve_when_pallet_known(self):
        _replace_processing_reserves(
            "P-101",
            self.agency,
            [
                {
                    "sku": "SKU-RESERVE",
                    "size": "44",
                    "goods_type": "Не обработанный",
                    "qty": 30,
                }
            ],
        )

        warehouse_reserve = WarehouseReserve.objects.get(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="P-101",
        )
        snapshot = WarehouseStockSnapshot.objects.get(
            agency=self.agency,
            container_code="PAL-RESERVE-1",
            sku_code="SKU-RESERVE",
            size="44",
        )
        self.assertEqual(warehouse_reserve.qty_reserved, 30)
        self.assertEqual(snapshot.processing_reserved_qty, 30)
        self.assertEqual(snapshot.available_qty, 40)
        self.assertEqual(snapshot.warehouse_state_code, "reserved_for_processing")

    def test_processing_reserve_rows_for_order_return_outstanding_warehouse_qty(self):
        WarehouseReserve.objects.create(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id="P-102",
            sku_code="SKU-RESERVE",
            size="44",
            goods_type="Не обработанный",
            qty_reserved=30,
            qty_satisfied=8,
            status=WarehouseReserve.STATUS_PARTIALLY_SATISFIED,
        )

        rows = _processing_reserve_rows_for_order("P-102", self.agency)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sku"], "SKU-RESERVE")
        self.assertEqual(rows[0]["qty"], 22)


class ProcessingWarehouseOperationBridgeTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="proc_bridge_user", password="x")
        Employee.objects.create(
            full_name="Processing Bridge User",
            user=self.user,
            role="processing_head",
            is_active=True,
        )
        self.client.force_login(self.user)
        self.agency = Agency.objects.create(agn_name="Processing Bridge Agency")
        self.request_factory = RequestFactory()
        self.processing_location = WarehouseWritePathService.ensure_location(
            warehouse_code="MSK",
            zone_code="OBR",
        )

    def _create_processing_snapshot(
        self,
        *,
        order_id: str,
        state_code: str,
        active_operation: WarehouseOperation | None = None,
    ) -> WarehouseStockSnapshot:
        snapshot = WarehouseStockSnapshot.objects.create(
            agency=self.agency,
            source_context_type="legacy_stock",
            source_context_id=f"legacy-{order_id}",
            sku_code="SKU-PROC",
            name="Processing Item",
            size="42",
            barcode="BC-PROC-42",
            goods_type="gv",
            qty=10,
            available_qty=0,
            processing_reserved_qty=10,
            container_code=f"PAL-{order_id}",
            location=self.processing_location,
            zone_code=self.processing_location.zone_code,
            zone_kind=self.processing_location.zone_kind,
            warehouse_state_code=state_code,
            active_operation=active_operation,
            active_operation_type=active_operation.operation_type if active_operation else "",
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
            source_document_type="processing_order",
            source_document_id=order_id,
            created_by=self.user,
        )
        return snapshot

    def test_take_processing_starts_warehouse_processing_when_goods_are_in_obr(self):
        order_id = "P-200"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_head",
                "status_label": "Передано в обработку",
                "cards": [{"id": "card-a", "article": "SKU-PROC", "rows": [{"size": "42", "qty": "10"}]}],
            },
        )
        snapshot = self._create_processing_snapshot(
            order_id=order_id,
            state_code="in_processing_zone",
        )

        response = self.client.post(
            f"/orders/processing/{order_id}/",
            {"action": "take_processing"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"/orders/processing/{order_id}/work/")
        operation = WarehouseOperation.objects.get(
            agency=self.agency,
            operation_type=WarehouseOperation.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
        )
        snapshot.refresh_from_db()
        self.assertEqual(operation.status, WarehouseOperation.STATUS_IN_PROGRESS)
        self.assertEqual(snapshot.warehouse_state_code, "processing_in_progress")
        self.assertEqual(snapshot.active_operation_id, operation.id)

    def test_take_processing_redirects_to_work_when_warehouse_already_in_progress(self):
        order_id = "P-200-INPROGRESS"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_head",
                "status_label": "Передано в обработку",
                "cards": [{"id": "card-a", "article": "SKU-PROC", "rows": [{"size": "42", "qty": "10"}]}],
            },
        )
        self._create_processing_snapshot(
            order_id=order_id,
            state_code="processing_in_progress",
        )

        response = self.client.post(
            f"/orders/processing/{order_id}/",
            {"action": "take_processing"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"/orders/processing/{order_id}/work/")
        self.assertEqual(
            WarehouseOperation.objects.filter(
                agency=self.agency,
                operation_type=WarehouseOperation.TYPE_PROCESSING,
                context_type="processing",
                context_id=order_id,
            ).count(),
            0,
        )

    def test_processing_work_page_prefers_warehouse_status_label(self):
        order_id = "P-200A"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_head",
                "status_label": "Передано в обработку",
                "cards": [{"id": "card-a", "article": "SKU-PROC", "rows": [{"size": "42", "qty": "10"}]}],
            },
        )
        self._create_processing_snapshot(
            order_id=order_id,
            state_code="processing_in_progress",
        )

        response = self.client.get(f"/orders/processing/{order_id}/work/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Товар в обработке")

    def test_processing_work_page_allows_stale_payload_when_warehouse_in_progress(self):
        order_id = "P-200A-WH-ONLY"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "draft",
                "status_label": "Черновик",
                "cards": [{"id": "card-a", "article": "SKU-PROC", "rows": [{"size": "42", "qty": "10"}]}],
            },
        )
        self._create_processing_snapshot(
            order_id=order_id,
            state_code="processing_in_progress",
        )

        response = self.client.get(f"/orders/processing/{order_id}/work/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Товар в обработке")

    def test_processing_detail_page_prefers_warehouse_status_label(self):
        order_id = "P-200B"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_head",
                "status_label": "Передано в обработку",
                "article": "SKU-PROC",
                "product_name": "Товар обработки",
            },
        )
        self._create_processing_snapshot(
            order_id=order_id,
            state_code="processing_in_progress",
        )

        response = self.client.get(f"/orders/processing/{order_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Товар в обработке")

    def test_processing_card_page_allows_stale_payload_when_warehouse_in_progress(self):
        order_id = "P-200B-WH-ONLY"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "draft",
                "status_label": "Черновик",
                "cards": [{"id": "card-a", "article": "SKU-PROC", "rows": [{"size": "42", "qty": "10"}]}],
            },
        )
        self._create_processing_snapshot(
            order_id=order_id,
            state_code="processing_in_progress",
        )

        response = self.client.get(f"/orders/processing/{order_id}/card/card-a/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["status_label"], "Товар в обработке")

    def test_processing_work_completion_finishes_warehouse_processing_operation(self):
        order_id = "P-201"
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "cards": [{"id": "card-a", "article": "SKU-PROC", "rows": [{"size": "42", "qty": "10"}]}],
                "processed_cards": ["card-a"],
                "processing_results": [
                    {
                        "card_id": "card-a",
                        "article": "SKU-PROC",
                        "size": "42",
                        "destination": "-",
                        "processed": "10",
                    }
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id=order_id,
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "status": "processing_in_work",
                "status_label": "Взята в работу",
                "cards": [{"id": "card-a", "article": "SKU-PROC", "rows": [{"size": "42", "qty": "10"}]}],
                "processed_cards": ["card-a"],
                "processing_results": [
                    {
                        "card_id": "card-a",
                        "article": "SKU-PROC",
                        "size": "42",
                        "destination": "-",
                        "processed": "10",
                    }
                ],
                "act": "placement",
                "act_state": "closed",
                "act_boxes": [{"code": "BOX-PROC-1", "items": [{"sku": "SKU-PROC", "qty": 10}]}],
                "act_pallets": [
                    {
                        "code": "PAL-PROC-1",
                        "boxes": ["BOX-PROC-1"],
                        "items": [],
                        "location": {"zone": "MR", "row": 1, "section": "", "tier": "", "cell": ""},
                    }
                ],
            },
        )
        operation = WarehouseOperation.objects.create(
            agency=self.agency,
            operation_type=WarehouseOperation.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
            source_location=self.processing_location,
            destination_location=self.processing_location,
            source_zone_code=self.processing_location.zone_code,
            destination_zone_code=self.processing_location.zone_code,
            status=WarehouseOperation.STATUS_IN_PROGRESS,
            planned_qty=10,
            started_at=timezone.now(),
        )
        snapshot = self._create_processing_snapshot(
            order_id=order_id,
            state_code="processing_in_progress",
            active_operation=operation,
        )

        request = self.request_factory.post(
            f"/orders/processing/{order_id}/work/",
            {"action": "finish_processing"},
        )
        request.user = self.user
        view = ProcessingWorkView()
        view.setup(request, order_id=order_id)
        with mock.patch(
            "processing_app.views._processing_finish_checks",
            return_value={
                "blockers": [],
                "placement_payload": {
                    "act_boxes": [{"code": "BOX-PROC-1"}],
                    "act_pallets": [{"code": "PAL-PROC-1"}],
                },
                "placement_closed": True,
                "has_boxes": True,
                "has_pallets": True,
                "warehouse_move_created": True,
                "warehouse_move_completed": True,
                "warehouse_move_progress": {
                    "total_pallets": 1,
                    "done_count": 1,
                    "created_count": 0,
                    "in_progress_count": 0,
                    "active_count": 0,
                    "canceled_count": 0,
                    "not_created_count": 0,
                    "pending_count": 0,
                    "moves_by_pallet": {},
                    "implicit_done_codes": {"PAL-PROC-1"},
                },
            },
        ), mock.patch("processing_app.views._processing_discrepancy_rows", return_value=[]):
            response = view.post(request, order_id=order_id)
        self.assertEqual(response.status_code, 302)
        operation.refresh_from_db()
        snapshot.refresh_from_db()
        reserve = WarehouseReserve.objects.get(
            agency=self.agency,
            reserve_type=WarehouseReserve.TYPE_PROCESSING,
            context_type="processing",
            context_id=order_id,
        )
        self.assertEqual(operation.status, WarehouseOperation.STATUS_DONE)
        self.assertEqual(snapshot.warehouse_state_code, "placed_after_processing")
        self.assertEqual(snapshot.processing_reserved_qty, 0)
        self.assertEqual(snapshot.available_qty, 10)
        self.assertIsNone(snapshot.active_operation_id)
        self.assertEqual(reserve.status, WarehouseReserve.STATUS_RELEASED)
