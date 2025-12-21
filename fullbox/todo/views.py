from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from .forms import TaskForm
from .models import Task


def task_list(request):
    tasks = Task.objects.all()
    stats = {
        "total": tasks.count(),
        "backlog": tasks.filter(status="backlog").count(),
        "in_progress": tasks.filter(status="in_progress").count(),
        "done": tasks.filter(status="done").count(),
        "blocked": tasks.filter(status="blocked").count(),
    }
    return render(request, "todo/task_list.html", {"tasks": tasks, "stats": stats})


def task_create(request):
    if request.method == "POST":
        form = TaskForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Задача создана")
            return redirect("todo:list")
    else:
        form = TaskForm()
    return render(request, "todo/task_form.html", {"form": form, "title": "Новая задача"})


def task_update(request, pk):
    task = get_object_or_404(Task, pk=pk)
    if request.method == "POST":
        form = TaskForm(request.POST, instance=task)
        if form.is_valid():
            form.save()
            messages.success(request, "Задача обновлена")
            return redirect("todo:list")
    else:
        form = TaskForm(instance=task)
    return render(request, "todo/task_form.html", {"form": form, "title": f"Редактирование: {task.title}"})


def task_delete(request, pk):
    task = get_object_or_404(Task, pk=pk)
    if request.method == "POST":
        task.delete()
        messages.success(request, "Задача удалена")
        return redirect("todo:list")
    return render(request, "todo/task_delete_confirm.html", {"task": task})
