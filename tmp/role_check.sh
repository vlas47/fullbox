#!/bin/bash
cd /opt/fullbox
source .venv/bin/activate
set -a
source .env
set +a
python fullbox/manage.py shell -c "from employees.models import Employee; print([(e.id,e.full_name,e.role,e.user_id) for e in Employee.objects.filter(is_active=True)])"
