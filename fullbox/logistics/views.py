from __future__ import annotations

from django.shortcuts import render

from employees.access import get_employee_for_user, get_request_role, role_required

from .services import (
    build_logistics_dashboard_context,
    build_logistics_trip_detail_context,
    build_logistics_trip_list_context,
    build_logistics_trip_loading_context,
    get_trip_detail_trip,
    get_trip_loading_trip,
    handle_logistics_dashboard_post,
    handle_logistics_trip_detail_post,
    handle_logistics_trip_loading_post,
)


ALLOWED_ROLES = ("logistician", "head_manager", "director", "admin", "developer")
TRIP_READ_ROLES = ALLOWED_ROLES + ("storekeeper",)


@role_required(*ALLOWED_ROLES)
def logistics_dashboard(request):
    role = get_request_role(request)
    employee = get_employee_for_user(request.user)
    if request.method == "POST":
        response = handle_logistics_dashboard_post(request, role=role, employee=employee)
        if response is not None:
            return response
    context = build_logistics_dashboard_context(role=role, employee=employee)
    return render(request, "logistics/dashboard.html", context)


@role_required(*TRIP_READ_ROLES)
def logistics_trip_list(request):
    role = get_request_role(request)
    context = build_logistics_trip_list_context(role=role)
    return render(request, "logistics/trip_list.html", context)


@role_required(*TRIP_READ_ROLES)
def logistics_trip_detail(request, pk: int):
    role = get_request_role(request)
    trip = get_trip_detail_trip(pk)
    if request.method == "POST":
        response = handle_logistics_trip_detail_post(request, role=role, trip=trip)
        if response is not None:
            return response
    context = build_logistics_trip_detail_context(role=role, trip=trip)
    return render(request, "logistics/trip_detail.html", context)


@role_required(*TRIP_READ_ROLES)
def logistics_trip_loading(request, pk: int):
    role = get_request_role(request)
    trip = get_trip_loading_trip(pk)
    if request.method == "POST":
        return handle_logistics_trip_loading_post(request, role=role, trip=trip)
    context = build_logistics_trip_loading_context(role=role, trip=trip)
    return render(request, "logistics/trip_loading.html", context)
