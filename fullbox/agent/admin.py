from django.contrib import admin

from .models import AgentCommand, AgentContext, AgentEvent, DeviceAgent


@admin.register(DeviceAgent)
class DeviceAgentAdmin(admin.ModelAdmin):
    list_display = ("agent_id", "name", "host", "version", "last_seen", "last_ip")
    search_fields = ("agent_id", "name", "host")
    readonly_fields = ("created_at", "updated_at", "last_seen")


@admin.register(AgentCommand)
class AgentCommandAdmin(admin.ModelAdmin):
    list_display = ("id", "agent_id", "command", "status", "created_at", "delivered_at", "acked_at")
    list_filter = ("status", "command")
    search_fields = ("agent_id", "command")
    readonly_fields = ("created_at", "updated_at", "delivered_at", "acked_at")


@admin.register(AgentEvent)
class AgentEventAdmin(admin.ModelAdmin):
    list_display = ("id", "agent_id", "event_type", "context_id", "created_at")
    list_filter = ("event_type",)
    search_fields = ("agent_id", "context_id")
    readonly_fields = ("created_at",)


@admin.register(AgentContext)
class AgentContextAdmin(admin.ModelAdmin):
    list_display = ("agent_id", "context_id", "user", "role", "order_id", "active", "expires_at")
    list_filter = ("active", "role")
    search_fields = ("agent_id", "context_id", "user__username", "user__email")
    readonly_fields = ("created_at", "updated_at", "last_seen", "expires_at")
