import os, sys
sys.path.insert(0, '/opt/fullbox/fullbox')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'fullbox.settings')
import django
django.setup()

from django.test import RequestFactory
from django.contrib.auth import get_user_model
from django.db import connection
from audit.models import OrderAuditEntry
from sku.models import Agency
from sklad.views import inventory_journal

print('db', connection.settings_dict.get('ENGINE'), connection.settings_dict.get('NAME'))
print('entries total', OrderAuditEntry.objects.count())
print('receiving', OrderAuditEntry.objects.filter(order_type='receiving').count())

User = get_user_model()
user = User.objects.filter(id=15).first()
print('user', user, 'is_authenticated', user.is_authenticated)
agency = Agency.objects.filter(portal_user=user).first()
print('agency', agency.id if agency else None, agency.agn_name if agency else None)

factory = RequestFactory()
request = factory.get('/sklad/journal/')
request.user = user
response = inventory_journal(request)
content = response.content.decode('utf-8')
print('has empty', 'Записей пока нет' in content)
print('tr count', content.count('<tr>'))
