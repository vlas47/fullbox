import os, sys
sys.path.insert(0, '/opt/fullbox/fullbox')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'fullbox.settings')
import django
django.setup()

from django.db.models import Q
from audit.models import OrderAuditEntry
from sku.models import Agency

agency = Agency.objects.filter(pk=2638).first()
query = Q(agency=agency)
entries = OrderAuditEntry.objects.filter(order_type='receiving').filter(query).select_related('agency').order_by('-created_at')
print('entries count', entries.count())

for entry in entries:
    _ = entry.order_id

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
