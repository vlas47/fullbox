"""Head manager UI views and marketplace helper logic."""

import json
import re
from pathlib import Path
from typing import Iterable

import requests
from django.db.models import Q

from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.utils import timezone
from django.views import View
from django.views.generic import CreateView, ListView, TemplateView, UpdateView

from employees.access import RoleRequiredMixin
from sku.models import Agency, Market, MarketCredential

from .forms import CarrierForm, OwnCompanyForm
from .models import Carrier, OwnCompany
from .services import (
    build_marketplace_warehouses_context,
    build_reference_form_context,
    build_reference_list_context,
    save_marketplace_warehouses_response,
    sync_marketplace_warehouses_response,
)


class HeadManagerDashboard(RoleRequiredMixin, TemplateView):
    template_name = 'head_manager/dashboard.html'
    allowed_roles = ("head_manager",)


class HeadManagerReferenceMixin(RoleRequiredMixin):
    allowed_roles = ("head_manager",)
    success_url = "/head-manager/"

    def get_success_url(self):
        return self.success_url


class OwnCompanyListView(HeadManagerReferenceMixin, ListView):
    template_name = "head_manager/reference_list.html"
    context_object_name = "items"
    model = OwnCompany

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(build_reference_list_context(
            title="Наши компании",
            subtitle="Юрлица и ИП FullBox для документов, отгрузок и транспортных накладных.",
            create_url="/head-manager/own-companies/new/",
            edit_base_url="/head-manager/own-companies",
            back_url="/head-manager/",
            type_label="компания",
            directory_mode="own_companies",
        ))
        return ctx


class OwnCompanyCreateView(HeadManagerReferenceMixin, CreateView):
    template_name = "head_manager/reference_form.html"
    form_class = OwnCompanyForm
    model = OwnCompany
    success_url = "/head-manager/own-companies/"

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, "Компания сохранена.")
        return response

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(build_reference_form_context(
            title="Новая компания",
            subtitle="Заполни реквизиты своей компании для документов и ТН.",
            back_url="/head-manager/own-companies/",
            submit_label="Сохранить компанию",
        ))
        return ctx


class OwnCompanyUpdateView(HeadManagerReferenceMixin, UpdateView):
    template_name = "head_manager/reference_form.html"
    form_class = OwnCompanyForm
    model = OwnCompany
    success_url = "/head-manager/own-companies/"

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, "Компания обновлена.")
        return response

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(build_reference_form_context(
            title="Редактирование компании",
            subtitle="Исправь реквизиты своей компании.",
            back_url="/head-manager/own-companies/",
            submit_label="Сохранить изменения",
        ))
        return ctx


class CarrierListView(HeadManagerReferenceMixin, ListView):
    template_name = "head_manager/reference_list.html"
    context_object_name = "items"
    model = Carrier

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(build_reference_list_context(
            title="Перевозчики",
            subtitle="Справочник транспортных компаний и внешних перевозчиков.",
            create_url="/head-manager/carriers/new/",
            edit_base_url="/head-manager/carriers",
            back_url="/head-manager/",
            type_label="перевозчик",
            directory_mode="carriers",
        ))
        return ctx


class CarrierCreateView(HeadManagerReferenceMixin, CreateView):
    template_name = "head_manager/reference_form.html"
    form_class = CarrierForm
    model = Carrier
    success_url = "/head-manager/carriers/"

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, "Перевозчик сохранен.")
        return response

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(build_reference_form_context(
            title="Новый перевозчик",
            subtitle="Заполни карточку транспортной компании или ИП.",
            back_url="/head-manager/carriers/",
            submit_label="Сохранить перевозчика",
        ))
        return ctx


class CarrierUpdateView(HeadManagerReferenceMixin, UpdateView):
    template_name = "head_manager/reference_form.html"
    form_class = CarrierForm
    model = Carrier
    success_url = "/head-manager/carriers/"

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, "Перевозчик обновлен.")
        return response

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(build_reference_form_context(
            title="Редактирование перевозчика",
            subtitle="Исправь реквизиты перевозчика.",
            back_url="/head-manager/carriers/",
            submit_label="Сохранить изменения",
        ))
        return ctx


def _marketplace_warehouses_path() -> Path:
    return settings.BASE_DIR.parent / "marketplace_warehouses.json"


