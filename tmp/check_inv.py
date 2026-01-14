import os, sys
sys.path.insert(0, '/opt/fullbox/fullbox')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'fullbox.settings')
import django
django.setup()
from django.db import connection
from audit.models import OrderAuditEntry

print('db', connection.settings_dict.get('ENGINE'), connection.settings_dict.get('NAME'))
print('entries total', OrderAuditEntry.objects.count())
print('receiving', OrderAuditEntry.objects.filter(order_type='receiving').count())
print('placement', OrderAuditEntry.objects.filter(order_type='receiving', payload__act='placement').count())
print('placement closed', OrderAuditEntry.objects.filter(order_type='receiving', payload__act='placement', payload__act_state='closed').count())
PY
