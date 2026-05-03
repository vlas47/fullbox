import json

from django.contrib.auth import get_user_model
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, TestCase, override_settings

from employees.models import Employee

from .models import AgentCommand, AgentContext, DeviceAgent
from .services import (
    agent_commands_response,
    agent_context_claim_response,
    agent_ping_response,
    agent_status_response,
)


class AgentServiceTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = get_user_model().objects.create_user(username="agent-user", password="secret")
        Employee.objects.create(user=self.user, full_name="Agent User", role="storekeeper")

    def _with_session(self, request):
        middleware = SessionMiddleware(lambda req: None)
        middleware.process_request(request)
        request.session.save()
        return request

    def test_agent_status_response_requires_agent_id(self):
        request = self.factory.get("/agent/status/")
        request.user = self.user
        request = self._with_session(request)

        response = agent_status_response(request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["error"], "missing_agent_id")

    def test_agent_context_claim_response_creates_context(self):
        request = self.factory.post(
            "/agent/contexts/claim/",
            data=json.dumps({"agent_id": "scanner-1", "order_id": 42, "box_id": "BOX-7"}),
            content_type="application/json",
        )
        request.user = self.user
        request = self._with_session(request)

        response = agent_context_claim_response(request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(AgentContext.objects.filter(agent_id="scanner-1", order_id=42, box_id="BOX-7").exists())

    @override_settings(DEBUG=True, AGENT_SHARED_TOKEN="", PRINT_AGENT_TOKEN="")
    def test_agent_commands_response_delivers_pending_commands(self):
        AgentCommand.objects.create(agent_id="scanner-1", command="scan", payload={"x": 1})
        request = self.factory.get("/agent/commands/?agent_id=scanner-1")
        request.user = self.user
        request = self._with_session(request)

        response = agent_commands_response(request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["commands"]), 1)
        self.assertEqual(AgentCommand.objects.get().status, AgentCommand.STATUS_DELIVERED)

    @override_settings(DEBUG=True, AGENT_SHARED_TOKEN="", PRINT_AGENT_TOKEN="")
    def test_agent_ping_response_upserts_device_agent(self):
        request = self.factory.post(
            "/agent/ping/",
            data=json.dumps({"agent_id": "scanner-2", "name": "Scanner 2", "meta": {"com_health": {"ready": True}}}),
            content_type="application/json",
        )
        request.user = self.user
        request = self._with_session(request)

        response = agent_ping_response(request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        agent = DeviceAgent.objects.get(agent_id="scanner-2")
        self.assertEqual(agent.name, "Scanner 2")
        self.assertEqual(agent.meta["com_health"]["ready"], True)