def _normalize_lines(values: Iterable) -> list[str]:
    lines = []
    for value in values:
        text = str(value).strip()
        if text:
            lines.append(text)
    return list(dict.fromkeys(lines))


def _guess_marketplace_warehouse_type(name: str, address: str = "") -> str:
    haystack = f"{name} {address}".lower()
    if any(token in haystack for token in ("транзит", "ппп", "рцпп", "гольёво")):
        return "Транзитный"
    if any(token in haystack for token in ("сц", "сортиров")):
        return "Сортировочный"
    if any(token in haystack for token in ("рфц", "ффц", "фулфил", "фулфилл")):
        return "Фулфилмент"
    return "Обычный"


def _warehouse_row_from_legacy_line(value) -> dict | None:
    text = str(value or "").strip()
    if not text:
        return None
    name = text
    address = ""
    for separator in (" — ", " - ", " – "):
        if separator in text:
            left, right = text.split(separator, 1)
            if left.strip() and right.strip():
                name = left.strip()
                address = right.strip()
                break
    return {
        "type": _guess_marketplace_warehouse_type(name, address),
        "name": name,
        "address": address,
    }


def _normalize_marketplace_warehouse_rows(values) -> list[dict]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    rows: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for value in values:
        if isinstance(value, dict):
            row_type = str(value.get("type") or "").strip() or _guess_marketplace_warehouse_type(
                str(value.get("name") or ""),
                str(value.get("address") or ""),
            )
            name = str(value.get("name") or "").strip()
            address = str(value.get("address") or "").strip()
            if not name and not address:
                continue
            row = {"type": row_type, "name": name, "address": address}
        else:
            row = _warehouse_row_from_legacy_line(value)
            if row is None:
                continue
        dedupe_key = (row["type"].lower(), row["name"].lower(), row["address"].lower())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        rows.append(row)
    return rows


def _warehouse_display_line(row: dict) -> str:
    warehouse_type = str(row.get("type") or "").strip()
    name = str(row.get("name") or "").strip()
    address = str(row.get("address") or "").strip()
    left = " · ".join(part for part in (warehouse_type, name) if part)
    if left and address:
        return f"{left} — {address}"
    return left or address


def _load_marketplace_warehouses() -> dict:
    base = {"wb": [], "ozon": [], "yandex": [], "sber": []}
    path = _marketplace_warehouses_path()
    if not path.exists():
        return base
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return base
    if not isinstance(data, dict):
        return base
    for key in base:
        values = data.get(key) or []
        base[key] = _normalize_marketplace_warehouse_rows(values)
    return base


def _save_marketplace_warehouses(data: dict, user: str = "", meta: dict | None = None) -> None:
    payload = {
        "wb": _normalize_marketplace_warehouse_rows(data.get("wb", [])),
        "ozon": _normalize_marketplace_warehouse_rows(data.get("ozon", [])),
        "yandex": _normalize_marketplace_warehouse_rows(data.get("yandex", [])),
        "sber": _normalize_marketplace_warehouse_rows(data.get("sber", [])),
        "meta": {
            "updated_at": timezone.localtime().isoformat(),
            "updated_by": user,
        },
    }
    if meta:
        payload["meta"].update(meta)
    path = _marketplace_warehouses_path()
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _extract_value(item: dict, keys: Iterable[str]) -> str:
    for key in keys:
        value = item.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _format_address_line(item: dict, name_keys: Iterable[str], address_keys: Iterable[str], city_keys: Iterable[str]):
    name = _extract_value(item, name_keys)
    address = _extract_value(item, address_keys)
    city = _extract_value(item, city_keys)
    address_parts = []
    if city and city.lower() not in address.lower():
        address_parts.append(city)
    if address:
        address_parts.append(address)
    address_text = ", ".join(address_parts).strip()
    return {
        "type": _guess_marketplace_warehouse_type(name, address_text),
        "name": name,
        "address": address_text,
    }


def _parse_items_payload(data) -> list[dict] | None:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return None
    for key in ("result", "warehouses", "data"):
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for nested_key in ("warehouses", "data", "result", "items"):
                nested_value = value.get(nested_key)
                if isinstance(nested_value, list):
                    return nested_value
    return None


