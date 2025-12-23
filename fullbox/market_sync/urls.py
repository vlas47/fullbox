from django.urls import path

from .views import dashboard, wb_settings, wb_sync_run, ozon_settings

urlpatterns = [
    path("", dashboard, name="market-sync"),
    path("wb/", wb_settings, name="market-sync-wb"),
    path("ozon/", ozon_settings, name="market-sync-ozon"),
    path("wb/run/", wb_sync_run, name="market-sync-wb-run"),
]
