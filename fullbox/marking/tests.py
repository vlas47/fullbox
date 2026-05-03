import json
from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase
from openpyxl import Workbook

from audit.models import OrderAuditEntry
from employees.models import Employee
from sku.models import Agency, SKU, SKUBarcode

from .models import MarkingCode
from .services import (
    processing_marking_import_response,
    processing_marking_scan_response,
    processing_marking_summary_response,
    receiving_marking_scan_response,
)


class MarkingServiceTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = get_user_model().objects.create_user(username="marking_user", password="pwd")
        Employee.objects.create(
            user=self.user,
            full_name="Маркировщик",
            role="storekeeper",
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Клиент маркировки")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-1",
            name="Куртка",
            size="42",
            honest_sign=True,
        )
        SKUBarcode.objects.create(sku=self.sku, value="2000000001000", size="42")
        OrderAuditEntry.objects.create(
            order_id="PROC-1",
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "stock_rows": [
                    {
                        "article": "SKU-1",
                        "size": "42",
                        "barcode": "2000000001000",
                        "qty": 2,
                    }
                ]
            },
        )
        OrderAuditEntry.objects.create(
            order_id="REC-1",
            order_type="receiving",
            action="status",
            agency=self.agency,
            payload={
                "receiving_mode": "cz",
                "items": [
                    {
                        "sku_code": "SKU-1",
                        "size": "42",
                        "barcode": "2000000001000",
                        "qty": 2,
                    }
                ],
            },
        )

    def _post_json(self, path: str, payload: dict):
        request = self.factory.post(
            path,
            data=json.dumps(payload),
            content_type="application/json",
        )
        request.user = self.user
        return request

    def test_processing_marking_summary_counts_used_codes(self):
        MarkingCode.objects.create(
            order_type="processing",
            order_id="PROC-1",
            agency=self.agency,
            sku=self.sku,
            sku_code="SKU-1",
            size="42",
            code="CZ-USED-1",
            used_at=OrderAuditEntry.objects.latest("id").created_at,
            used_by=self.user,
        )

        request = self.factory.get("/marking/processing/PROC-1/summary/")
        request.user = self.user
        response = processing_marking_summary_response(request=request, order_id="PROC-1")
        payload = json.loads(response.content)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["total_count"], 1)
        self.assertEqual(payload["items"][0]["sku_code"], "SKU-1")

    def test_processing_marking_scan_creates_used_code(self):
        request = self._post_json(
            "/marking/processing/PROC-1/scan/",
            {
                "code": "CZ-PROC-1",
                "sku_code": "SKU-1",
                "size": "42",
                "barcode": "2000000001000",
                "box_barcode": "BOX-1",
            },
        )

        response = processing_marking_scan_response(request=request, order_id="PROC-1")
        payload = json.loads(response.content)

        self.assertTrue(payload["ok"])
        code = MarkingCode.objects.get(code="CZ-PROC-1")
        self.assertEqual(code.order_type, "processing")
        self.assertEqual(code.order_id, "PROC-1")
        self.assertIsNotNone(code.used_at)

    def test_receiving_marking_scan_requires_open_box(self):
        request = self._post_json(
            "/marking/receiving/REC-1/scan/",
            {
                "code": "CZ-REC-1",
                "sku_code": "SKU-1",
                "size": "42",
                "barcode": "2000000001000",
            },
        )

        response = receiving_marking_scan_response(request=request, order_id="REC-1")
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["error"], "Откройте короб перед сканированием ЧЗ.")

    def test_processing_marking_import_creates_codes_from_xlsx(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["barcode", "code"])
        sheet.append(["2000000001000", "CZ-IMP-1"])
        buffer = BytesIO()
        workbook.save(buffer)
        upload = SimpleUploadedFile(
            "codes.xlsx",
            buffer.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        request = self.factory.post(
            "/marking/processing/PROC-1/import/",
            data={"file": upload},
        )
        request.user = self.user

        response = processing_marking_import_response(request=request, order_id="PROC-1")
        payload = json.loads(response.content)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["added"], 1)
        self.assertTrue(MarkingCode.objects.filter(code="CZ-IMP-1", order_id="PROC-1").exists())
