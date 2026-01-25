import json
import re
from pathlib import Path
from typing import Iterable

import requests
from django.db.models import Q

from django.conf import settings
from django.shortcuts import redirect
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from employees.access import RoleRequiredMixin
from sku.models import Agency, Market, MarketCredential


class HeadManagerDashboard(RoleRequiredMixin, TemplateView):
    template_name = 'head_manager/dashboard.html'
    allowed_roles = ("head_manager",)


def _marketplace_warehouses_path() -> Path:
    return settings.BASE_DIR.parent / "marketplace_warehouses.json"


def _normalize_lines(values: Iterable) -> list[str]:
    lines = []
    for value in values:
        text = str(value).strip()
        if text:
            lines.append(text)
    return list(dict.fromkeys(lines))


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
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            values = []
        base[key] = _normalize_lines(values)
    return base


def _save_marketplace_warehouses(data: dict, user: str = "", meta: dict | None = None) -> None:
    payload = {
        "wb": data.get("wb", []),
        "ozon": data.get("ozon", []),
        "yandex": data.get("yandex", []),
        "sber": data.get("sber", []),
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
    if name and address_text:
        return f"{name} — {address_text}"
    if address_text:
        return address_text
    return name


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


def _fetch_wb_warehouses(token: str) -> tuple[list[str], str | None]:
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
        lines = []
        for item in items:
            if not isinstance(item, dict):
                continue
            line = _format_address_line(
                item,
                name_keys=("name", "warehouseName", "officeName", "warehouse", "title"),
                address_keys=("address", "warehouseAddress", "officeAddress", "addr", "addressFull"),
                city_keys=("city", "town", "region"),
            )
            if line:
                lines.append(line)
        return _normalize_lines(lines), None
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


def _fetch_ozon_clusters(client_id: str, api_key: str) -> tuple[list[str], str | None]:
    lines = []
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
                lines.append(f"{cluster_name} - {name}")
            elif name:
                lines.append(name)
    return _normalize_lines(lines), errors[0] if errors else None


def _fetch_ozon_warehouses(client_id: str, api_key: str) -> tuple[list[str], str | None]:
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
    lines = []
    for item in items:
        if not isinstance(item, dict):
            continue
        line = _format_address_line(
            item,
            name_keys=("name", "warehouse_name", "title"),
            address_keys=("address", "address_full", "warehouse_address", "address_text"),
            city_keys=("city", "region"),
        )
        if line:
            lines.append(line)
    return _normalize_lines(lines), None


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
        data = _load_marketplace_warehouses()
        ctx["warehouses"] = {
            "wb": "\n".join(data.get("wb", [])),
            "ozon": "\n".join(data.get("ozon", [])),
            "yandex": "\n".join(data.get("yandex", [])),
            "sber": "\n".join(data.get("sber", [])),
        }
        ctx["saved"] = kwargs.get("saved", False)
        ctx["error"] = kwargs.get("error", "")
        ctx["sync_info"] = self.request.session.pop("marketplace_sync_info", "")
        ctx["sync_errors"] = self.request.session.pop("marketplace_sync_errors", [])
        ctx["sync_agency"] = self.request.session.pop("marketplace_sync_agency", "")
        ctx["sync_default_name"] = "Кейзи"
        return ctx

    def post(self, request, *args, **kwargs):
        def normalize(key: str) -> list[str]:
            raw = request.POST.get(key, "") or ""
            return [line.strip() for line in raw.splitlines() if line.strip()]

        data = {
            "wb": normalize("wb"),
            "ozon": normalize("ozon"),
            "yandex": normalize("yandex"),
            "sber": normalize("sber"),
        }
        try:
            user = request.user.username if request.user.is_authenticated else ""
            _save_marketplace_warehouses(data, user=user)
        except OSError:
            return self.render_to_response(self.get_context_data(error="Не удалось сохранить список."))
        return redirect("/head-manager/marketplace-warehouses/?saved=1")


class MarketplaceWarehousesSyncView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager",)

    def post(self, request, *args, **kwargs):
        client_id = (request.POST.get("client_id") or "").strip()
        client_name = (request.POST.get("client_name") or "").strip()
        if not client_id and not client_name:
            client_name = "Кейзи"

        agency = _find_agency(client_id, client_name)
        if not agency:
            request.session["marketplace_sync_errors"] = [
                "Клиент не найден. Укажите ID клиента или название.",
            ]
            return redirect("/head-manager/marketplace-warehouses/")

        data, errors = _sync_marketplace_warehouses(agency)
        errors = [err for err in errors if err]

        try:
            user = request.user.username if request.user.is_authenticated else ""
            meta = {
                "source": "sync",
                "agency_id": agency.id,
                "agency_name": agency.agn_name or "",
            }
            _save_marketplace_warehouses(data, user=user, meta=meta)
            wb_count = len(data.get("wb", []))
            ozon_count = len(data.get("ozon", []))
            request.session["marketplace_sync_info"] = (
                f"Синхронизация выполнена: WB {wb_count}, Ozon {ozon_count}."
            )
            request.session["marketplace_sync_agency"] = agency.agn_name or str(agency.id)
        except OSError:
            errors.append("Не удалось сохранить список.")

        if errors:
            request.session["marketplace_sync_errors"] = errors

        return redirect("/head-manager/marketplace-warehouses/")
