from django.conf import settings
from django.contrib.auth.mixins import AccessMixin
from django.http import HttpResponseForbidden

from .models import Employee


STAFF_ROLES = {
    "admin",
    "director",
    "head_manager",
    "processing_head",
    "processing_worker",
    "manager",
    "storekeeper",
    "logistician",
    "accountant",
    "reachtruck_driver",
    "developer",
}


def is_staff_role(role: str | None) -> bool:
    return role in STAFF_ROLES


def get_employee_for_user(user, *, preferred_id: int | None = None, preferred_role: str | None = None):
    if not user or not getattr(user, "is_authenticated", False):
        return None
    employees = Employee.objects.filter(user=user, is_active=True).order_by("id")
    if preferred_id:
        employee = employees.filter(id=preferred_id).first()
        if employee:
            return employee
    if preferred_role:
        employee = employees.filter(role=preferred_role).first()
        if employee:
            return employee
    return employees.first()


def get_request_employee(request):
    if not request or not getattr(request, "user", None):
        return None
    preferred_id = None
    preferred_role = None
    session = getattr(request, "session", None)
    if session is not None:
        raw_id = session.get("employee_id")
        try:
            preferred_id = int(raw_id)
        except (TypeError, ValueError):
            preferred_id = None
        preferred_role = str(session.get("employee_role") or "").strip() or None
    return get_employee_for_user(
        request.user,
        preferred_id=preferred_id,
        preferred_role=preferred_role,
    )


def get_request_role(request):
    employee = get_request_employee(request)
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
        "logistician": "/logistics/",
        "head_manager": "/head-manager/",
        "processing_head": "/processing-head/",
        "processing_worker": "/processing-worker/",
        "reachtruck_driver": "/reachtruck/",
        "developer": "/dev/",
        "admin": "/admin/",
    }
    if role in mapping:
        return mapping[role]
    if role:
        return f"/cabinet/{role}/"
    return "/"
