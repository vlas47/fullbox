from django.conf import settings
from django.contrib.auth.mixins import AccessMixin
from django.http import HttpResponseForbidden

from .models import Employee


STAFF_ROLES = {
    "admin",
    "director",
    "head_manager",
    "processing_head",
    "manager",
    "accountant",
    "developer",
}


def is_staff_role(role: str | None) -> bool:
    return role in STAFF_ROLES


def get_employee_for_user(user):
    if not user or not getattr(user, "is_authenticated", False):
        return None
    return Employee.objects.filter(user=user, is_active=True).first()


def get_request_role(request):
    employee = get_employee_for_user(request.user)
    if employee:
        return employee.role
    if settings.DEBUG:
        role = request.session.get("employee_role")
        if role:
            return role
        if request.user.is_authenticated:
            return request.user.username
    return None


def role_required(*roles):
    def decorator(view_func):
        def _wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return HttpResponseForbidden("Доступ запрещен")
            role = get_request_role(request)
            if role not in roles:
                return HttpResponseForbidden("Доступ запрещен")
            return view_func(request, *args, **kwargs)

        return _wrapped

    return decorator


class RoleRequiredMixin(AccessMixin):
    allowed_roles = ()

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return HttpResponseForbidden("Доступ запрещен")
        role = get_request_role(request)
        if self.allowed_roles and role not in self.allowed_roles:
            return HttpResponseForbidden("Доступ запрещен")
        return super().dispatch(request, *args, **kwargs)


def resolve_cabinet_url(role: str | None) -> str:
    mapping = {
        "manager": "/team-manager/",
        "storekeeper": "/sklad/",
        "head_manager": "/head-manager/",
        "processing_head": "/processing-head/",
        "developer": "/dev/",
        "admin": "/admin/",
    }
    if role in mapping:
        return mapping[role]
    if role:
        return f"/cabinet/{role}/"
    return "/"
