from __future__ import annotations

import json

import requests
from django.db import IntegrityError
from django.http import JsonResponse
from django.utils import timezone

from sku.models import Agency, Market, MarketCredential, MarketplaceBinding, SKU, SKUBarcode, SKUPhoto

from .models import MarketSyncReport


def _views():
    from . import views as market_sync_views

    return market_sync_views


def _report_link(report: MarketSyncReport | None) -> str:
    if not report:
        return ""
    return f"/market-sync/report/{report.id}/"


def _parse_payload(body) -> dict:
    raw_body = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body or "")
    try:
        payload = json.loads(raw_body or "{}")
    except json.JSONDecodeError:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _has_meaningful_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _apply_marketplace_updates(*, sku: SKU, update_fields: dict, overwrite: bool) -> bool:
    changed = False
    for field, value in update_fields.items():
        if value is None:
            continue
        current = getattr(sku, field)
        if not overwrite and _has_meaningful_value(current):
            continue
        if current != value:
            setattr(sku, field, value)
            changed = True
    return changed


def run_wb_sync_request(*, body) -> JsonResponse:
    views = _views()
    payload = _parse_payload(body)

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

    started_at = timezone.now()
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
            chars = views._extract_characteristics(card)
            name_raw = views._extract_first([card.get("title"), card.get("name"), card.get("subjectName")]) or vendor_code
            name = views._trim(name_raw, 255) or vendor_code
            brand = views._trim(card.get("brand"), 255)
            color = views._trim(views._extract_color(card) or views._find_char_value(chars, ["цвет"]), 64)
            size = views._trim(views._extract_size(card) or views._find_char_value(chars, ["размер"]), 64)
            subject = views._extract_first([card.get("subjectName"), card.get("subject")]) or views._find_char_value(
                chars, ["предмет", "категория"]
            )
            composition = views._trim(views._find_char_value(chars, ["состав", "материал"]), 255)
            gender = views._trim(views._find_char_value(chars, ["пол"]), 64)
            season = views._trim(views._find_char_value(chars, ["сезон"]), 64)
            made_in = views._trim(
                views._find_char_value(chars, ["страна производства", "страна изготов", "страна"]), 128
            )
            additional_name = views._trim(views._find_char_value(chars, ["доп", "дополн"]), 255)
            description = views._normalize_text(
                views._extract_first([card.get("description"), card.get("descriptionRu")])
                or views._find_char_value(chars, ["описание"])
            )
            tovar_category = views._trim(subject, 128)
            vid_tovar = views._trim(views._find_char_value(chars, ["вид товара", "вид"]), 128)
            type_tovar = views._trim(views._find_char_value(chars, ["тип товара", "тип"]), 128)

            dimensions = card.get("dimensions") if isinstance(card.get("dimensions"), dict) else {}
            length_mm = views._parse_length_mm(
                views._extract_first([dimensions.get("length"), views._find_char_value(chars, ["длина упаков", "длина"])])
            )
            width_mm = views._parse_length_mm(
                views._extract_first([dimensions.get("width"), views._find_char_value(chars, ["ширина упаков", "ширина"])])
            )
            height_mm = views._parse_length_mm(
                views._extract_first([dimensions.get("height"), views._find_char_value(chars, ["высота упаков", "высота"])])
            )
            volume = views._parse_volume(
                views._extract_first([dimensions.get("volume"), views._find_char_value(chars, ["объем", "объём"])])
            )
            weight_gross_kg = views._parse_weight_kg(
                views._extract_first(
                    [
                        dimensions.get("weightBrutto"),
                        dimensions.get("weight_brutto"),
                        card.get("weightGross"),
                        views._find_char_value(chars, ["вес брутто", "масса брутто"]),
                    ]
                )
            )
            weight_net_kg = views._parse_weight_kg(
                views._extract_first(
                    [
                        card.get("weightNetto"),
                        views._find_char_value(chars, ["вес нетто", "масса нетто"]),
                    ]
                )
            )
            weight_generic_kg = views._parse_weight_kg(
                views._extract_first(
                    [
                        dimensions.get("weight"),
                        card.get("weight"),
                        views._find_char_value(chars, ["вес", "масса"]),
                    ]
                )
            )
            weight_gross_effective_kg = weight_gross_kg if weight_gross_kg is not None else weight_generic_kg
            weight_kg = (
                weight_gross_effective_kg
                if weight_gross_effective_kg is not None
                else weight_net_kg
            )
            cr_product_date = views._parse_date(views._find_char_value(chars, ["дата производства", "дата изготовления"]))
            end_product_date = views._parse_date(views._find_char_value(chars, ["срок годности", "годен до"]))
            honest_sign = views._parse_flag(views._find_char_value(chars, ["честный знак", "маркиров"]))
            use_nds = views._parse_flag(views._find_char_value(chars, ["ндс"]))
            sign_akciz = views._parse_flag(views._find_char_value(chars, ["акциз"]))

            photo_urls = views._extract_photos(card)
            primary_photo = photo_urls[0] if photo_urls else None

            size_barcodes = views._extract_size_barcodes(card)
            barcodes = views._flatten_size_barcodes(size_barcodes)
            if size is None:
                size_values = [key for key in size_barcodes.keys() if key]
                if len(size_values) == 1:
                    size = views._trim(size_values[0], 64)
            code_value = views._trim(barcodes[0], 128) if barcodes else None

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
            if weight_net_kg is not None:
                update_fields["weight_net_kg"] = weight_net_kg
            if weight_gross_effective_kg is not None:
                update_fields["weight_gross_kg"] = weight_gross_effective_kg
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

            binding = (
                MarketplaceBinding.objects.filter(marketplace="WB", external_id=str(nm_id)).first()
                if nm_id
                else None
            )
            overwrite = bool(binding and binding.sync_mode == "overwrite")

            sku, is_created = SKU.objects.get_or_create(
                agency=agency,
                sku_code=vendor_code,
                defaults=update_fields,
            )
            if not is_created:
                changed = _apply_marketplace_updates(
                    sku=sku,
                    update_fields=update_fields,
                    overwrite=overwrite,
                )
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
                        "last_synced_at": now,
                    },
                )

            if photo_urls:
                existing_photos = set(SKUPhoto.objects.filter(sku=sku).values_list("url", flat=True))
                for idx, url in enumerate(photo_urls):
                    if url in existing_photos:
                        continue
                    SKUPhoto.objects.create(sku=sku, url=url, sort_order=idx)

            if barcodes:
                existing_barcodes = {bc.value: bc for bc in SKUBarcode.objects.filter(sku=sku)}
                has_primary = any(bc.is_primary for bc in existing_barcodes.values())
                primary_set = False
                if size_barcodes:
                    for size_value, values in size_barcodes.items():
                        size_label = views._trim(size_value, 64) if size_value else None
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

    finished_at = timezone.now()
    report = MarketSyncReport.objects.create(
        agency=agency,
        marketplace="WB",
        status="ok" if not errors else "error",
        started_at=started_at,
        finished_at=finished_at,
        duration_sec=(finished_at - started_at).total_seconds(),
        processed=processed,
        created=created,
        updated=updated,
        barcodes_created=barcode_created,
        errors=errors,
    )
    return JsonResponse(
        {
            "ok": not errors,
            "processed": processed,
            "created": created,
            "updated": updated,
            "barcodes_created": barcode_created,
            "errors": errors,
            "report_id": report.id,
            "report_url": _report_link(report),
            "duration_sec": report.duration_sec,
        }
    )


