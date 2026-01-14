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
if agency.portal_user:
    query |= Q(user=agency.portal_user)
if agency.agn_name:
    query |= Q(payload__org__iexact=agency.agn_name)
    query |= Q(payload__org__icontains=agency.agn_name)
order_ids = list(OrderAuditEntry.objects.filter(order_type='receiving').filter(query).values_list('order_id', flat=True).distinct())
print('order_ids', order_ids)
placement_entries = OrderAuditEntry.objects.filter(order_type='receiving', payload__act='placement')
placement_entries = placement_entries.filter(order_id__in=order_ids)
print('placement count', placement_entries.count())
closed = placement_entries.filter(payload__act_state='closed').count()
print('placement closed', closed)
PY
