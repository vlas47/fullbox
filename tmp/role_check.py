from employees.models import Employee

for e in Employee.objects.filter(is_active=True).order_by('id'):
    print(e.id, e.full_name, e.role, e.user_id)
