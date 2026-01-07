from django.urls import path

from . import views

app_name = "employees"

urlpatterns = [
    path("", views.employee_list, name="list"),
    path("<int:pk>/edit/", views.EmployeeEditView.as_view(), name="edit"),
]
