import json
from datetime import datetime, timedelta

from django.conf import settings
from django.http import FileResponse, HttpResponseBadRequest, JsonResponse
from django.utils import timezone
from django.views.generic import TemplateView
from django.views.decorators.http import require_GET, require_POST

from agent.models import AgentCommand, DeviceAgent
from employees.access import RoleRequiredMixin, role_required
from processing_app.models import ProcessingPrintJob
from .utils import (
    LABEL_FIELDS,
    LABEL_SIZE_KEYS,
    LABEL_SIZES,
    load_available_printers_data,
    clean_label_enabled,
    load_label_settings,
    load_print_agent_status,
    load_scanner_settings,
    normalize_scanner_settings,
    save_label_settings,
    save_scanner_settings,
)

AGENT_VERSION = "1.0.10"

ALLOWED_ROLES = ("head_manager", "director", "admin", "storekeeper", "processing_head", "manager")


class LabelSettingsView(RoleRequiredMixin, TemplateView):
    template_name = "labels/settings.html"
    allowed_roles = ALLOWED_ROLES

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        printers, printers_meta = load_available_printers_data()
        label_settings = load_label_settings()
        agent_status = load_print_agent_status()
        agent_name = str(agent_status.get("agent") or "").strip() or "неизвестно"
        last_seen_raw = agent_status.get("last_seen")
        last_seen_text = "нет данных"
        is_online = False
        if last_seen_raw:
            try:
                last_seen = datetime.fromisoformat(str(last_seen_raw))
                if timezone.is_naive(last_seen):
                    last_seen = timezone.make_aware(last_seen)
                last_seen_text = timezone.localtime(last_seen).strftime("%d.%m.%Y %H:%M:%S")
                is_online = (timezone.now() - last_seen) <= timedelta(seconds=20)
            except (TypeError, ValueError):
                last_seen_text = str(last_seen_raw)

        pending_count = ProcessingPrintJob.objects.filter(
            status=ProcessingPrintJob.STATUS_PENDING,
        ).count()
        printing_count = ProcessingPrintJob.objects.filter(
            status=ProcessingPrintJob.STATUS_PRINTING,
        ).count()
        failed_count = ProcessingPrintJob.objects.filter(
            status=ProcessingPrintJob.STATUS_FAILED,
        ).count()
        last_job = ProcessingPrintJob.objects.order_by("-updated_at").first()
        last_error = ""
        last_job_time = ""
        if last_job:
            last_job_time = timezone.localtime(last_job.updated_at).strftime("%d.%m.%Y %H:%M:%S")
            if last_job.status == ProcessingPrintJob.STATUS_FAILED:
                last_error = last_job.error or "ошибка без описания"

        paused = bool(agent_status.get("paused"))
        if paused:
            print_status = "Печать остановлена"
        elif pending_count:
            print_status = f"В очереди: {pending_count}"
            if not is_online:
                print_status = f"{print_status} (агент не активен)"
        elif last_job and last_job.status == ProcessingPrintJob.STATUS_FAILED:
            print_status = "Ошибка печати"
        elif last_job and last_job.status == ProcessingPrintJob.STATUS_PRINTING:
            print_status = "Печать выполняется"
        else:
            print_status = "Готов к печати"

        agent_line = f"{agent_name} · {last_seen_text}" if last_seen_text else agent_name
        label_sample = {
            "article": "КОВРИКИ001",
            "name": "Коврики универсальные",
            "size": "M",
            "brand": "Fullbox",
            "subject": "Коврики",
            "color": "Черный",
            "composition": "Полиэстер",
            "supplier": "Кондель",
            "country": "Россия",
            "barcode_extra": "SKU-0001",
        }
        scanner_settings = load_scanner_settings()
        scanner_default = scanner_settings.get("default") if isinstance(scanner_settings, dict) else {}
        scanner_default = scanner_default if isinstance(scanner_default, dict) else {}
        scanner_updated_raw = scanner_settings.get("updated_at") if isinstance(scanner_settings, dict) else None
        scanner_updated_text = "нет данных"
        if scanner_updated_raw:
            try:
                scanner_updated_at = datetime.fromisoformat(str(scanner_updated_raw))
                if timezone.is_naive(scanner_updated_at):
                    scanner_updated_at = timezone.make_aware(scanner_updated_at)
                scanner_updated_text = timezone.localtime(scanner_updated_at).strftime("%d.%m.%Y %H:%M:%S")
            except (TypeError, ValueError):
                scanner_updated_text = str(scanner_updated_raw)
        scanner_updated_by = ""
        if isinstance(scanner_settings, dict):
            scanner_updated_by = str(scanner_settings.get("updated_by") or "").strip()

        agent_items = []
        ports_pool = set()
        agent_qs = DeviceAgent.objects.all().order_by("-last_seen", "-updated_at")
        online_threshold = timezone.now() - timedelta(seconds=30)
        for agent in agent_qs:
            meta = agent.meta if isinstance(agent.meta, dict) else {}
            com_meta = meta.get("com") if isinstance(meta.get("com"), dict) else {}
            ports = meta.get("com_ports") or meta.get("ports")
            if isinstance(ports, str):
                ports = [ports]
            if not isinstance(ports, list):
                ports = []
            ports = [str(port).strip() for port in ports if str(port).strip()]
            for port in ports:
                ports_pool.add(port)
            last_seen_text = "нет данных"
            if agent.last_seen:
                try:
                    last_seen_text = timezone.localtime(agent.last_seen).strftime("%d.%m.%Y %H:%M:%S")
                except (TypeError, ValueError):
                    last_seen_text = str(agent.last_seen)
            is_online = bool(agent.last_seen and agent.last_seen >= online_threshold)
            agent_items.append(
                {
                    "agent_id": agent.agent_id,
                    "title": agent.name or agent.host or agent.agent_id,
                    "version": agent.version,
                    "last_seen": last_seen_text,
                    "is_online": is_online,
                    "status": "онлайн" if is_online else "нет связи",
                    "com": {
                        "enabled": com_meta.get("enabled"),
                        "port": com_meta.get("port"),
                        "baud": com_meta.get("baud"),
                        "eol": com_meta.get("eol"),
                        "idle_ms": com_meta.get("idle_ms") if com_meta.get("idle_ms") is not None else com_meta.get("idle"),
                    },
                    "ports": ports,
                }
            )
        ctx.update(
            {
                "label_sizes": LABEL_SIZES,
                "available_printers": printers,
                "available_printers_meta": printers_meta,
                "label_sample": label_sample,
                "label_sample_barcode": "4601234567890",
                "label_settings": label_settings,
                "print_status_line": print_status,
                "print_agent_line": agent_line,
                "print_last_error": last_error,
                "print_last_job_time": last_job_time,
                "print_queue_pending": pending_count,
                "print_queue_printing": printing_count,
                "print_queue_failed": failed_count,
                "print_paused": paused,
                "scanner_settings": scanner_settings,
                "scanner_default": scanner_default,
                "scanner_updated_at": scanner_updated_text,
                "scanner_updated_by": scanner_updated_by,
                "scanner_agents": agent_items,
                "scanner_ports": sorted(ports_pool),
            }
        )
        return ctx

    def post(self, request, *args, **kwargs):
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)
        key = (payload.get("key") or "").strip()
        if key not in LABEL_SIZE_KEYS:
            return JsonResponse({"ok": False, "error": "invalid_key"}, status=400)
        text_data = payload.get("text") if isinstance(payload.get("text"), dict) else {}
        font_data = payload.get("fonts") if isinstance(payload.get("fonts"), dict) else {}
        enabled_data = payload.get("enabled") if isinstance(payload.get("enabled"), dict) else None
        cleaned_text = {}
        for field in LABEL_FIELDS:
            if field in text_data:
                cleaned_text[field] = str(text_data.get(field) or "").strip()
        cleaned_fonts = {}
        for field in LABEL_FIELDS:
            if field not in font_data:
                continue
            try:
                value = float(str(font_data.get(field)).replace(",", "."))
            except (TypeError, ValueError):
                continue
            if value <= 0:
                continue
            cleaned_fonts[field] = value
        cleaned_enabled = clean_label_enabled(enabled_data) if enabled_data is not None else None
        settings = load_label_settings()
        entry = {"text": cleaned_text, "fonts": cleaned_fonts}
        if cleaned_enabled is not None:
            entry["enabled"] = cleaned_enabled
        elif isinstance(settings.get(key), dict) and settings.get(key, {}).get("enabled"):
            entry["enabled"] = settings[key]["enabled"]
        settings[key] = entry
        save_label_settings(settings)
        return JsonResponse({"ok": True, "key": key})


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


