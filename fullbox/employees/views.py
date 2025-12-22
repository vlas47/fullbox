from django.shortcuts import render

from .models import Employee


def employee_list(request):
    employees = Employee.objects.all()
    stats = {
        "total": employees.count(),
        "active": employees.filter(is_active=True).count(),
        "inactive": employees.filter(is_active=False).count(),
    }
    return render(request, "employees/employee_list.html", {"employees": employees, "stats": stats})