def _fetch_wb_warehouses(token: str) -> tuple[list[dict], str | None]:
    endpoints = ["https://marketplace-api.wildberries.ru/api/v3/warehouses"]
    errors = []
    for url in endpoints:
        try:
            response = requests.get(url, headers={"Authorization": token}, timeout=20)
        except requests.RequestException as exc:
            errors.append(f"WB API недоступен ({url}): {exc}")
            continue
        if response.status_code != 200:
            detail = ""
            try:
                payload = response.json()
                detail = (payload.get("detail") or payload.get("title") or "").strip()
            except ValueError:
                detail = ""
            extra = f": {detail}" if detail else ""
            errors.append(f"WB API ошибка {response.status_code} ({url}){extra}")
            continue
        try:
            data = response.json()
        except ValueError:
            errors.append(f"WB API вернул некорректный JSON ({url}).")
            continue
        items = _parse_items_payload(data)
        if items is None:
            errors.append(f"WB API не вернул список складов ({url}).")
            continue
        if not items:
            errors.append("WB: список складов пуст.")
            continue
        rows = []
        for item in items:
            if not isinstance(item, dict):
                continue
            row = _format_address_line(
                item,
                name_keys=("name", "warehouseName", "officeName", "warehouse", "title"),
                address_keys=("address", "warehouseAddress", "officeAddress", "addr", "addressFull"),
                city_keys=("city", "town", "region"),
            )
            if row:
                rows.append(row)
        return _normalize_marketplace_warehouse_rows(rows), None
    if errors:
        return [], errors[0]
    return [], "WB API не отвечает."


