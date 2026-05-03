from django.http import HttpResponseForbidden
from django.shortcuts import render

from employees.access import role_required

from .ui_services import build_inventory_journal_page


@role_required("storekeeper")
def dashboard(request):
    return render(request, "sklad/dashboard.html")


def inventory_journal(request):
    page = build_inventory_journal_page(request=request)
    if isinstance(page, HttpResponseForbidden):
        return page
    return render(request, page["template_name"], page["context"])
