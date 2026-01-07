from django import forms
from django.core.exceptions import ValidationError

from PIL import Image
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from .models import Employee
from employees.access import RoleRequiredMixin, get_request_role, resolve_cabinet_url


class EmployeeForm(forms.ModelForm):
    class Meta:
        model = Employee
        fields = ("full_name", "role", "email", "phone", "facsimile", "is_active")
        widgets = {
            "facsimile": forms.ClearableFileInput(attrs={"accept": "image/png"}),
        }

    def clean_facsimile(self):
        facsimile = self.cleaned_data.get("facsimile")
        if not facsimile:
            return facsimile
        content_type = getattr(facsimile, "content_type", "")
        if content_type and content_type.lower() != "image/png":
            raise ValidationError("Факсимиле нужно загрузить в формате PNG.")
        try:
            facsimile.seek(0)
            image = Image.open(facsimile)
            if image.format != "PNG":
                raise ValidationError("Факсимиле нужно загрузить в формате PNG.")
        except ValidationError:
            raise
        except Exception as exc:
            raise ValidationError("Не удалось прочитать PNG-файл.") from exc
        finally:
            facsimile.seek(0)
        return facsimile


def employee_list(request):
    employees = Employee.objects.all()
    stats = {
        "total": employees.count(),
        "active": employees.filter(is_active=True).count(),
        "inactive": employees.filter(is_active=False).count(),
    }
    role = get_request_role(request)
    can_edit = role in {"head_manager", "director", "admin"}
    cabinet_url = resolve_cabinet_url(role)
    return render(
        request,
        "employees/employee_list.html",
        {
            "employees": employees,
            "stats": stats,
            "can_edit": can_edit,
            "cabinet_url": cabinet_url,
        },
    )


class EmployeeEditView(RoleRequiredMixin, View):
    allowed_roles = ("head_manager", "director", "admin")
    template_name = "employees/employee_form.html"

    def get(self, request, pk: int):
        employee = get_object_or_404(Employee, pk=pk)
        form = EmployeeForm(instance=employee)
        return render(
            request,
            self.template_name,
            {
                "employee": employee,
                "form": form,
                "cabinet_url": resolve_cabinet_url(get_request_role(request)),
            },
        )

    def post(self, request, pk: int):
        employee = get_object_or_404(Employee, pk=pk)
        form = EmployeeForm(request.POST, request.FILES, instance=employee)
        if form.is_valid():
            form.save()
            return redirect("/employees/")
        return render(
            request,
            self.template_name,
            {
                "employee": employee,
                "form": form,
                "cabinet_url": resolve_cabinet_url(get_request_role(request)),
            },
        )