def _normalize_ozon_client_id(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if text.isdigit():
        return text
    match = re.fullmatch(r"(\d+)(?:\.0+)?", text)
    return match.group(1) if match else text


def _ozon_headers(client_id: str, api_key: str) -> dict:
    return {
        "Client-Id": client_id,
        "Api-Key": api_key,
        "Content-Type": "application/json",
    }


def _ozon_post(path: str, client_id: str, api_key: str, payload: dict, timeout: int = 30):
    url = f"https://api-seller.ozon.ru{path}"
    try:
        response = requests.post(
            url,
            headers=_ozon_headers(client_id, api_key),
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return None, f"Ozon API недоступен: {exc}"
    if response.status_code != 200:
        snippet = (response.text or "").strip()
        if len(snippet) > 200:
            snippet = f"{snippet[:200]}..."
        detail = f": {snippet}" if snippet else ""
        return None, f"Ozon API ошибка {response.status_code}{detail}"
    try:
        data = response.json()
    except ValueError:
        return None, "Ozon API вернул некорректный JSON."
    return data, None


def _parse_ozon_clusters(data) -> list[dict] | None:
    if not isinstance(data, dict):
        return None
    clusters = data.get("clusters")
    if not isinstance(clusters, list):
        return None
    items = []
    for cluster in clusters:
        if not isinstance(cluster, dict):
            continue
        cluster_name = str(cluster.get("name") or "").strip()
        logistic_clusters = cluster.get("logistic_clusters") or []
        if not isinstance(logistic_clusters, list):
            continue
        for log_cluster in logistic_clusters:
            if not isinstance(log_cluster, dict):
                continue
            warehouses = log_cluster.get("warehouses") or []
            if not isinstance(warehouses, list):
                continue
            for warehouse in warehouses:
                if not isinstance(warehouse, dict):
                    continue
                item = dict(warehouse)
                if cluster_name:
                    item["cluster_name"] = cluster_name
                items.append(item)
    return items


def _fetch_ozon_clusters(client_id: str, api_key: str) -> tuple[list[dict], str | None]:
    rows = []
    errors = []
    for cluster_type in (1, 2):
        data, error = _ozon_post(
            "/v1/cluster/list",
            client_id,
            api_key,
            {"limit": 200, "offset": 0, "cluster_type": cluster_type},
        )
        if error:
            errors.append(error)
            continue
        items = _parse_ozon_clusters(data or {})
        if items is None:
            errors.append("Ozon API не вернул список кластеров.")
            continue
        for item in items:
            name = str(item.get("name") or "").strip()
            cluster_name = str(item.get("cluster_name") or "").strip()
            if cluster_name and name and cluster_name not in name:
                rows.append(
                    {
                        "type": _guess_marketplace_warehouse_type(cluster_name, name),
                        "name": cluster_name,
                        "address": name,
                    }
                )
            elif name:
                rows.append(
                    {
                        "type": _guess_marketplace_warehouse_type(name, ""),
                        "name": name,
                        "address": "",
                    }
                )
    return _normalize_marketplace_warehouse_rows(rows), errors[0] if errors else None


def _fetch_ozon_warehouses(client_id: str, api_key: str) -> tuple[list[dict], str | None]:
    data, error = _ozon_post("/v1/warehouse/list", client_id, api_key, {})
    if error:
        data, error = _ozon_post("/v1/warehouse/list", client_id, api_key, {"limit": 200, "offset": 0})
    if error:
        return [], error
    items = _parse_items_payload(data or {})
    if items is None:
        return [], "Ozon API не вернул список складов."
    if not items:
        cluster_lines, cluster_error = _fetch_ozon_clusters(client_id, api_key)
        if cluster_lines:
            return cluster_lines, None
        return [], cluster_error or "Ozon: список складов пуст."
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        row = _format_address_line(
            item,
            name_keys=("name", "warehouse_name", "title"),
            address_keys=("address", "address_full", "warehouse_address", "address_text"),
            city_keys=("city", "region"),
        )
        if row:
            rows.append(row)
    return _normalize_marketplace_warehouse_rows(rows), None


def _find_agency(client_id: str | None, client_name: str | None) -> Agency | None:
    if client_id:
        return Agency.objects.filter(pk=client_id).first()
    tokens = []
    if client_name:
        tokens.append(client_name)
    tokens.extend(["кейзи", "keizi", "keyzi", "кейз"])
    query = Q()
    for token in tokens:
        token = (token or "").strip()
        if not token:
            continue
        query |= Q(agn_name__icontains=token) | Q(fio_agn__icontains=token)
    if query:
        agency = Agency.objects.filter(query).order_by("id").first()
        if agency:
            return agency
    case_query = Q()
    for token in tokens:
        token = (token or "").strip()
        if not token:
            continue
        variants = {token, token.lower(), token.upper()}
        for variant in variants:
            case_query |= Q(agn_name__contains=variant) | Q(fio_agn__contains=variant)
    if case_query:
        return Agency.objects.filter(case_query).order_by("id").first()
    return None


def _sync_marketplace_warehouses(agency: Agency) -> tuple[dict, list[str]]:
    data = _load_marketplace_warehouses()
    errors = []
    wb_market = Market.objects.filter(name__iexact="WB").first()
    ozon_market = Market.objects.filter(name__iexact="OZON").first()

    if wb_market:
        credential = MarketCredential.objects.filter(agency=agency, market=wb_market).first()
        token = (credential.market_key or "").strip() if credential else ""
        if token:
            wb_list, wb_error = _fetch_wb_warehouses(token)
            if wb_list:
                data["wb"] = wb_list
            else:
                errors.append(wb_error or "WB: список складов пуст.")
        else:
            errors.append("WB: не указан токен.")
    else:
        errors.append("WB: маркетплейс не найден.")

    if ozon_market:
        credential = MarketCredential.objects.filter(agency=agency, market=ozon_market).first()
        token = (credential.market_key or "").strip() if credential else ""
        client_id_value = _normalize_ozon_client_id(credential.client_id) if credential else ""
        if client_id_value and token:
            ozon_list, ozon_error = _fetch_ozon_warehouses(client_id_value, token)
            if ozon_list:
                data["ozon"] = ozon_list
            else:
                errors.append(ozon_error or "Ozon: список складов пуст.")
        else:
            if not client_id_value:
                errors.append("Ozon: не указан Client ID.")
            if not token:
                errors.append("Ozon: не указан API ключ.")
    else:
        errors.append("Ozon: маркетплейс не найден.")

    return data, errors


class MarketplaceWarehousesView(RoleRequiredMixin, TemplateView):
    template_name = "head_manager/marketplace_warehouses.html"
    allowed_roles = ("head_manager",)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            build_marketplace_warehouses_context(
                request=self.request,
                saved=kwargs.get("saved", False),
                error=kwargs.get("error", ""),
            )
        )
        return ctx

    def post(self, request, *args, **kwargs):
        response, error = save_marketplace_warehouses_response(request=request)
        if response is None:
            return self.render_to_response(self.get_context_data(error="Не удалось сохранить список."))
        return response


class MarketplaceWarehousesSyncView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager",)

    def post(self, request, *args, **kwargs):
        return sync_marketplace_warehouses_response(request=request)
