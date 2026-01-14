import os, sys
sys.path.insert(0, '/opt/fullbox/fullbox')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'fullbox.settings')
import django
django.setup()
from django.contrib.auth import get_user_model

User = get_user_model()
user = User.objects.filter(id=15).first()
print('is_staff', user.is_staff)
PY
