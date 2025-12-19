from django.contrib import admin

from .models import AuditEntry


@admin.register(AuditEntry)
class AuditEntryAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "sku", "user", "description")
    list_filter = ("action", "user")
    search_fields = ("sku__sku_code", "sku__name", "description")
    ordering = ("-created_at",)
