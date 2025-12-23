import json

import requests
from django.db.models import Max
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST

from sku.models import Agency, Market, MarketCredential, SKU, SKUBarcode, MarketplaceBinding

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


def _extract_barcodes(card):
    barcodes = []
    sizes = card.get("sizes")
    if not isinstance(sizes, list):
        return barcodes
    for size in sizes:
        if not isinstance(size, dict):
            continue
        skus = size.get("skus") or []
        if isinstance(skus, list):
            for sku in skus:
                if sku:
                    barcodes.append(str(sku))
    return list(dict.fromkeys(barcodes))


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
            nm_id = card.get("nmID") or card.get("nmId") or card.get("nmid")
            name = _extract_first([card.get("title"), card.get("name"), card.get("subjectName")]) or vendor_code
            brand = card.get("brand")
            color = _extract_color(card)
            size = _extract_size(card)
            barcodes = _extract_barcodes(card)
            code_value = barcodes[0] if barcodes else None

            sku, is_created = SKU.objects.update_or_create(
                agency=agency,
                sku_code=vendor_code,
                defaults={
                    "name": name,
                    "brand": brand,
                    "market": wb_market,
                    "color": color,
                    "size": size,
                    "name_print": name,
                    "code": code_value,
                    "source": "marketplace",
                    "source_reference": str(nm_id) if nm_id else None,
                },
            )
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

            if barcodes:
                existing_barcodes = {
                    bc.value: bc for bc in SKUBarcode.objects.filter(sku=sku)
                }
                has_primary = any(bc.is_primary for bc in existing_barcodes.values())
                for idx, value in enumerate(barcodes):
                    if value in existing_barcodes:
                        continue
                    SKUBarcode.objects.create(
                        sku=sku,
                        value=value,
                        is_primary=not has_primary and idx == 0,
                    )
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
