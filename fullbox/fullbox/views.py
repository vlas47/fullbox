from pathlib import Path

from django.contrib.auth import get_user_model, login
from django.http import HttpResponseRedirect
from django.shortcuts import redirect, render
from django.utils.html import escape
from sku.models import Agency

from django.conf import settings


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


def login_menu(request):
    """Простое меню входа для разработки."""
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

    if username == "developer":
        target = next_url or "/dev/"
    elif username == "admin":
        target = next_url or "/admin/"
    else:
        target = next_url or f"/cabinet/{username}/"
    return redirect(target)


def role_cabinet(request, role):
    """Простой кабинет для каждой роли (временно)."""
    title = ROLE_TITLES.get(role, "Кабинет")
    return render(request, "role_cabinet.html", {"role": role, "title": title})


def project_description(request):
    """Описание проекта для кабинета директора."""
    content = _load_text_file(settings.BASE_DIR / "README.md")
    return render(
        request,
        "project_text.html",
        {
            "title": "Описание проекта",
            "subtitle": "Актуальное описание Fullbox из README.md",
            "content": content,
        },
    )


def development_journal(request):
    """Журнал разработки для кабинета директора."""
    content = _load_text_file(settings.BASE_DIR / "journal.md")
    return render(
        request,
        "project_text.html",
        {
            "title": "Журнал разработки",
            "subtitle": "Хронология изменений проекта",
            "content": content,
        },
    )


def _load_text_file(path: Path) -> str:
    try:
        data = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "Файл не найден."
    except OSError:
        return "Не удалось прочитать файл."
    return escape(data)
