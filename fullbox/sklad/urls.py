from django.urls import path

from .views import dashboard, inventory_journal

app_name = "sklad"

urlpatterns = [
    path("", dashboard, name="dashboard"),
    path("journal/", inventory_journal, name="inventory_journal"),
]
