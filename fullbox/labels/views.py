import json
from datetime import datetime, timedelta

from django.http import JsonResponse
from django.utils import timezone
from django.views.generic import TemplateView

from employees.access import RoleRequiredMixin
from processing_app.models import ProcessingPrintJob
from .utils import (
    LABEL_FIELDS,
    LABEL_SIZE_KEYS,
    LABEL_SIZES,
    load_available_printers_data,
    clean_label_enabled,
    load_label_settings,
    load_print_agent_status,
    save_label_settings,
)


class LabelSettingsView(RoleRequiredMixin, TemplateView):
    template_name = "labels/settings.html"
    allowed_roles = ("head_manager", "director", "admin")

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
        last_job = ProcessingPrintJob.objects.order_by("-updated_at").first()
        last_error = ""
        last_job_time = ""
        if last_job:
            last_job_time = timezone.localtime(last_job.updated_at).strftime("%d.%m.%Y %H:%M:%S")
            if last_job.status == ProcessingPrintJob.STATUS_FAILED:
                last_error = last_job.error or "ошибка без описания"

        if pending_count:
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
