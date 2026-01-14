import os, sys, re
sys.path.insert(0, '/opt/fullbox/fullbox')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'fullbox.settings')
import django
django.setup()
from django.db.models import Q
from audit.models import OrderAuditEntry
from sku.models import Agency

agency_id = 2638
agency = Agency.objects.filter(pk=agency_id).first()
print('agency', agency.id if agency else None)

if not agency:
    raise SystemExit

def _parse_int_value(raw):
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return 0

order_ids = []
query = Q(agency=agency)
if agency.portal_user_id:
    query |= Q(user=agency.portal_user)
if agency.agn_name:
    query |= Q(payload__org__iexact=agency.agn_name)
    query |= Q(payload__org__icontains=agency.agn_name)
if agency.fio_agn:
    query |= Q(payload__fio__iexact=agency.fio_agn)
    query |= Q(payload__fio__icontains=agency.fio_agn)
if agency.email:
    query |= Q(payload__email__iexact=agency.email)
if query:
    order_ids = list(OrderAuditEntry.objects.filter(order_type='receiving').filter(query).values_list('order_id', flat=True).distinct())
print('order_ids', order_ids)

entries = OrderAuditEntry.objects.filter(order_type='receiving')
if order_ids:
    entries = entries.filter(order_id__in=order_ids)
else:
    entries = entries.none()
entries = entries.select_related('agency').order_by('-created_at')

latest_by_order = {}
blocked_orders = set()
for entry in entries:
    if entry.order_id in latest_by_order or entry.order_id in blocked_orders:
        continue
    payload = entry.payload or {}
    if payload.get('act') != 'placement':
        continue
    state = (payload.get('act_state') or 'closed').lower()
    if state != 'closed':
        blocked_orders.add(entry.order_id)
        continue
    latest_by_order[entry.order_id] = entry

print('latest_by_order', list(latest_by_order.keys()))

rows = []
def normalize_location(pallet):
    if not pallet:
        return 'PR'
    location_value = (pallet or {}).get('location')
    def normalize_zone(value: str) -> str:
        text = (value or '').strip()
        if not text:
            return ''
        if re.search(r'^pr$', text, re.IGNORECASE) or re.search(r'зона приемки|поле приемки', text, re.IGNORECASE):
            return 'PR'
        if re.search(r'^otg?$', text, re.IGNORECASE) or re.search(r'зона отгрузки|отгрузк', text, re.IGNORECASE):
            return 'OTG'
        if re.search(r'^mr$', text, re.IGNORECASE) or re.search(r'между ряд', text, re.IGNORECASE):
            return 'MR'
        if re.search(r'^os$', text, re.IGNORECASE) or re.search(r'основн|стеллаж|ряд|полк|секци|ярус|ячейк', text, re.IGNORECASE):
            return 'OS'
        return text.upper()
    def format_label(zone, row=0, section=0, tier=0, cell=0):
        if zone == 'PR':
            return 'PR · Зона приемки'
        if zone == 'OTG':
            return 'OTG · Зона отгрузки'
        if zone == 'MR':
            return f'MR · Между рядами · Ряд {row}' if row else 'MR · Между рядами'
        if zone == 'OS':
            if row and section and tier and cell:
                return f'OS · Ряд {row} · Секция {section} · Ярус {tier} · Ячейка {cell}'
            if row:
                return f'OS · Ряд {row}'
            return 'OS · Основной склад'
        return zone or 'PR'
    zone = ''
    row = section = tier = cell = 0
    if isinstance(location_value, str):
        zone = normalize_zone(location_value)
    elif isinstance(location_value, dict):
        zone = normalize_zone(location_value.get('zone') or '')
        row = _parse_int_value(location_value.get('row') or pallet.get('row'))
        section = _parse_int_value(location_value.get('section'))
        tier = _parse_int_value(location_value.get('tier'))
        cell = _parse_int_value(location_value.get('cell'))
        if not zone:
            rack = (location_value.get('rack') or pallet.get('rack') or '').strip()
            row_text = (location_value.get('row') or pallet.get('row') or '').strip()
            section_text = (location_value.get('section') or '').strip()
            tier_text = (location_value.get('tier') or '').strip()
            shelf = (location_value.get('shelf') or pallet.get('shelf') or '').strip()
            cell_text = (location_value.get('cell') or '').strip()
            if rack or row_text or section_text or tier_text or shelf or cell_text:
                zone = 'OS'
    if not zone:
        zone = normalize_zone(pallet.get('zone') or '')
    if not zone:
        rack = (pallet.get('rack') or '').strip()
        row_text = (pallet.get('row') or '').strip()
        shelf = (pallet.get('shelf') or '').strip()
        if rack or row_text or shelf:
            zone = 'OS'
    zone = zone or 'PR'
    return format_label(zone, row=row, section=section, tier=tier, cell=cell)

for entry in latest_by_order.values():
    payload = entry.payload or {}
    boxes = payload.get('act_boxes') or []
    pallets = payload.get('act_pallets') or []
    box_to_pallet = {}
    pallet_locations = {}
    for pallet in pallets:
        pallet_code = (pallet or {}).get('code') or ''
        if pallet_code:
            pallet_locations[pallet_code] = normalize_location(pallet)
        for box_code in (pallet or {}).get('boxes') or []:
            if box_code and pallet_code and box_code not in box_to_pallet:
                box_to_pallet[box_code] = pallet_code
    def append_row(item, box_code='-', pallet_code='-'):
        sku = (item.get('sku') or item.get('sku_code') or '').strip()
        name = (item.get('name') or '-').strip()
        size = (item.get('size') or '-').strip()
        qty = item.get('qty')
        if qty in (None, ''):
            qty = item.get('actual_qty') or 0
        location = pallet_locations.get(pallet_code) if pallet_code and pallet_code != '-' else 'PR'
        rows.append({'sku': sku or '-', 'name': name or '-', 'size': size or '-', 'qty': qty, 'location': location})
    for box in boxes:
        box_code = (box or {}).get('code') or '-'
        pallet_code = box_to_pallet.get(box_code, '-')
        for item in (box or {}).get('items') or []:
            append_row(item, box_code=box_code, pallet_code=pallet_code)
    for pallet in pallets:
        pallet_code = (pallet or {}).get('code') or '-'
        for item in (pallet or {}).get('items') or []:
            append_row(item, box_code='-', pallet_code=pallet_code)
    if not boxes and not pallets:
        for item in payload.get('act_items') or []:
            append_row(item, box_code='-', pallet_code='-')

print('rows', len(rows))
PY
