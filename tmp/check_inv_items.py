import os, sys
sys.path.insert(0, '/opt/fullbox/fullbox')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'fullbox.settings')
import django
django.setup()
from audit.models import OrderAuditEntry

for order_id in ['6','7']:
    entry = OrderAuditEntry.objects.filter(order_type='receiving', order_id=order_id, payload__act='placement').order_by('-created_at').first()
    print('order', order_id, 'entry', bool(entry))
    if not entry:
        continue
    payload = entry.payload or {}
    boxes = payload.get('act_boxes') or []
    pallets = payload.get('act_pallets') or []
    items = payload.get('act_items') or []
    print(' boxes', len(boxes), 'pallets', len(pallets), 'items', len(items))
    if boxes:
        print(' box items', sum(len((box or {}).get('items') or []) for box in boxes))
    if pallets:
        print(' pallet items', sum(len((pallet or {}).get('items') or []) for pallet in pallets))
PY