@role_required(*ALLOWED_ROLES)
@require_POST
def scanner_settings_save(request):
    payload = _parse_json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)
    settings_payload = payload.get("settings") if isinstance(payload, dict) else None
    if not isinstance(settings_payload, dict):
        settings_payload = payload if isinstance(payload, dict) else {}
    updated_by = ""
    if request.user and request.user.is_authenticated:
        updated_by = request.user.get_full_name() or request.user.username
    normalized = save_scanner_settings(settings_payload, updated_by=updated_by or None, when=timezone.now())
    return JsonResponse({"ok": True, "settings": normalized})


@role_required(*ALLOWED_ROLES)
@require_POST
def scanner_settings_apply(request):
    payload = _parse_json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)
    settings_payload = payload.get("settings") if isinstance(payload, dict) else None
    if not isinstance(settings_payload, dict):
        settings_payload = payload if isinstance(payload, dict) else {}
    normalized = normalize_scanner_settings(settings_payload)
    config = normalized.get("default") if isinstance(normalized, dict) else None
    if not isinstance(config, dict):
        return JsonResponse({"ok": False, "error": "invalid_settings"}, status=400)
    target_agent = str(payload.get("agent_id") or "").strip() if isinstance(payload, dict) else ""
    scope = str(payload.get("scope") or "").strip().lower() if isinstance(payload, dict) else ""
    reconnect_only = bool(payload.get("reconnect_only")) if isinstance(payload, dict) else False
    reconnect = bool(payload.get("reconnect")) if isinstance(payload, dict) else False

    if target_agent:
        agent_ids = [target_agent]
    else:
        qs = DeviceAgent.objects.all()
        if scope != "all":
            online_since = timezone.now() - timedelta(seconds=60)
            qs = qs.filter(last_seen__gte=online_since)
        agent_ids = list(qs.values_list("agent_id", flat=True))
    if not agent_ids:
        return JsonResponse({"ok": False, "error": "no_agents"}, status=404)

    commands_created = 0
    for agent_id in agent_ids:
        if reconnect_only:
            command = "scanner.reconnect"
            cmd_payload = {"source": "labels"}
        else:
            command = "scanner.config"
            cmd_payload = {
                "enabled": config.get("enabled"),
                "port": config.get("port"),
                "baud": config.get("baud"),
                "eol": config.get("eol"),
                "idle_ms": config.get("idle_ms"),
                "reconnect": reconnect,
                "source": "labels",
            }
        AgentCommand.objects.create(agent_id=agent_id, command=command, payload=cmd_payload)
        commands_created += 1
    return JsonResponse({"ok": True, "count": commands_created, "agent_ids": agent_ids})


