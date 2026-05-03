from django.conf import settings
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from .web_ui import (
    build_development_journal_context,
    build_project_text_context,
    dev_login_response,
    development_journal_file_response,
    favicon_response,
    file_response,
    landing_submit_response,
    login_menu_response,
    role_cabinet_response,
    sign_in_response,
    sign_out_response,
)


def login_menu(request):
    return login_menu_response(request)


def dev_login(request, username):
    return dev_login_response(request, username)


def role_cabinet(request, role):
    return role_cabinet_response(request, role)


def sign_in(request):
    return sign_in_response(request)


def sign_out(request):
    return sign_out_response(request)


def favicon(request):
    return favicon_response()


@csrf_exempt
def landing_submit(request):
    return landing_submit_response(request)


def project_description(request):
    return render(
        request,
        "project_text.html",
        build_project_text_context(
            title="Описание проекта",
            subtitle="Актуальное описание Fullbox из README.md",
            path=settings.BASE_DIR.parent / "README.md",
        ),
    )


def project_structure(request):
    return render(
        request,
        "project_text.html",
        build_project_text_context(
            title="Описание структуры проекта",
            subtitle="Архитектура, границы модулей, рекомендации и риски",
            path=settings.BASE_DIR.parent / "architecture.md",
            action_buttons=[
                {"label": "Блок-схема проекта", "url": "/project-structure/diagram/", "variant": "primary"},
                {"label": "Журнал разработки", "url": "/development-journal/"},
            ],
        ),
    )


def project_block_diagram(request):
    return render(
        request,
        "project_block_diagram.html",
        {
            "title": "Блок-схема проекта",
            "subtitle": "Связи между ролями, бизнес-процессами и системными модулями",
        },
    )


def development_journal(request):
    return render(request, "project_text.html", build_development_journal_context())


def project_description_file(request):
    return file_response(settings.BASE_DIR.parent / "README.md", "README.md")


def development_journal_file(request):
    return development_journal_file_response()
