import json
import datetime
import re
from decimal import Decimal, InvalidOperation

import requests
from django.db import IntegrityError
from django.db.models import Max
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST

from sku.models import Agency, Market, MarketCredential, SKU, SKUBarcode, SKUPhoto, MarketplaceBinding


OZON_API_BASE = "https://api-seller.ozon.ru"

from .forms import WBSettingsForm, OzonSettingsForm


def dashboard(request):
    client_id = request.GET.get("client")
    selected_client = None
    credentials = {}
    if client_id:
        selected_client = Agency.objects.filter(pk=client_id).first()
    if selected_client:
        credentials = {
            item.market_id: item
            for item in MarketCredential.objects.filter(agency=selected_client).select_related("market")
        }
    wb_market = Market.objects.filter(name__iexact="WB").first()
    wb_credential = credentials.get(wb_market.id) if wb_market else None
    wb_configured = bool(wb_credential and (wb_credential.market_key or "").strip())
    ozon_market = Market.objects.filter(name__iexact="OZON").first()
    ozon_credential = credentials.get(ozon_market.id) if ozon_market else None
    ozon_configured = bool(
        ozon_credential
        and (ozon_credential.market_key or "").strip()
        and (ozon_credential.client_id or "").strip()
    )
    marketplaces = [
        {
            "name": "Wildberries",
            "status_class": "green" if wb_configured else "red",
            "status_text": "Настроено" if wb_configured else "Не настроено",
            "settings_url": f"/market-sync/wb/?client={selected_client.id}"
            if selected_client
            else None,
        },
        {
            "name": "Ozon",
            "status_class": "green" if ozon_configured else "red",
            "status_text": "Настроено" if ozon_configured else "Не настроено",
            "settings_url": f"/market-sync/ozon/?client={selected_client.id}"
            if selected_client
            else None,
        },
        {
            "name": "Яблоко",
            "status_class": "red",
            "status_text": "Не настроено",
            "settings_url": None,
        },
        {
            "name": "Яндекс Маркет",
            "status_class": "red",
            "status_text": "Не настроено",
            "settings_url": None,
        },
        {
            "name": "Lamoda",
            "status_class": "red",
            "status_text": "Не настроено",
            "settings_url": None,
        },
    ]
    return render(
        request,
        "market_sync/dashboard.html",
        {
            "selected_client": selected_client,
            "marketplaces": marketplaces,
            "wb_configured": wb_configured,
        },
    )


def wb_settings(request):
    client_id = request.GET.get("client") or request.POST.get("client")
    if not client_id:
        return redirect("/market-sync/")
    selected_client = get_object_or_404(Agency, pk=client_id)
    wb_market = Market.objects.filter(name__iexact="WB").first()
    if not wb_market:
        return render(
            request,
            "market_sync/wb_settings.html",
            {
                "selected_client": selected_client,
                "form": WBSettingsForm(),
                "market_missing": True,
            },
        )
    credential = MarketCredential.objects.filter(
        agency=selected_client, market=wb_market
    ).first()
    if request.method == "POST":
        form = WBSettingsForm(request.POST, instance=credential)
        if form.is_valid():
            record = form.save(commit=False)
            record.agency = selected_client
            record.market = wb_market
            if record.pk is None:
                next_id = (MarketCredential.objects.aggregate(max_id=Max("id"))["max_id"] or 0) + 1
                record.id = next_id
            record.save()
            return redirect(f"/market-sync/?client={selected_client.id}")
    else:
        form = WBSettingsForm(instance=credential)
    return render(
        request,
        "market_sync/wb_settings.html",
        {
            "selected_client": selected_client,
            "form": form,
            "market_missing": False,
        },
    )


