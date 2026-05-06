from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import RequestFactory
from django.test import TestCase
from django.utils import timezone

from agent.models import AgentCommand, DeviceAgent
from processing_app.models import ProcessingPrintJob

from labels.services import (
    build_label_settings_context,
    scanner_settings_apply_response,
    scanner_test_response,
)
from labels.utils import (
    build_print_status_snapshot,
    load_available_printers_data,
    load_print_agent_status,
    save_print_agent_status,
)


class AvailablePrintersFromAgentTests(TestCase):
    def test_load_available_printers_uses_active_agent_printers_without_json_sync(self):
        DeviceAgent.objects.create(
            agent_id="pc-001",
            name="Warehouse-PC-01",
            host="WH-01",
            last_seen=timezone.now(),
            meta={"printers": ["Zebra GK420d", "TSC TE200", "Zebra GK420d"]},
        )

        with TemporaryDirectory() as tmp_dir:
            missing_path = Path(tmp_dir) / "available_printers.json"
            with patch("labels.utils.available_printers_path", return_value=missing_path):
                printers, meta = load_available_printers_data()

        self.assertEqual(printers, ["Zebra GK420d", "TSC TE200"])
        self.assertEqual(meta.get("source"), "agent")
        self.assertEqual(meta.get("updated_by"), "Warehouse-PC-01")

    def test_build_print_status_snapshot_reports_queue_and_printer_states(self):
        DeviceAgent.objects.create(
            agent_id="pc-001",
            name="Warehouse-PC-01",
            host="WH-01",
            last_seen=timezone.now(),
            meta={
                "printers": ["Zebra GK420d", "Microsoft Print to PDF"],
                "printer_details": [
                    {
                        "name": "Zebra GK420d",
                        "is_default": True,
                        "is_offline": False,
                        "is_paused": False,
                        "is_busy": False,
                        "is_local": True,
                        "is_network": False,
                        "jobs": 0,
                        "status": "",
                    },
                    {
                        "name": "Microsoft Print to PDF",
                        "is_default": False,
                        "is_offline": False,
                        "is_paused": False,
                        "is_busy": True,
                        "is_local": True,
                        "is_network": False,
                        "jobs": 1,
                        "status": "busy",
                    },
                ],
                "printer_details_updated_at": timezone.now().isoformat(),
            },
        )
        ProcessingPrintJob.objects.create(
            barcode="111",
            label_png_base64="ZmFrZQ==",
            printer_name="Zebra GK420d",
            status=ProcessingPrintJob.STATUS_PENDING,
        )
        printing_job = ProcessingPrintJob.objects.create(
            barcode="222",
            label_png_base64="ZmFrZQ==",
            printer_name="Microsoft Print to PDF",
            status=ProcessingPrintJob.STATUS_PRINTING,
        )
        ProcessingPrintJob.objects.filter(pk=printing_job.pk).update(
            updated_at=timezone.now() - timedelta(minutes=5)
        )
        ProcessingPrintJob.objects.create(
            barcode="333",
            label_png_base64="ZmFrZQ==",
            printer_name="Zebra GK420d",
            status=ProcessingPrintJob.STATUS_FAILED,
            error="paper jam",
        )

        snapshot = build_print_status_snapshot()

        self.assertEqual(snapshot["print_queue_pending"], 1)
        self.assertEqual(snapshot["print_queue_printing"], 1)
        self.assertEqual(snapshot["print_queue_failed"], 1)
        self.assertEqual(snapshot["print_queue_stuck"], 1)
        self.assertIn("зависшие задания", snapshot["print_status_line"].lower())

        by_name = {item["name"]: item for item in snapshot["printer_statuses"]}
        self.assertEqual(by_name["Zebra GK420d"]["state_key"], "ready")
        self.assertEqual(by_name["Microsoft Print to PDF"]["state_key"], "busy")
        self.assertTrue(by_name["Microsoft Print to PDF"]["is_virtual"])


class LabelServiceTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_build_label_settings_context_contains_updated_pallet_size(self):
        context = build_label_settings_context()

        pallet = next((item for item in context["label_sizes"] if item.get("key") == "pallet"), None)
        self.assertIsNotNone(pallet)
        self.assertEqual(pallet["width_mm"], 58)
        self.assertEqual(pallet["height_mm"], 60)

    def test_label_settings_template_keeps_product_and_storage_groups_visible(self):
        context = build_label_settings_context()
        context.update(
            {
                "cabinet_url": "/team-manager/",
                "return_url": "/team-manager/",
                "workspace_role_label": "Сотрудник",
                "return_label": "В кабинет",
            }
        )

        html = render_to_string("labels/settings.html", context)

        self.assertIn("Товарные этикетки", html)
        self.assertIn("Складские этикетки", html)
        self.assertIn('data-label-key="item_5860"', html)
        self.assertIn('data-label-key="item_75120"', html)
        self.assertIn('data-label-key="pallet"', html)
        self.assertIn("compact-printer-card", html)
        self.assertIn("label-printer-quick", html)
        self.assertIn("data-print-refresh", html)
        self.assertIn("data-compact-status", html)
        self.assertIn("data-preview-block", html)
        self.assertIn('data-label-width="75"', html)
        self.assertIn('data-label-height="120"', html)

    def test_build_label_settings_context_exposes_online_agent_and_ports(self):
        DeviceAgent.objects.create(
            agent_id="pc-002",
            name="Scanner-PC",
            host="WH-02",
            version="1.2.3",
            last_seen=timezone.now(),
            meta={"ports": ["COM3"], "com": {"enabled": True, "port": "COM3", "baud": 9600}},
        )

        context = build_label_settings_context()

        self.assertEqual(context["scanner_ports"], ["COM3"])
        self.assertEqual(context["scanner_agents"][0]["agent_id"], "pc-002")
        self.assertTrue(context["scanner_agents"][0]["is_online"])

    def test_scanner_settings_apply_response_creates_command_for_target_agent(self):
        DeviceAgent.objects.create(agent_id="pc-003", last_seen=timezone.now())

        response = scanner_settings_apply_response(
            body=b'{"agent_id":"pc-003","settings":{"default":{"enabled":true,"port":"COM4","baud":115200,"eol":"LF","idle_ms":50}}}'
        )

        self.assertEqual(response.status_code, 200)
        command = AgentCommand.objects.get()
        self.assertEqual(command.agent_id, "pc-003")
        self.assertEqual(command.command, "scanner.config")
        self.assertEqual(command.payload["port"], "COM4")

    def test_scanner_test_response_requires_agent(self):
        response = scanner_test_response(
            body=b'{"settings":{"default":{"enabled":true,"port":"COM4","baud":115200,"eol":"LF","idle_ms":50}}}'
        )

        self.assertEqual(response.status_code, 400)


class PrintAgentStatusTests(TestCase):
    def test_save_print_agent_status_throttles_repeated_heartbeat_writes(self):
        with TemporaryDirectory() as tmp_dir:
            status_path = Path(tmp_dir) / "print_agent_status.json"
            started_at = timezone.now()
            with patch("labels.utils.print_agent_status_path", return_value=status_path):
                first_payload = save_print_agent_status("agent-1", when=started_at)
                first_mtime = status_path.stat().st_mtime_ns

                second_payload = save_print_agent_status(
                    "agent-1",
                    when=started_at + timedelta(seconds=2),
                )
                second_mtime = status_path.stat().st_mtime_ns
                stored = load_print_agent_status()

        self.assertEqual(first_payload["agent"], "agent-1")
        self.assertEqual(second_payload["agent"], "agent-1")
        self.assertEqual(first_mtime, second_mtime)
        self.assertEqual(stored["agent"], "agent-1")
        self.assertEqual(stored["last_seen"], started_at.isoformat())

    def test_save_print_agent_status_updates_immediately_for_new_agent(self):
        with TemporaryDirectory() as tmp_dir:
            status_path = Path(tmp_dir) / "print_agent_status.json"
            started_at = timezone.now()
            with patch("labels.utils.print_agent_status_path", return_value=status_path):
                save_print_agent_status("agent-1", when=started_at)
                save_print_agent_status("agent-2", when=started_at + timedelta(seconds=1))
                stored = load_print_agent_status()

        self.assertEqual(stored["agent"], "agent-2")
        self.assertEqual(stored["last_seen"], (started_at + timedelta(seconds=1)).isoformat())
