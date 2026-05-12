from pathlib import Path

from django.test import SimpleTestCase


class ProcessingScannerAgentTemplateRegressionTests(SimpleTestCase):
    def _template_source(self, template_name):
        template_path = Path(__file__).resolve().parent / "templates" / "processing" / template_name
        return template_path.read_text(encoding="utf-8")

    def test_processing_flow_templates_render_scanner_agents_and_local_bridge(self):
        for template_name in ("processing_flow.html", "processing_flow_directions.html"):
            with self.subTest(template_name=template_name):
                template_source = self._template_source(template_name)
                self.assertIn("const populateAgentSelect = () => {", template_source)
                self.assertIn("const upsertScannerAgentOption = (agentInfo) => {", template_source)
                self.assertIn("http://127.0.0.1:17841/whoami", template_source)
                self.assertIn("const scheduleLocalAgentSelectionRefresh = (delay = 0) => {", template_source)
                self.assertIn("refreshLocalAgentSelection().catch(() => {});", template_source)
                self.assertIn("status: agentOnline ? 'онлайн' : 'нет связи',", template_source)
                self.assertNotIn("setAgentStatus(scannerState.text, isError);", template_source)


class ReceivingScannerAgentTemplateRegressionTests(SimpleTestCase):
    def test_receiving_flow_refreshes_local_agent_after_initial_page_load(self):
        template_path = (
            Path(__file__).resolve().parent.parent
            / "orders"
            / "templates"
            / "orders"
            / "receiving_flow.html"
        )
        template_source = template_path.read_text(encoding="utf-8")

        self.assertIn("http://127.0.0.1:17841/whoami", template_source)
        self.assertIn("const scheduleLocalAgentSelectionRefresh = (delay = 0) => {", template_source)
        self.assertIn("refreshLocalAgentSelection().catch(() => {});", template_source)
