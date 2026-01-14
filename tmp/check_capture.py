import os, sys
sys.path.insert(0, '/opt/fullbox/fullbox')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'fullbox.settings')
import django
django.setup()

from django.test import RequestFactory
from django.contrib.auth import get_user_model
import sklad.views as views

User = get_user_model()
user = User.objects.filter(id=15).first()

captured = {}

def fake_render(request, template_name, context):
    captured['template'] = template_name
    captured['rows_len'] = len(context.get('rows') or [])
    captured['order_ids'] = context.get('order_ids')
    from django.http import HttpResponse
    return HttpResponse('ok')

views.render = fake_render

factory = RequestFactory()
request = factory.get('/sklad/journal/')
request.user = user
response = views.inventory_journal(request)
print('response', response.status_code)
print('captured', captured)
PY
