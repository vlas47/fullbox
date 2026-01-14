import os, sys, inspect
sys.path.insert(0, '/opt/fullbox/fullbox')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'fullbox.settings')
import django
django.setup()
import sklad.views as v
source = inspect.getsource(v.inventory_journal)
print('placement_entries' in source)
print(source.split('\n')[0])
PY
