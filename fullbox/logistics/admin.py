from django.contrib import admin

from .models import LogisticsTrip, LogisticsTripOrder


class LogisticsTripOrderInline(admin.TabularInline):
    model = LogisticsTripOrder
    extra = 0


@admin.register(LogisticsTrip)
class LogisticsTripAdmin(admin.ModelAdmin):
    list_display = ("number", "trip_date", "status", "assigned_logistician", "vehicle_name", "vehicle_number", "created_at")
    list_filter = ("status", "trip_date", "vehicle_type")
    search_fields = ("number", "vehicle_name", "vehicle_number", "driver_name")
    inlines = [LogisticsTripOrderInline]


@admin.register(LogisticsTripOrder)
class LogisticsTripOrderAdmin(admin.ModelAdmin):
    list_display = ("trip", "shipping_order", "loading_sequence", "delivery_sequence", "updated_at")
    list_filter = ("trip__status",)
    search_fields = ("trip__number", "shipping_order__number", "shipping_order__agency__agn_name")

