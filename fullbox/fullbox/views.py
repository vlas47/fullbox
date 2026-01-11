from pathlib import Path
import time

import requests
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.http import (
    FileResponse,
    HttpResponse,
    HttpResponseForbidden,
    HttpResponseNotFound,
    HttpResponseRedirect,
)
from django.shortcuts import redirect, render
from django.utils.html import escape
from employees.models import Employee

from django.conf import settings
from employees.access import get_employee_for_user, get_request_role, resolve_cabinet_url
from sku.models import Agency


DEV_USERS = [
    ("admin", "Администратор"),
    ("director", "Директор"),
    ("accountant", "Бухгалтер"),
    ("head_manager", "Главный менеджер"),
    ("manager", "Менеджер"),
    ("storekeeper", "Кладовщик"),
    ("picker", "Сборщик"),
    ("developer", "Разработчик"),
]

ROLE_TITLES = dict(DEV_USERS)
_REMOTE_JOURNAL_CACHE = {"ts": 0.0, "data": None}
_FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
    "<rect width='64' height='64' rx='12' fill='#c79a1c'/>"
    "<path d='M20 46V18h24v8H30v4h12v8H30v8h-10z' fill='#1f2328'/>"
    "</svg>"
)


def login_menu(request):
    """Простое меню входа для разработки."""
    if not settings.DEBUG:
        return HttpResponseNotFound()
    usernames = [u[0] for u in DEV_USERS]
    existing = (
        get_user_model()
        .objects.filter(username__in=usernames)
        .values_list("username", flat=True)
    )
    items = [
        {"username": username, "label": label}
        for username, label in DEV_USERS
        if username in existing
    ]
    agencies = Agency.objects.order_by("agn_name")
    return render(request, "login_menu.html", {"users": items, "agencies": agencies})


def dev_login(request, username):
    """Быстрый вход для разработки без ввода пароля."""
    if not settings.DEBUG:
        return HttpResponseNotFound()
    User = get_user_model()
    try:
        user = User.objects.get(username=username)
    except User.DoesNotExist:
        return HttpResponseRedirect("/login-menu/")

    backend = "django.contrib.auth.backends.ModelBackend"
    user.backend = backend
    login(request, user, backend=backend)
    next_url = request.GET.get("next")
    if next_url and not next_url.startswith("/"):
        next_url = None

    employee_role = None
    if username == "manager":
        employee_role = "manager"
    elif username == "storekeeper":
        employee_role = "storekeeper"

    if employee_role:
        employee = (
            Employee.objects.filter(role=employee_role, is_active=True)
            .order_by("full_name")
            .first()
        )
        if employee:
            request.session["employee_id"] = employee.id
            request.session["employee_name"] = employee.full_name
            request.session["employee_role"] = employee.role
        else:
            request.session.pop("employee_id", None)
            request.session.pop("employee_name", None)
            request.session.pop("employee_role", None)
    else:
        request.session.pop("employee_id", None)
        request.session.pop("employee_name", None)
        request.session.pop("employee_role", None)

    if username == "developer":
        target = next_url or "/dev/"
    elif username == "admin":
        target = next_url or "/admin/"
    else:
        target = next_url or f"/cabinet/{username}/"
    return redirect(target)


def role_cabinet(request, role):
    """Простой кабинет для каждой роли (временно)."""
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Доступ запрещен")
    current_role = get_request_role(request)
    if current_role != role:
        return HttpResponseForbidden("Доступ запрещен")
    title = ROLE_TITLES.get(role, "Кабинет")
    return render(request, "role_cabinet.html", {"role": role, "title": title})


def sign_in(request):
    if request.user.is_authenticated:
        role = get_request_role(request)
        if role:
            return redirect(resolve_cabinet_url(role))
        agency = Agency.objects.filter(portal_user=request.user).first()
        if agency:
            return redirect(f"/client/dashboard/?client={agency.id}")
    error = None
    employees = Employee.objects.select_related("user").order_by("role", "full_name")
    clients = Agency.objects.select_related("portal_user").order_by("agn_name")
    if request.method == "POST":
        username = (request.POST.get("username") or "").strip()
        password = request.POST.get("password") or ""
        user = authenticate(request, username=username, password=password)
        if not user:
            error = "Неверный логин или пароль"
        else:
            login(request, user)
            employee = get_employee_for_user(user)
            agency = Agency.objects.filter(portal_user=user).first()
            if employee:
                request.session["employee_name"] = employee.full_name
                request.session["employee_role"] = employee.role
                return redirect(resolve_cabinet_url(get_request_role(request)))
            if agency:
                request.session.pop("employee_name", None)
                request.session.pop("employee_role", None)
                return redirect(f"/client/dashboard/?client={agency.id}")
            else:
                request.session.pop("employee_name", None)
                request.session.pop("employee_role", None)
            return redirect("/")
    return render(
        request,
        "login.html",
        {
            "error": error,
            "employees": employees,
            "clients": clients,
        },
    )


