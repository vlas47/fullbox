import os, sys, re
sys.path.insert(0, '/opt/fullbox/fullbox')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'fullbox.settings')
import django
django.setup()

from django.test import RequestFactory
from django.contrib.auth import get_user_model
from sklad.views import inventory_journal

User = get_user_model()
user = User.objects.filter(id=15).first()
factory = RequestFactory()
request = factory.get('/sklad/journal/')
request.user = user
response = inventory_journal(request)
content = response.content.decode('utf-8')
print('rows text count', content.count('<tr>'))
# extract tbody contents
match = re.search(r'<tbody>(.*?)</tbody>', content, re.S)
if match:
    snippet = match.group(1)
    print('tbody snippet', snippet.strip()[:400])
PY
