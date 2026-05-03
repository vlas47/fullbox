import json
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from employees.access import get_request_role
from .models import AgentCommand, AgentContext, AgentEvent, DeviceAgent
from .services import (
    agent_allowed as agent_allowed_service,
    agent_command_ack_response,
    agent_commands_response,
    agent_context_claim_response,
    agent_context_release_response,
    agent_event_response,
    agent_events_poll_response,
    agent_forbidden as agent_forbidden_service,
    agent_ping_response,
    agent_status_response,
)


def _parse_json_body(request):
    try:
        body = request.body.decode("utf-8")
    except (AttributeError, UnicodeDecodeError):
        return None
    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _get_agent_id(data):
    if not isinstance(data, dict):
        return ""
    value = data.get("agent_id")
    if value is None:
        value = data.get("agentId")
    return str(value or "").strip()


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _extract_token(request):
    header = request.headers.get("X-Agent-Token") or request.META.get("HTTP_X_AGENT_TOKEN")
    if header:
        return header.strip()
    auth = request.headers.get("Authorization") or request.META.get("HTTP_AUTHORIZATION")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _agent_allowed(request) -> bool:
    return agent_allowed_service(request)


def _agent_forbidden():
    return agent_forbidden_service()


def _touch_agent(agent_id: str, request):
    if not agent_id:
        return
    now = timezone.now()
    updated = DeviceAgent.objects.filter(agent_id=agent_id).update(
        last_seen=now,
        last_ip=_client_ip(request),
    )
    if not updated:
        DeviceAgent.objects.create(
            agent_id=agent_id,
            name="",
            host="",
            version="",
            last_seen=now,
            last_ip=_client_ip(request),
            meta={},
        )


def _context_ttl_seconds() -> int:
    try:
        ttl = int(getattr(settings, "AGENT_CONTEXT_TTL", 30))
    except (TypeError, ValueError):
        ttl = 30
    return max(5, min(ttl, 300))


def _agent_online_state(agent_id: str) -> tuple[bool, str, dict]:
    if not agent_id:
        return False, "", {}
    agent = DeviceAgent.objects.filter(agent_id=agent_id).first()
    if not agent or not agent.last_seen:
        return False, "", {}
    online_threshold = timezone.now() - timedelta(seconds=30)
    return agent.last_seen >= online_threshold, agent.last_seen.isoformat(), agent.meta or {}


def _extract_scanner_state(meta: dict) -> dict:
    if not isinstance(meta, dict):
        return {"ready": None, "reason": "", "port": "", "error": ""}
    com_health = meta.get("com_health")
    if isinstance(com_health, dict):
        return {
            "ready": com_health.get("ready") if isinstance(com_health.get("ready"), bool) else None,
            "reason": str(com_health.get("reason") or ""),
            "port": str(com_health.get("port") or ""),
            "error": str(com_health.get("error") or ""),
        }
    com_status = meta.get("com_status")
    if isinstance(com_status, dict):
        connected = com_status.get("connected") if isinstance(com_status.get("connected"), bool) else None
        error = str(com_status.get("error") or "")
        return {
            "ready": connected if isinstance(connected, bool) else None,
            "reason": "connected" if connected else "not_connected",
            "port": "",
            "error": error,
        }
    return {"ready": None, "reason": "", "port": "", "error": ""}


@login_required
@require_GET
def agent_status(request):
    return agent_status_response(request)


def _get_active_context(agent_id: str):
    if not agent_id:
        return None
    now = timezone.now()
    return (
        AgentContext.objects.filter(agent_id=agent_id, active=True, expires_at__gt=now)
        .order_by("-last_seen", "-updated_at")
        .first()
    )


def _context_owner_payload(ctx: AgentContext) -> dict:
    if not ctx:
        return {}
    user_label = ""
    if ctx.user_id:
        user = ctx.user
        if user:
            user_label = user.get_full_name() or user.username or ""
    return {
        "user_id": ctx.user_id,
        "user": user_label,
        "role": ctx.role or "",
        "order_id": ctx.order_id,
        "box_id": ctx.box_id or "",
        "updated_at": ctx.updated_at.isoformat() if ctx.updated_at else "",
    }


@login_required
@require_POST
def agent_context_claim(request):
    return agent_context_claim_response(request)


@login_required
@require_POST
def agent_context_release(request):
    return agent_context_release_response(request)


@csrf_exempt
@require_POST
def agent_ping(request):
    return agent_ping_response(request)


@csrf_exempt
@require_GET
def agent_commands(request):
    return agent_commands_response(request)


@csrf_exempt
@require_POST
def agent_command_ack(request, command_id: int):
    return agent_command_ack_response(request, command_id)


@csrf_exempt
@require_POST
def agent_event(request):
    return agent_event_response(request)


@login_required
@require_GET
def agent_events_poll(request):
    return agent_events_poll_response(request)
