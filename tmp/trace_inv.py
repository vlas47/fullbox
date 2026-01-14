import os, sys
sys.path.insert(0, '/opt/fullbox/fullbox')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'fullbox.settings')
import django
django.setup()

import sys as pysys
from django.test import RequestFactory
from django.contrib.auth import get_user_model
import sklad.views as views

User = get_user_model()
user = User.objects.filter(id=15).first()

captured = {}

def tracer(frame, event, arg):
    if event == 'return' and frame.f_code is views.inventory_journal.__code__:
        locals_copy = dict(frame.f_locals)
        captured['order_ids'] = locals_copy.get('order_ids')
        captured['latest_by_order_len'] = len(locals_copy.get('latest_by_order') or {})
        captured['rows_len'] = len(locals_copy.get('rows') or [])
        captured['client_agency'] = locals_copy.get('client_agency')
    return tracer

pysys.settrace(tracer)
factory = RequestFactory()
request = factory.get('/sklad/journal/')
request.user = user
response = views.inventory_journal(request)
pysys.settrace(None)
print('captured', captured)
PY
