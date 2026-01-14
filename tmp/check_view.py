import os, sys
sys.path.insert(0, '/opt/fullbox/fullbox')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'fullbox.settings')
import django
django.setup()

from django.test import RequestFactory
from django.contrib.auth import get_user_model
from sklad.views import inventory_journal

User = get_user_model()
user = User.objects.filter(id=15).first()
print('user', user)

factory = RequestFactory()
request = factory.get('/sklad/journal/')
request.user = user

response = inventory_journal(request)
print('response status', response.status_code)
# response is HttpResponse with rendered content; to inspect context, use response.context_data only with TemplateResponse
try:
    print('context rows', len(response.context_data['rows']))
except Exception as exc:
    print('no context_data', exc)
    # fallback: check rendered content for "Записей пока нет"
    content = response.content.decode('utf-8')
    print('has empty', 'Записей пока нет' in content)
PY
