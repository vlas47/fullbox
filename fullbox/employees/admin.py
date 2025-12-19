from django.contrib import admin

from .models import Employee


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'role', 'email', 'phone', 'is_active', 'updated_at')
    list_filter = ('role', 'is_active')
    search_fields = ('full_name', 'email', 'phone')
    ordering = ('full_name',)