def run_ozon_sync_request(*, body) -> JsonResponse:
    views = _views()
    payload = _parse_payload(body)

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
    client_id_value = views._normalize_ozon_client_id(credential.client_id) if credential else ""
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

    started_at = timezone.now()
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
        data, error = views._ozon_post("/v3/product/list", client_id_value, token, list_payload)
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
        finished_at = timezone.now()
        report = MarketSyncReport.objects.create(
            agency=agency,
            marketplace="OZON",
            status="ok",
            started_at=started_at,
            finished_at=finished_at,
            duration_sec=(finished_at - started_at).total_seconds(),
            processed=0,
            created=0,
            updated=0,
            barcodes_created=0,
            errors=[],
        )
        return JsonResponse(
            {
                "ok": True,
                "processed": 0,
                "created": 0,
                "updated": 0,
                "barcodes_created": 0,
                "errors": [],
                "report_id": report.id,
                "report_url": _report_link(report),
                "duration_sec": report.duration_sec,
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
            data, error = views._ozon_post("/v3/product/info/list", client_id_value, token, info_payload)
            if error:
                errors.append(error)
                break
            info_items.extend((data or {}).get("items") or [])

            attr_payload = {
                "filter": {"product_id": batch},
                "limit": 1000,
            }
            attr_data, attr_error = views._ozon_post(
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
        name = views._trim(item.get("name") or item.get("title"), 255) or offer_id
        brand = views._trim(item.get("brand") or views._ozon_find_attr(attributes, ["бренд"]), 255)
        color = views._trim(views._ozon_find_attr(attributes, ["цвет"]), 64)
        size = views._trim(views._ozon_find_attr(attributes, ["размер"]), 64)
        composition = views._trim(views._ozon_find_attr(attributes, ["состав", "материал"]), 255)
        gender = views._trim(views._ozon_find_attr(attributes, ["пол"]), 64)
        season = views._trim(views._ozon_find_attr(attributes, ["сезон"]), 64)
        made_in = views._trim(views._ozon_find_attr(attributes, ["страна"]), 128)
        tovar_category = views._trim(
            views._ozon_find_attr(attributes, ["категория", "тип товара", "предмет", "назначение"]),
            128,
        )
        description = views._normalize_text(item.get("description"))

        weight_kg = views._ozon_weight_kg(
            views._extract_first([item.get("weight"), item.get("weight_g"), item.get("weight_kg")])
        )
        dimensions = item.get("dimensions") if isinstance(item.get("dimensions"), dict) else {}
        length_mm = views._parse_length_mm(
            views._extract_first([item.get("depth"), item.get("length"), dimensions.get("length")]),
            default_unit="mm",
        )
        width_mm = views._parse_length_mm(
            views._extract_first([item.get("width"), dimensions.get("width")]),
            default_unit="mm",
        )
        height_mm = views._parse_length_mm(
            views._extract_first([item.get("height"), dimensions.get("height")]),
            default_unit="mm",
        )
        volume = views._parse_volume(item.get("volume") or dimensions.get("volume"))

        images = item.get("images") or []
        if isinstance(images, str):
            images = [images]
        primary_image = views._extract_first([item.get("primary_image"), images[0] if images else None])

        barcodes = item.get("barcodes") or item.get("barcode") or []
        if isinstance(barcodes, str):
            barcodes = [barcodes]
        barcodes = [str(value) for value in barcodes if value]
        code_value = views._trim(barcodes[0], 128) if barcodes else None

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
            update_fields["weight_gross_kg"] = weight_kg
        if product_id is not None:
            update_fields["source_reference"] = str(product_id)

        binding = (
            MarketplaceBinding.objects.filter(marketplace="OZON", external_id=str(product_id)).first()
            if product_id is not None
            else None
        )
        overwrite = bool(binding and binding.sync_mode == "overwrite")

        sku, is_created = SKU.objects.get_or_create(
            agency=agency,
            sku_code=offer_id,
            defaults=update_fields,
        )
        if not is_created:
            changed = _apply_marketplace_updates(
                sku=sku,
                update_fields=update_fields,
                overwrite=overwrite,
            )
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
                    "last_synced_at": now,
                },
            )

        if images:
            existing_photos = set(SKUPhoto.objects.filter(sku=sku).values_list("url", flat=True))
            for idx, url in enumerate(images):
                if url in existing_photos:
                    continue
                SKUPhoto.objects.create(sku=sku, url=url, sort_order=idx)

        if barcodes:
            existing_barcodes = {bc.value: bc for bc in SKUBarcode.objects.filter(sku=sku)}
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

    finished_at = timezone.now()
    report = MarketSyncReport.objects.create(
        agency=agency,
        marketplace="OZON",
        status="ok" if not errors else "error",
        started_at=started_at,
        finished_at=finished_at,
        duration_sec=(finished_at - started_at).total_seconds(),
        processed=processed,
        created=created,
        updated=updated,
        barcodes_created=barcode_created,
        errors=errors,
    )
    return JsonResponse(
        {
            "ok": not errors,
            "processed": processed,
            "created": created,
            "updated": updated,
            "barcodes_created": barcode_created,
            "errors": errors,
            "report_id": report.id,
            "report_url": _report_link(report),
            "duration_sec": report.duration_sec,
        }
    )
