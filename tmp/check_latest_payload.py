import os, sys
sys.path.insert(0, '/opt/fullbox/fullbox')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'fullbox.settings')
import django
django.setup()
from audit.models import OrderAuditEntry

for order_id in ['6','7']:
    entries = OrderAuditEntry.objects.filter(order_type='receiving', order_id=order_id, payload__act='placement').order_by('-created_at')
    entry = entries.first()
    print('order', order_id, 'placement entries', entries.count())
    if entry:
        payload = entry.payload or {}
        print(' latest keys', sorted([k for k in payload.keys() if k.startswith('act') or k in ('status','status_label')])[:10])
        print(' act_boxes len', len(payload.get('act_boxes') or []), 'act_items len', len(payload.get('act_items') or []), 'act_pallets len', len(payload.get('act_pallets') or []))
PY