def ozon_settings(request):
    client_id = request.GET.get("client") or request.POST.get("client")
    if not client_id:
        return redirect("/market-sync/")
    selected_client = get_object_or_404(Agency, pk=client_id)
    ozon_market = Market.objects.filter(name__iexact="OZON").first()
    if not ozon_market:
        return render(
            request,
            "market_sync/ozon_settings.html",
            {
                "selected_client": selected_client,
                "form": OzonSettingsForm(),
                "market_missing": True,
            },
        )
    credential = MarketCredential.objects.filter(
        agency=selected_client, market=ozon_market
    ).first()
    if request.method == "POST":
        form = OzonSettingsForm(request.POST, instance=credential)
        if form.is_valid():
            record = form.save(commit=False)
            record.agency = selected_client
            record.market = ozon_market
            if record.pk is None:
                next_id = (MarketCredential.objects.aggregate(max_id=Max("id"))["max_id"] or 0) + 1
                record.id = next_id
            record.save()
            return redirect(f"/market-sync/?client={selected_client.id}")
    else:
        form = OzonSettingsForm(instance=credential)
    return render(
        request,
        "market_sync/ozon_settings.html",
        {
            "selected_client": selected_client,
            "form": form,
            "market_missing": False,
        },
    )


def _extract_first(values):
    for value in values:
        if value:
            return value
    return None


def _extract_color(card):
    colors = card.get("colors")
    if isinstance(colors, list) and colors:
        first = colors[0]
        if isinstance(first, dict):
            return first.get("name") or first.get("value")
        return str(first)
    return None


def _extract_size(card):
    sizes = card.get("sizes")
    if not isinstance(sizes, list):
        return None
    for size in sizes:
        if not isinstance(size, dict):
            continue
        value = _extract_first([size.get("techSize"), size.get("wbSize"), size.get("size")])
        if value:
            return value
    return None


def _extract_size_barcodes(card):
    size_map = {}
    sizes = card.get("sizes")
    if not isinstance(sizes, list):
        return size_map
    for size in sizes:
        if not isinstance(size, dict):
            continue
        size_value = _normalize_text(
            _extract_first([size.get("techSize"), size.get("wbSize"), size.get("size")])
        )
        skus = size.get("skus") or []
        if isinstance(skus, list):
            for sku in skus:
                if sku:
                    key = size_value or ""
                    size_map.setdefault(key, []).append(str(sku))
    for key, values in size_map.items():
        size_map[key] = list(dict.fromkeys(values))
    return size_map


def _flatten_size_barcodes(size_map):
    barcodes = []
    for values in size_map.values():
        barcodes.extend(values)
    return list(dict.fromkeys(barcodes))


def _extract_barcodes(card):
    return _flatten_size_barcodes(_extract_size_barcodes(card))


def _normalize_text(value):
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        parts = [str(v).strip() for v in value if v]
        text = ", ".join([part for part in parts if part])
        return text or None
    text = str(value).strip()
    return text or None


def _extract_characteristics(card):
    chars = (
        card.get("characteristics")
        or card.get("characteristicsFull")
        or card.get("characteristics_short")
    )
    if not isinstance(chars, list):
        return []
    items = []
    for item in chars:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or item.get("charName") or "").strip()
        value = item.get("value")
        if value is None:
            value = item.get("values")
        if value is None:
            value = item.get("valueName")
        if value is None:
            value = item.get("valueId")
        value_text = _normalize_text(value)
        if name and value_text:
            items.append((name.lower(), value_text))
    return items


def _find_char_value(chars, names):
    for needle in names:
        needle = needle.lower()
        for char_name, value in chars:
            if needle in char_name:
                return value
    return None


def _parse_decimal(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value).strip().replace(",", ".")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)", text)
    if not match:
        return None
    try:
        return Decimal(match.group(1))
    except InvalidOperation:
        return None


def _parse_length_mm(value, default_unit="cm"):
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        number = Decimal(str(value))
        unit = default_unit
    else:
        text = str(value).strip().lower().replace(",", ".")
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([a-zа-я]+)?", text)
        if not match:
            return None
        number = Decimal(match.group(1))
        unit = match.group(2) or default_unit
    if "мм" in unit or "mm" in unit:
        return number
    if "см" in unit or "cm" in unit:
        return number * Decimal("10")
    if unit in ("м", "m") or "метр" in unit:
        return number * Decimal("1000")
    return number * Decimal("10") if default_unit == "cm" else number


