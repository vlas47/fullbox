from django.contrib import admin

from .models import AuditEntry, AuditJournal


@admin.register(AuditJournal)
class AuditJournalAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "description")
    search_fields = ("code", "name", "description")
    ordering = ("code",)


@admin.register(AuditEntry)
class AuditEntryAdmin(admin.ModelAdmin):
    list_display = ("created_at", "journal", "action", "sku", "user", "description")
    list_filter = ("action", "user", "journal")
    search_fields = ("sku__sku_code", "sku__name", "description", "journal__name", "journal__code")
    ordering = ("-created_at",)
