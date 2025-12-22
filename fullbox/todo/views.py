from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from .forms import TaskAttachmentForm, TaskCommentForm, TaskForm
from .models import Task, TaskAttachment


def task_list(request):
    tasks = Task.objects.all()
    return render(request, "todo/task_list.html", {"tasks": tasks})


def task_create(request):
    if request.method == "POST":
        form = TaskForm(request.POST)
        if form.is_valid():
            task = form.save(commit=False)
            if request.user.is_authenticated:
                task.created_by = request.user
            task.save()
            messages.success(request, "Задача создана")
            return redirect("todo:list")
    else:
        form = TaskForm()
    return render(request, "todo/task_form.html", {"form": form, "title": "Новая задача"})


def task_update(request, pk):
    task = get_object_or_404(Task, pk=pk)
    if not (request.user.is_authenticated and task.created_by_id == request.user.id):
        messages.error(request, "Редактировать задачу может только постановщик")
        return redirect("todo:detail", pk=task.pk)
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


def task_detail(request, pk):
    task = get_object_or_404(Task, pk=pk)
    can_complete = (
        request.user.is_authenticated
        and task.assigned_to
        and request.user.username == task.assigned_to.role
    )
    comment_form = TaskCommentForm()
    attachment_form = TaskAttachmentForm()
    can_edit = request.user.is_authenticated and task.created_by_id == request.user.id

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "complete":
            if can_complete:
                task.status = "done"
                task.save(update_fields=["status", "updated_at"])
                messages.success(request, "Задача отмечена как выполненная")
            else:
                messages.error(request, "Недостаточно прав для завершения задачи")
            return redirect("todo:detail", pk=task.pk)

        if action == "rework":
            if not can_edit:
                messages.error(request, "Вернуть на доработку может только постановщик")
                return redirect("todo:detail", pk=task.pk)
            if task.status != "done":
                messages.error(request, "Вернуть на доработку можно только выполненную задачу")
                return redirect("todo:detail", pk=task.pk)
            comment_form = TaskCommentForm(request.POST)
            if comment_form.is_valid():
                comment = comment_form.save(commit=False)
                comment.task = task
                if request.user.is_authenticated:
                    comment.author = request.user
                comment.save()
                task.status = "in_progress"
                task.save(update_fields=["status", "updated_at"])
                messages.success(request, "Задача возвращена на доработку")
                return redirect("todo:detail", pk=task.pk)
        elif action == "comment":
            comment_form = TaskCommentForm(request.POST)
            if comment_form.is_valid():
                comment = comment_form.save(commit=False)
                comment.task = task
                if request.user.is_authenticated:
                    comment.author = request.user
                comment.save()
                messages.success(request, "Комментарий добавлен")
                return redirect("todo:detail", pk=task.pk)
        elif action == "attach":
            attachment_form = TaskAttachmentForm(request.POST, request.FILES)
            if attachment_form.is_valid():
                files = request.FILES.getlist("files")
                if not files:
                    messages.error(request, "Файлы не выбраны")
                else:
                    for uploaded_file in files:
                        TaskAttachment.objects.create(
                            task=task,
                            uploaded_by=request.user if request.user.is_authenticated else None,
                            file=uploaded_file,
                        )
                    messages.success(request, "Файлы загружены")
                    return redirect("todo:detail", pk=task.pk)
        else:
            messages.error(request, "Неизвестное действие")

    comments = task.comments.select_related("author").order_by("-created_at")
    attachments = task.attachments.select_related("uploaded_by").order_by("-uploaded_at")
    context = {
        "task": task,
        "comments": comments,
        "attachments": attachments,
        "comment_form": comment_form,
        "attachment_form": attachment_form,
        "can_complete": can_complete,
        "can_edit": can_edit,
    }
    return render(request, "todo/task_detail.html", context)