def _parse_weight_kg(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value).strip().lower().replace(",", ".")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([a-zа-я]+)?", text)
    if not match:
        return None
    number = Decimal(match.group(1))
    unit = match.group(2) or ""
    if "кг" in unit or "kg" in unit:
        return number
    if ("г" in unit and "кг" not in unit) or unit == "g":
        return number / Decimal("1000")
    return number


def _parse_volume(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value).strip().lower().replace(",", ".")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([a-zа-я0-9³^]+)?", text)
    if not match:
        return None
    number = Decimal(match.group(1))
    unit = (match.group(2) or "").strip()
    if "см3" in unit or "см³" in unit:
        return number / Decimal("1000")
    if "м3" in unit or "м³" in unit:
        return number * Decimal("1000")
    return number


def _parse_date(value):
    if value is None:
        return None
    if isinstance(value, datetime.date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_flag(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return value > 0
    text = str(value).strip().lower()
    if any(token in text for token in ("нет", "без", "no", "false", "0")):
        return False
    if any(token in text for token in ("да", "yes", "true", "1", "есть")):
        return True
    if re.search(r"\d", text):
        return True
    return None


def _extract_photos(card):
    photos = card.get("photos") or []
    urls = []
    if isinstance(photos, list):
        for photo in photos:
            if isinstance(photo, dict):
                url = _extract_first(
                    [
                        photo.get("big"),
                        photo.get("square"),
                        photo.get("tm"),
                        photo.get("c246x328"),
                        photo.get("c516x688"),
                    ]
                )
            else:
                url = str(photo)
            if url:
                urls.append(url)
    return list(dict.fromkeys(urls))


def _trim(value, max_len):
    text = _normalize_text(value)
    if not text:
        return None
    return text[:max_len]


def _ozon_headers(client_id: str, api_key: str) -> dict:
    return {
        "Client-Id": client_id,
        "Api-Key": api_key,
        "Content-Type": "application/json",
    }


def _normalize_ozon_client_id(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    match = re.fullmatch(r"(\d+)(?:\.0+)?", text)
    if match:
        return match.group(1)
    return text


def _ozon_post(path: str, client_id: str, api_key: str, payload: dict, timeout: int = 30):
    url = f"{OZON_API_BASE}{path}"
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


def _ozon_attr_value(attr: dict):
    values = attr.get("values")
    if isinstance(values, list) and values:
        first = values[0]
        if isinstance(first, dict):
            return _normalize_text(first.get("value") or first.get("dictionary_value_id"))
        return _normalize_text(first)
    return _normalize_text(attr.get("value") or attr.get("value_name"))


def _ozon_find_attr(attributes: list, names: list[str]):
    for attr in attributes:
        name = (attr.get("attribute_name") or attr.get("name") or "").strip().lower()
        if not name:
            continue
        for needle in names:
            if needle in name:
                return _ozon_attr_value(attr)
    return None


def _ozon_weight_kg(value):
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        number = Decimal(str(value))
        if number > 50:
            return number / Decimal("1000")
        return number
    return _parse_weight_kg(value)


@require_POST
def wb_sync_run(request):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        payload = {}

    client_id = payload.get("client")
    if not client_id:
        return JsonResponse({"ok": False, "errors": ["Не указан клиент."]}, status=400)

    agency = Agency.objects.filter(pk=client_id).first()
    if not agency:
        return JsonResponse({"ok": False, "errors": ["Клиент не найден."]}, status=404)

    wb_market = Market.objects.filter(name__iexact="WB").first()
    if not wb_market:
        return JsonResponse({"ok": False, "errors": ["Маркетплейс WB не найден."]}, status=400)

    credential = MarketCredential.objects.filter(agency=agency, market=wb_market).first()
    token = (credential.market_key or "").strip() if credential else ""
    if not token:
        return JsonResponse({"ok": False, "errors": ["Не указан токен WB."]}, status=400)

    created = 0
    updated = 0
    processed = 0
    barcode_created = 0
    errors = []
    now = timezone.now()
    cursor = {"limit": 100}
    base_url = "https://content-api.wildberries.ru/content/v2/get/cards/list"

    for _ in range(50):
        try:
            response = requests.post(
                base_url,
                headers={"Authorization": token, "Content-Type": "application/json"},
                json={"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}},
                timeout=30,
            )
        except requests.RequestException as exc:
            errors.append(f"WB API недоступен: {exc}")
            break

        if response.status_code != 200:
            errors.append(f"WB API ошибка: {response.status_code}")
            break

        try:
            data = response.json()
        except ValueError:
            errors.append("WB API вернул некорректный JSON.")
            break

        cards = data.get("cards")
        cursor_data = data.get("cursor")
        if cards is None and isinstance(data.get("data"), dict):
            cards = data["data"].get("cards")
            cursor_data = data["data"].get("cursor")

        if not cards:
            break

        for card in cards:
            vendor_code = (card.get("vendorCode") or card.get("vendor_code") or "").strip()
            if not vendor_code:
                continue
            if len(vendor_code) > 64:
                vendor_code = vendor_code[:64]
            nm_id = card.get("nmID") or card.get("nmId") or card.get("nmid")
            chars = _extract_characteristics(card)
            name_raw = _extract_first([card.get("title"), card.get("name"), card.get("subjectName")]) or vendor_code
            name = _trim(name_raw, 255) or vendor_code
            brand = _trim(card.get("brand"), 255)
            color = _trim(_extract_color(card) or _find_char_value(chars, ["цвет"]), 64)
            size = _trim(_extract_size(card) or _find_char_value(chars, ["размер"]), 64)
            subject = _extract_first([card.get("subjectName"), card.get("subject")]) or _find_char_value(
                chars, ["предмет", "категория"]
            )
            composition = _trim(_find_char_value(chars, ["состав", "материал"]), 255)
            gender = _trim(_find_char_value(chars, ["пол"]), 64)
            season = _trim(_find_char_value(chars, ["сезон"]), 64)
            made_in = _trim(
                _find_char_value(chars, ["страна производства", "страна изготов", "страна"]), 128
            )
            additional_name = _trim(_find_char_value(chars, ["доп", "дополн"]), 255)
            description = _normalize_text(
                _extract_first([card.get("description"), card.get("descriptionRu")])
                or _find_char_value(chars, ["описание"])
            )
            tovar_category = _trim(subject, 128)
            vid_tovar = _trim(_find_char_value(chars, ["вид товара", "вид"]), 128)
            type_tovar = _trim(_find_char_value(chars, ["тип товара", "тип"]), 128)

            dimensions = card.get("dimensions") if isinstance(card.get("dimensions"), dict) else {}
            length_mm = _parse_length_mm(
                _extract_first([dimensions.get("length"), _find_char_value(chars, ["длина упаков", "длина"])])
            )
            width_mm = _parse_length_mm(
                _extract_first([dimensions.get("width"), _find_char_value(chars, ["ширина упаков", "ширина"])])
            )
            height_mm = _parse_length_mm(
                _extract_first([dimensions.get("height"), _find_char_value(chars, ["высота упаков", "высота"])])
            )
            volume = _parse_volume(
                _extract_first([dimensions.get("volume"), _find_char_value(chars, ["объем", "объём"])])
            )
            weight_kg = _parse_weight_kg(
                _extract_first(
                    [
                        card.get("weight"),
                        card.get("weightGross"),
                        card.get("weightNetto"),
                        _find_char_value(chars, ["вес", "масса"]),
                    ]
                )
            )
            cr_product_date = _parse_date(
                _find_char_value(chars, ["дата производства", "дата изготовления"])
            )
            end_product_date = _parse_date(
                _find_char_value(chars, ["срок годности", "годен до"])
            )
            honest_sign = _parse_flag(_find_char_value(chars, ["честный знак", "маркиров"]))
            use_nds = _parse_flag(_find_char_value(chars, ["ндс"]))
            sign_akciz = _parse_flag(_find_char_value(chars, ["акциз"]))

            photo_urls = _extract_photos(card)
            primary_photo = photo_urls[0] if photo_urls else None

            size_barcodes = _extract_size_barcodes(card)
            barcodes = _flatten_size_barcodes(size_barcodes)
            if size is None:
                size_values = [key for key in size_barcodes.keys() if key]
                if len(size_values) == 1:
                    size = _trim(size_values[0], 64)
            code_value = _trim(barcodes[0], 128) if barcodes else None

            update_fields = {
                "name": name,
                "market": wb_market,
                "source": "marketplace",
                "name_print": name,
            }
            if brand is not None:
                update_fields["brand"] = brand
            if color is not None:
                update_fields["color"] = color
            if size is not None:
                update_fields["size"] = size
            if composition is not None:
                update_fields["composition"] = composition
            if gender is not None:
                update_fields["gender"] = gender
            if season is not None:
                update_fields["season"] = season
            if made_in is not None:
                update_fields["made_in"] = made_in
            if additional_name is not None:
                update_fields["additional_name"] = additional_name
            if tovar_category is not None:
                update_fields["tovar_category"] = tovar_category
            if vid_tovar is not None:
                update_fields["vid_tovar"] = vid_tovar
            if type_tovar is not None:
                update_fields["type_tovar"] = type_tovar
            if description is not None:
                update_fields["description"] = description
            if code_value is not None:
                update_fields["code"] = code_value
            if primary_photo is not None:
                update_fields["img"] = primary_photo
            if length_mm is not None:
                update_fields["length_mm"] = length_mm
            if width_mm is not None:
                update_fields["width_mm"] = width_mm
            if height_mm is not None:
                update_fields["height_mm"] = height_mm
            if volume is not None:
                update_fields["volume"] = volume
            if weight_kg is not None:
                update_fields["weight_kg"] = weight_kg
            if cr_product_date is not None:
                update_fields["cr_product_date"] = cr_product_date
            if end_product_date is not None:
                update_fields["end_product_date"] = end_product_date
            if honest_sign is not None:
                update_fields["honest_sign"] = honest_sign
            if use_nds is not None:
                update_fields["use_nds"] = use_nds
            if sign_akciz is not None:
                update_fields["sign_akciz"] = sign_akciz
            if nm_id:
                update_fields["source_reference"] = str(nm_id)

            sku, is_created = SKU.objects.get_or_create(
                agency=agency,
                sku_code=vendor_code,
                defaults=update_fields,
            )
            if not is_created:
                changed = False
                for field, value in update_fields.items():
                    if value is None:
                        continue
                    if getattr(sku, field) != value:
                        setattr(sku, field, value)
                        changed = True
                if changed:
                    sku.save()
            if is_created:
                created += 1
            else:
                updated += 1
            processed += 1

            if nm_id:
                MarketplaceBinding.objects.update_or_create(
                    marketplace="WB",
                    external_id=str(nm_id),
                    defaults={
                        "sku": sku,
                        "sync_mode": "overwrite",
                        "last_synced_at": now,
                    },
                )

            if photo_urls:
                existing_photos = set(
                    SKUPhoto.objects.filter(sku=sku).values_list("url", flat=True)
                )
                for idx, url in enumerate(photo_urls):
                    if url in existing_photos:
                        continue
                    SKUPhoto.objects.create(sku=sku, url=url, sort_order=idx)

            if barcodes:
                existing_barcodes = {
                    bc.value: bc for bc in SKUBarcode.objects.filter(sku=sku)
                }
                has_primary = any(bc.is_primary for bc in existing_barcodes.values())
                primary_set = False
                if size_barcodes:
                    for size_value, values in size_barcodes.items():
                        size_label = _trim(size_value, 64) if size_value else None
                        for value in values:
                            if value in existing_barcodes:
                                existing = existing_barcodes[value]
                                if size_label and existing.size != size_label:
                                    existing.size = size_label
                                    existing.save(update_fields=["size"])
                                continue
                            if SKUBarcode.objects.filter(value=value).exists():
                                continue
                            try:
                                SKUBarcode.objects.create(
                                    sku=sku,
                                    value=value,
                                    size=size_label,
                                    is_primary=not has_primary and not primary_set,
                                )
                            except IntegrityError:
                                continue
                            barcode_created += 1
                            if not has_primary and not primary_set:
                                primary_set = True
                                has_primary = True
                else:
                    for idx, value in enumerate(barcodes):
                        if value in existing_barcodes:
                            continue
                        if SKUBarcode.objects.filter(value=value).exists():
                            continue
                        try:
                            SKUBarcode.objects.create(
                                sku=sku,
                                value=value,
                                is_primary=not has_primary and idx == 0,
                            )
                        except IntegrityError:
                            continue
                        barcode_created += 1
                        if idx == 0:
                            has_primary = True

        if cursor_data and cursor_data.get("updatedAt") and cursor_data.get("nmID") is not None:
            cursor = {
                "limit": cursor.get("limit", 100),
                "updatedAt": cursor_data["updatedAt"],
                "nmID": cursor_data["nmID"],
            }
        else:
            break

    return JsonResponse(
        {
            "ok": not errors,
            "processed": processed,
            "created": created,
            "updated": updated,
            "barcodes_created": barcode_created,
            "errors": errors,
        }
    )


@require_POST
def ozon_sync_run(request):
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        payload = {}

    client_id = payload.get("client")
    if not client_id:
        return JsonResponse(
            {
                "ok": False,
                "errors": [
                    "Не указан клиент.",
                    "Передайте ID клиента в JSON-теле запроса: {\"client\": <id>}.",
                ],
            },
            status=400,
        )

    agency = Agency.objects.filter(pk=client_id).first()
    if not agency:
        return JsonResponse(
            {
                "ok": False,
                "errors": [f"Клиент с ID {client_id} не найден в базе."],
            },
            status=404,
        )

    ozon_market = Market.objects.filter(name__iexact="OZON").first()
    if not ozon_market:
        return JsonResponse(
            {
                "ok": False,
                "errors": ["Маркетплейс Ozon не найден в справочнике (Market.name=OZON)."],
            },
            status=400,
        )

    credential = MarketCredential.objects.filter(agency=agency, market=ozon_market).first()
    token = (credential.market_key or "").strip() if credential else ""
    client_id_value = _normalize_ozon_client_id(credential.client_id) if credential else ""
    if not token or not client_id_value:
        missing = []
        if not client_id_value:
            missing.append("Не указан Client ID Ozon для клиента.")
        if not token:
            missing.append("Не указан API ключ Ozon для клиента.")
        return JsonResponse(
            {"ok": False, "errors": missing or ["Не указан Client ID или API ключ Ozon."]},
            status=400,
        )
    if not client_id_value.isdigit() or int(client_id_value) <= 0:
        return JsonResponse(
            {
                "ok": False,
                "errors": [
                    "Client ID Ozon должен быть положительным числом.",
                    "Проверьте значение Client ID в настройках Ozon.",
                ],
            },
            status=400,
        )

    created = 0
    updated = 0
    processed = 0
    barcode_created = 0
    errors = []
    now = timezone.now()

    list_items = []
    last_id = ""
    for _ in range(50):
        list_payload = {
            "filter": {"visibility": "ALL"},
            "last_id": last_id,
            "limit": 1000,
        }
        data, error = _ozon_post(
            "/v3/product/list", client_id_value, token, list_payload
        )
        if error:
            errors.append(error)
            break
        result = (data or {}).get("result") or {}
        items = result.get("items") or []
        if not items:
            break
        list_items.extend(items)
        next_last_id = result.get("last_id") or ""
        if not next_last_id or next_last_id == last_id:
            break
        last_id = next_last_id

    if not errors and not list_items:
        return JsonResponse(
            {
                "ok": True,
                "processed": 0,
                "created": 0,
                "updated": 0,
                "barcodes_created": 0,
                "errors": [],
            }
        )

    product_ids = []
    offer_by_product = {}
    for item in list_items:
        product_id = item.get("product_id")
        offer_id = item.get("offer_id") or item.get("offerId")
        if product_id is None:
            continue
        product_ids.append(product_id)
        if offer_id:
            offer_by_product[product_id] = str(offer_id)

    def _chunked(values, size):
        for idx in range(0, len(values), size):
            yield values[idx : idx + size]

    attr_by_product = {}
    info_items = []
    if not errors:
        for batch in _chunked(product_ids, 100):
            info_payload = {"product_id": batch}
            data, error = _ozon_post(
                "/v3/product/info/list", client_id_value, token, info_payload
            )
            if error:
                errors.append(error)
                break
            info_items.extend((data or {}).get("items") or [])

            attr_payload = {
                "filter": {"product_id": batch},
                "limit": 1000,
            }
            attr_data, attr_error = _ozon_post(
                "/v4/product/info/attributes", client_id_value, token, attr_payload
            )
            if attr_error:
                errors.append(attr_error)
                continue
            attr_result = (attr_data or {}).get("result")
            if isinstance(attr_result, dict):
                entries = attr_result.get("items") or []
            elif isinstance(attr_result, list):
                entries = attr_result
            else:
                entries = []
            for entry in entries:
                product_id = entry.get("product_id")
                attributes = entry.get("attributes") or []
                if product_id is not None:
                    attr_by_product[product_id] = attributes

    for item in info_items:
        product_id = item.get("product_id") or item.get("id")
        offer_id = item.get("offer_id") or offer_by_product.get(product_id)
        if not offer_id:
            continue
        offer_id = str(offer_id).strip()
        if not offer_id:
            continue
        if len(offer_id) > 64:
            offer_id = offer_id[:64]

        attributes = item.get("attributes") or attr_by_product.get(product_id, [])
        name = _trim(item.get("name") or item.get("title"), 255) or offer_id
        brand = _trim(
            item.get("brand") or _ozon_find_attr(attributes, ["бренд"]), 255
        )
        color = _trim(_ozon_find_attr(attributes, ["цвет"]), 64)
        size = _trim(_ozon_find_attr(attributes, ["размер"]), 64)
        composition = _trim(
            _ozon_find_attr(attributes, ["состав", "материал"]), 255
        )
        gender = _trim(_ozon_find_attr(attributes, ["пол"]), 64)
        season = _trim(_ozon_find_attr(attributes, ["сезон"]), 64)
        made_in = _trim(_ozon_find_attr(attributes, ["страна"]), 128)
        tovar_category = _trim(
            _ozon_find_attr(
                attributes, ["категория", "тип товара", "предмет", "назначение"]
            ),
            128,
        )
        description = _normalize_text(item.get("description"))

        weight_kg = _ozon_weight_kg(
            _extract_first(
                [
                    item.get("weight"),
                    item.get("weight_g"),
                    item.get("weight_kg"),
                ]
            )
        )
        dimensions = item.get("dimensions") if isinstance(item.get("dimensions"), dict) else {}
        length_mm = _parse_length_mm(
            _extract_first([item.get("depth"), item.get("length"), dimensions.get("length")]),
            default_unit="mm",
        )
        width_mm = _parse_length_mm(
            _extract_first([item.get("width"), dimensions.get("width")]),
            default_unit="mm",
        )
        height_mm = _parse_length_mm(
            _extract_first([item.get("height"), dimensions.get("height")]),
            default_unit="mm",
        )
        volume = _parse_volume(item.get("volume") or dimensions.get("volume"))

        images = item.get("images") or []
        if isinstance(images, str):
            images = [images]
        primary_image = _extract_first(
            [item.get("primary_image"), images[0] if images else None]
        )

        barcodes = item.get("barcodes") or item.get("barcode") or []
        if isinstance(barcodes, str):
            barcodes = [barcodes]
        barcodes = [str(value) for value in barcodes if value]
        code_value = _trim(barcodes[0], 128) if barcodes else None

        update_fields = {
            "name": name,
            "market": ozon_market,
            "source": "marketplace",
            "name_print": name,
        }
        if brand is not None:
            update_fields["brand"] = brand
        if color is not None:
            update_fields["color"] = color
        if size is not None:
            update_fields["size"] = size
        if composition is not None:
            update_fields["composition"] = composition
        if gender is not None:
            update_fields["gender"] = gender
        if season is not None:
            update_fields["season"] = season
        if made_in is not None:
            update_fields["made_in"] = made_in
        if tovar_category is not None:
            update_fields["tovar_category"] = tovar_category
        if description is not None:
            update_fields["description"] = description
        if code_value is not None:
            update_fields["code"] = code_value
        if primary_image is not None:
            update_fields["img"] = primary_image
        if length_mm is not None:
            update_fields["length_mm"] = length_mm
        if width_mm is not None:
            update_fields["width_mm"] = width_mm
        if height_mm is not None:
            update_fields["height_mm"] = height_mm
        if volume is not None:
            update_fields["volume"] = volume
        if weight_kg is not None:
            update_fields["weight_kg"] = weight_kg
        if product_id is not None:
            update_fields["source_reference"] = str(product_id)

        sku, is_created = SKU.objects.get_or_create(
            agency=agency,
            sku_code=offer_id,
            defaults=update_fields,
        )
        if not is_created:
            changed = False
            for field, value in update_fields.items():
                if value is None:
                    continue
                if getattr(sku, field) != value:
                    setattr(sku, field, value)
                    changed = True
            if changed:
                sku.save()
        if is_created:
            created += 1
        else:
            updated += 1
        processed += 1

        if product_id is not None:
            MarketplaceBinding.objects.update_or_create(
                marketplace="OZON",
                external_id=str(product_id),
                defaults={
                    "sku": sku,
                    "sync_mode": "overwrite",
                    "last_synced_at": now,
                },
            )

        if images:
            existing_photos = set(
                SKUPhoto.objects.filter(sku=sku).values_list("url", flat=True)
            )
            for idx, url in enumerate(images):
                if url in existing_photos:
                    continue
                SKUPhoto.objects.create(sku=sku, url=url, sort_order=idx)

        if barcodes:
            existing_barcodes = {
                bc.value: bc for bc in SKUBarcode.objects.filter(sku=sku)
            }
            has_primary = any(bc.is_primary for bc in existing_barcodes.values())
            for idx, value in enumerate(barcodes):
                if value in existing_barcodes:
                    existing = existing_barcodes[value]
                    if size and existing.size != size:
                        existing.size = size
                        existing.save(update_fields=["size"])
                    continue
                if SKUBarcode.objects.filter(value=value).exists():
                    continue
                try:
                    SKUBarcode.objects.create(
                        sku=sku,
                        value=value,
                        size=size,
                        is_primary=not has_primary and idx == 0,
                    )
                except IntegrityError:
                    continue
                barcode_created += 1
                if idx == 0:
                    has_primary = True

    return JsonResponse(
        {
            "ok": not errors,
            "processed": processed,
            "created": created,
            "updated": updated,
            "barcodes_created": barcode_created,
            "errors": errors,
        }
    )
