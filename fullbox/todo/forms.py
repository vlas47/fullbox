from django import forms

from .models import Task


class TaskForm(forms.ModelForm):
    due_date = forms.DateTimeField(
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        label="Дедлайн",
    )

    class Meta:
        model = Task
        fields = ["title", "description", "status", "priority", "due_date"]
