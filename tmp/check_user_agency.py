import os, sys
sys.path.insert(0, '/opt/fullbox/fullbox')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'fullbox.settings')
import django
django.setup()
from django.contrib.auth import get_user_model
from sku.models import Agency

User = get_user_model()
user = User.objects.filter(id=15).first()
print('user', user)
agency = Agency.objects.filter(portal_user=user).first()
print('agency', agency.id if agency else None, agency.agn_name if agency else None, agency.fio_agn if agency else None)
PY
