import os, sys
sys.path.insert(0, '/opt/fullbox/fullbox')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'fullbox.settings')
import django
django.setup()

from django.db.models import Q
from django.test import RequestFactory
from django.contrib.auth import get_user_model
from audit.models import OrderAuditEntry
from sku.models import Agency
from sklad.views import inventory_journal

User = get_user_model()
user = User.objects.filter(id=15).first()
agency = Agency.objects.filter(portal_user=user).first()

query = Q()
if agency:
    query |= Q(agency=agency)
    if agency.portal_user:
        query |= Q(user=agency.portal_user)
    if agency.agn_name:
        query |= Q(payload__org__iexact=agency.agn_name)
        query |= Q(payload__org__icontains=agency.agn_name)
    if agency.fio_agn:
        query |= Q(payload__fio__iexact=agency.fio_agn)
        query |= Q(payload__fio__icontains=agency.fio_agn)
    if agency.email:
        query |= Q(payload__email__iexact=agency.email)
order_ids = list(
    OrderAuditEntry.objects.filter(order_type='receiving').filter(query).values_list('order_id', flat=True).distinct()
) if query else []
print('order_ids', order_ids)

entries = OrderAuditEntry.objects.filter(order_type='receiving')
if agency:
    if order_ids:
        entries = entries.filter(order_id__in=order_ids)
    else:
        entries = entries.none()
print('entries count', entries.count())

factory = RequestFactory()
request = factory.get('/sklad/journal/')
request.user = user
response = inventory_journal(request)
content = response.content.decode('utf-8')
print('has empty', 'Записей пока нет' in content)