@role_required(*ALLOWED_ROLES)
@require_POST
def scanner_test(request):
    payload = _parse_json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)
    settings_payload = payload.get("settings") if isinstance(payload, dict) else None
    if not isinstance(settings_payload, dict):
        settings_payload = payload if isinstance(payload, dict) else {}
    normalized = normalize_scanner_settings(settings_payload)
    config = normalized.get("default") if isinstance(normalized, dict) else None
    if not isinstance(config, dict):
        return JsonResponse({"ok": False, "error": "invalid_settings"}, status=400)
    target_agent = str(payload.get("agent_id") or "").strip() if isinstance(payload, dict) else ""
    if not target_agent:
        return JsonResponse({"ok": False, "error": "agent_required"}, status=400)
    cmd_payload = {
        "port": config.get("port"),
        "baud": config.get("baud"),
        "eol": config.get("eol"),
        "idle_ms": config.get("idle_ms"),
        "source": "labels",
    }
    command = AgentCommand.objects.create(agent_id=target_agent, command="scanner.test", payload=cmd_payload)
    return JsonResponse({"ok": True, "command_id": command.id})


@role_required(*ALLOWED_ROLES)
@require_GET
def scanner_test_status(request, command_id: int):
    try:
        command = AgentCommand.objects.get(pk=command_id)
    except AgentCommand.DoesNotExist:
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)
    return JsonResponse(
        {
            "ok": True,
            "status": command.status,
            "command": command.command,
            "agent_id": command.agent_id,
            "result": command.result,
            "error": command.error,
            "acked_at": command.acked_at.isoformat() if command.acked_at else "",
        }
    )


@require_GET
def download_fullbox_agent_bundle(request):
    version = AGENT_VERSION.strip()
    zip_name = "fullbox_agent_bundle.zip"
    exe_name = "Fullbox.Agent.Setup.exe"
    if version:
        zip_name = f"fullbox_agent_bundle_v{version}.zip"
        exe_name = f"Fullbox.Agent.Setup.v{version}.exe"
    if request.GET.get("format") == "zip":
        path = (settings.BASE_DIR / "static" / "agents" / "fullbox_agent_bundle.zip").resolve()
        if not path.exists():
            return HttpResponseBadRequest("bundle_not_found")
        response = FileResponse(path.open("rb"), content_type="application/zip")
        response["Content-Disposition"] = f"attachment; filename={zip_name}"
        response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response
    setup_path = (settings.BASE_DIR / "static" / "agents" / "Fullbox.Agent.Setup.exe").resolve()
    if setup_path.exists():
        response = FileResponse(setup_path.open("rb"), content_type="application/octet-stream")
        response["Content-Disposition"] = f"attachment; filename={exe_name}"
        response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response
    path = (settings.BASE_DIR / "static" / "agents" / "fullbox_agent_bundle.zip").resolve()
    if not path.exists():
        return HttpResponseBadRequest("bundle_not_found")
    response = FileResponse(path.open("rb"), content_type="application/zip")
    response["Content-Disposition"] = f"attachment; filename={zip_name}"
    return response
