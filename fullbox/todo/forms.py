from django import forms
from django.utils import timezone

from employees.models import Employee

from .models import Task, TaskComment, default_due_date


class TaskForm(forms.ModelForm):
    due_date = forms.DateTimeField(
        required=True,
        widget=forms.DateTimeInput(
            attrs={"type": "datetime-local"},
            format="%Y-%m-%dT%H:%M",
        ),
        input_formats=["%Y-%m-%dT%H:%M"],
        label="Дедлайн",
    )
    assigned_to = forms.ModelChoiceField(
        queryset=Employee.objects.none(),
        required=False,
        label="Исполнитель",
        empty_label="Не назначен",
    )
    observer = forms.ModelChoiceField(
        queryset=Employee.objects.none(),
        required=False,
        label="Наблюдатель",
        empty_label="Не назначен",
    )

    class Meta:
        model = Task
        fields = [
            "title",
            "description",
            "assigned_to",
            "observer",
            "priority",
            "due_date",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            if self.instance and self.instance.pk and self.instance.due_date:
                self.initial["due_date"] = timezone.localtime(self.instance.due_date)
            else:
                self.initial["due_date"] = timezone.localtime(default_due_date())
        active_employees = Employee.objects.filter(is_active=True)
        extra_ids = []
        if self.instance and self.instance.assigned_to_id:
            extra_ids.append(self.instance.assigned_to_id)
        if self.instance and self.instance.observer_id:
            extra_ids.append(self.instance.observer_id)
        if extra_ids:
            active_employees = active_employees | Employee.objects.filter(
                id__in=extra_ids
            )
        self.fields["assigned_to"].queryset = active_employees.distinct()
        self.fields["observer"].queryset = active_employees.distinct()


class TaskCommentForm(forms.ModelForm):
    class Meta:
        model = TaskComment
        fields = ["body"]
        widgets = {
            "body": forms.Textarea(
                attrs={
                    "rows": 3,
                    "placeholder": "Добавьте комментарий",
                }
            )
        }
        labels = {"body": "Комментарий"}


class MultiFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class TaskAttachmentForm(forms.Form):
    files = forms.FileField(
        label="Файлы",
        required=False,
        widget=MultiFileInput(attrs={"multiple": True}),
    )