def sign_out(request):
    logout(request)
    request.session.flush()
    return redirect("/login/")


def favicon(request):
    response = HttpResponse(_FAVICON_SVG, content_type="image/svg+xml")
    response["Cache-Control"] = "public, max-age=86400"
    return response


def project_description(request):
    """Описание проекта для кабинета директора."""
    sections = _load_sections(settings.BASE_DIR.parent / "README.md")
    return render(
        request,
        "project_text.html",
        {
            "title": "Описание проекта",
            "subtitle": "Актуальное описание Fullbox из README.md",
            "sections": sections,
        },
    )


def development_journal(request):
    """Журнал разработки для кабинета директора."""
    local_path = settings.BASE_DIR.parent / "journal.md"
    remote_url = getattr(settings, "JOURNAL_REMOTE_URL", "")
    cache_seconds = getattr(settings, "JOURNAL_REMOTE_CACHE_SECONDS", 300)
    remote_data = _load_remote_text(remote_url, cache_seconds)
    if remote_data:
        sections = _load_sections_from_text(remote_data)
    else:
        sections = _load_sections(local_path)
    return render(
        request,
        "project_text.html",
        {
            "title": "Журнал разработки",
            "subtitle": "Хронология изменений проекта",
            "sections": sections,
        },
    )


def project_description_file(request):
    return _file_response(settings.BASE_DIR.parent / "README.md", "README.md")


def development_journal_file(request):
    local_path = settings.BASE_DIR.parent / "journal.md"
    remote_url = getattr(settings, "JOURNAL_REMOTE_URL", "")
    cache_seconds = getattr(settings, "JOURNAL_REMOTE_CACHE_SECONDS", 300)
    remote_data = _load_remote_text(remote_url, cache_seconds)
    if remote_data is not None:
        response = HttpResponse(remote_data, content_type="text/markdown; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="journal.md"'
        return response
    return _file_response(local_path, "journal.md")


def _load_text_file(path: Path) -> str:
    try:
        data = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "Файл не найден."
    except OSError:
        return "Не удалось прочитать файл."
    return escape(data)


def _load_sections(path: Path) -> list[dict]:
    try:
        data = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [{"title": "Ошибка", "body": "Файл не найден."}]
    except OSError:
        return [{"title": "Ошибка", "body": "Не удалось прочитать файл."}]

    return _load_sections_from_text(data)


def _load_sections_from_text(data: str) -> list[dict]:
    lines = data.splitlines()
    sections = []
    current = {"title": "Документ", "body": []}
    for line in lines:
        if line.lstrip().startswith("#"):
            if current["body"] or current["title"] != "Документ":
                sections.append(current)
            title = line.lstrip("#").strip() or "Раздел"
            current = {"title": title, "body": []}
        else:
            current["body"].append(line)
    if current["body"] or current["title"] != "Документ":
        sections.append(current)

    accents = ["#d6a300", "#2dd4bf", "#60a5fa", "#f97316", "#34d399", "#f43f5e"]
    decorated = []
    for idx, section in enumerate(sections):
        decorated.append(
            {
                "title": escape(section["title"]),
                "body": escape("\n".join(section["body"]).strip()),
                "accent": accents[idx % len(accents)],
            }
        )
    return decorated


def _load_remote_text(url: str, cache_seconds: int):
    if not url:
        return None
    now = time.time()
    cached = _REMOTE_JOURNAL_CACHE.get("data")
    if cached and now - _REMOTE_JOURNAL_CACHE.get("ts", 0) < cache_seconds:
        return cached
    try:
        response = requests.get(url, timeout=4)
        response.raise_for_status()
        data = response.text
    except requests.RequestException:
        return cached
    _REMOTE_JOURNAL_CACHE["ts"] = now
    _REMOTE_JOURNAL_CACHE["data"] = data
    return data


def _file_response(path: Path, filename: str):
    try:
        return FileResponse(open(path, "rb"), as_attachment=True, filename=filename)
    except FileNotFoundError:
        return HttpResponse("Файл не найден.", status=404)
    except OSError:
        return HttpResponse("Не удалось прочитать файл.", status=500)
