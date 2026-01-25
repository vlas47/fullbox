from django.urls import path

from .views import LabelSettingsView

app_name = "labels"

urlpatterns = [
    path("settings/", LabelSettingsView.as_view(), name="settings"),
]
