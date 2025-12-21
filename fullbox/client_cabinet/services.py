import os
from typing import Any

import requests


DADATA_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"


def fetch_party_by_inn(inn: str) -> dict[str, Any]:
    """
    Получает карточку организации из DaData по ИНН.
    Возвращает словарь с ключами под Agency.
    """
    token = os.environ.get("DADATA_TOKEN") or os.environ.get("DADATA_API_KEY") or "b71eaa6658a6b2d3ae74a1e143e0d5719b9444f3"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Token {token}",
    }
    payload = {"query": inn}
    resp = requests.post(DADATA_URL, json=payload, headers=headers, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    suggestions = data.get("suggestions") or []
    if not suggestions:
        return {}
    item = suggestions[0].get("data") or {}

    address = (item.get("address") or {}).get("unrestricted_value")
    mgmt = item.get("management") or {}

    return {
        "agn_name": (item.get("name") or {}).get("full_with_opf") or item.get("value"),
        "inn": item.get("inn"),
        "kpp": item.get("kpp"),
        "ogrn": item.get("ogrn"),
        "adres": address,
        "fakt_adres": address,
        "fio_agn": mgmt.get("name"),
        "pref": (item.get("opf") or {}).get("short"),
        "sign_oferta": True,
    }
