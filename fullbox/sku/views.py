from django.db import models
from django.http import JsonResponse
from django.urls import reverse
from django.views.generic import ListView
from django.shortcuts import get_object_or_404, redirect

from .models import SKU
from audit.models import log_sku_change


class SKUListView(ListView):
    model = SKU
    paginate_by = 20
    template_name = 'sku/sku_list.html'
    context_object_name = 'items'
    view_modes = ('table', 'cards')
    sort_fields = {
        'sku_code': 'sku_code',
        'name': 'name',
        'brand': 'brand',
        'agency': 'agency__agn_name',
        'market': 'market__name',
        'color': 'color',
        'color_ref': 'color_ref__name',
        'size': 'size',
        'name_print': 'name_print',
        'code': 'code',
        'gender': 'gender',
        'season': 'season',
        'additional_name': 'additional_name',
        'composition': 'composition',
        'made_in': 'made_in',
        'cr_product_date': 'cr_product_date',
        'end_product_date': 'end_product_date',
        'sign_akciz': 'sign_akciz',
        'tovar_category': 'tovar_category',
        'use_nds': 'use_nds',
        'vid_tovar': 'vid_tovar',
        'type_tovar': 'type_tovar',
        'stor_unit': 'stor_unit__stor_name',
        'weight_kg': 'weight_kg',
        'volume': 'volume',
        'length_mm': 'length_mm',
        'honest_sign': 'honest_sign',
        'source': 'source',
        'source_reference': 'source_reference',
        'created_at': 'created_at',
        'updated_at': 'updated_at',
    }
    filter_fields = {
        'sku_code': 'sku_code',
        'name': 'name',
        'brand': 'brand',
        'agency': 'agency__agn_name',
        'market': 'market__name',
        'color': 'color',
        'color_ref': 'color_ref__name',
        'size': 'size',
        'name_print': 'name_print',
        'code': 'code',
        'gender': 'gender',
        'season': 'season',
        'additional_name': 'additional_name',
        'composition': 'composition',
        'made_in': 'made_in',
        'tovar_category': 'tovar_category',
        'vid_tovar': 'vid_tovar',
        'type_tovar': 'type_tovar',
        'stor_unit': 'stor_unit__stor_name',
        'source_reference': 'source_reference',
    }
    default_sort = 'sku_code'

    def get_queryset(self):
        qs = super().get_queryset()
        search = self.request.GET.get('q')
        if search:
            qs = qs.filter(
                models.Q(sku_code__icontains=search)
                | models.Q(name__icontains=search)
                | models.Q(barcodes__value__icontains=search)
            ).distinct()
        filter_field = self.request.GET.get('filter_field')
        filter_value = (self.request.GET.get('filter_value') or "").strip()
        if filter_field in self.filter_fields and filter_value:
            lookup = self.filter_fields[filter_field]
            qs = qs.filter(**{f"{lookup}__icontains": filter_value}).distinct()
        sort_key = self.request.GET.get('sort', self.default_sort)
        direction = self.request.GET.get('dir', 'asc')
        sort_field = self.sort_fields.get(sort_key, self.sort_fields[self.default_sort])
        order_by = f"-{sort_field}" if direction == 'desc' else sort_field
        qs = qs.order_by(order_by)
        return qs.select_related(
            'market',
            'agency',
            'color_ref',
            'stor_unit',
        ).prefetch_related('barcodes', 'photos', 'marketplace_bindings')

    def build_sort_url(self, field: str, direction: str) -> str:
        params = self.request.GET.copy()
        if 'view' not in params:
            params['view'] = 'table'
        params['sort'] = field
        params['dir'] = direction
        if self.request.GET.get('filter_field'):
            params['filter_field'] = self.request.GET.get('filter_field')
        if self.request.GET.get('filter_value'):
            params['filter_value'] = self.request.GET.get('filter_value')
        return f"?{params.urlencode()}"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        view = self.request.GET.get('view', 'table')
        if view not in self.view_modes:
            view = 'table'
        ctx['view_mode'] = view
        current_sort = self.request.GET.get('sort', self.default_sort)
        current_dir = 'desc' if self.request.GET.get('dir') == 'desc' else 'asc'
        sort_info = {}
        for field in self.sort_fields:
            is_current = current_sort == field
            next_dir = 'desc' if is_current and current_dir == 'asc' else 'asc'
            sort_info[field] = {
                'url': self.build_sort_url(field, next_dir),
                'active': is_current,
                'dir': current_dir if is_current else '',
                'next_dir': next_dir,
            }
        ctx['current_sort'] = current_sort
        ctx['current_dir'] = current_dir
        ctx['sort_info'] = sort_info
        ctx['filter_field'] = self.request.GET.get('filter_field') or ''
        ctx['filter_value'] = self.request.GET.get('filter_value') or ''
        return ctx


def suggest_sku(request):
    """Возвращает подсказки для поля поиска SKU."""
    query = (request.GET.get("q") or "").strip()
    if len(query) < 2:
        return JsonResponse({"items": []})

    qs = (
        SKU.objects.filter(
            models.Q(sku_code__icontains=query)
            | models.Q(name__icontains=query)
            | models.Q(barcodes__value__icontains=query)
        )
        .distinct()
        .order_by("sku_code")[:10]
    )
    items = [
        {
            "value": sku.sku_code,
            "label": f"{sku.sku_code} — {sku.name}",
        }
        for sku in qs
    ]
    return JsonResponse({"items": items})


def clone_sku(request, pk: int):
    """Создает копию SKU и отправляет в админку для редактирования."""
    orig = get_object_or_404(SKU, pk=pk)

    base_code = f"{orig.sku_code}-copy"
    new_code = base_code
    counter = 1
    while SKU.objects.filter(sku_code=new_code).exists():
        new_code = f"{base_code}{counter}"
        counter += 1

    clone = SKU.objects.create(
        sku_code=new_code,
        name=orig.name,
        brand=orig.brand,
        market=orig.market,
        agency=orig.agency,
        color=orig.color,
        color_ref=orig.color_ref,
        size=orig.size,
        name_print=orig.name_print,
        code=None,
        img=orig.img,
        img_comment=orig.img_comment,
        gender=orig.gender,
        season=orig.season,
        additional_name=orig.additional_name,
        composition=orig.composition,
        made_in=orig.made_in,
        cr_product_date=orig.cr_product_date,
        end_product_date=orig.end_product_date,
        sign_akciz=orig.sign_akciz,
        tovar_category=orig.tovar_category,
        use_nds=orig.use_nds,
        vid_tovar=orig.vid_tovar,
        type_tovar=orig.type_tovar,
        stor_unit=orig.stor_unit,
        weight_kg=orig.weight_kg,
        volume=orig.volume,
        length_mm=orig.length_mm,
        width_mm=orig.width_mm,
        height_mm=orig.height_mm,
        honest_sign=orig.honest_sign,
        description=orig.description,
        source=orig.source,
        source_reference=None,
    )

    # Копируем фото (без уникальных ограничений)
    for photo in orig.photos.all():
        clone.photos.create(url=photo.url, sort_order=photo.sort_order)

    log_sku_change("clone", clone, user=request.user if request.user.is_authenticated else None, description=f"Копия из {orig.sku_code}")

    admin_url = reverse("admin:sku_sku_change", args=[clone.pk])
    return redirect(admin_url)
