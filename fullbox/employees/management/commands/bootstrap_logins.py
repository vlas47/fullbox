from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from employees.models import Employee
from sku.models import Agency


ROLE_USERNAMES = {
    "admin": "admin",
    "director": "director",
    "accountant": "accountant",
    "head_manager": "head",
    "manager": "manager",
    "storekeeper": "storekeeper",
    "picker": "picker",
    "developer": "dev",
}


class Command(BaseCommand):
    help = "Create logins for employees and clients."

    def add_arguments(self, parser):
        parser.add_argument("--password", default="1")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        password = options["password"]
        dry_run = options["dry_run"]
        User = get_user_model()

        existing_users = list(User.objects.all())
        used = {user.username for user in existing_users}
        users_by_name = {user.username: user for user in existing_users}

        def reserve_username(base, current=None):
            if current and current in used:
                used.remove(current)
            candidate = base
            counter = 1
            while candidate in used:
                counter += 1
                candidate = f"{base}{counter}"
            used.add(candidate)
            return candidate

        def apply_user(user, username):
            changed = False
            if user.username != username:
                user.username = username
                changed = True
            if password:
                user.set_password(password)
                changed = True
            if changed and not dry_run:
                user.save()
            return changed

        created = 0
        updated = 0

        def sync_employees():
            nonlocal created, updated
            for employee in Employee.objects.select_related("user").order_by("role", "full_name"):
                base = ROLE_USERNAMES.get(employee.role) or (employee.role or "user")
                base_user = users_by_name.get(base)
                reuse_base = False
                if base_user:
                    linked_emp = Employee.objects.filter(user=base_user).exclude(pk=employee.pk).exists()
                    linked_agency = Agency.objects.filter(portal_user=base_user).exists()
                    reuse_base = not linked_emp and not linked_agency

                if reuse_base:
                    if employee.user_id != base_user.id:
                        if not dry_run:
                            employee.user = base_user
                            employee.save(update_fields=["user"])
                    if apply_user(base_user, base):
                        updated += 1
                    self.stdout.write(f"Employee: {employee.full_name} -> {base}")
                    continue

                current = employee.user.username if employee.user else None
                username = reserve_username(base, current)
                if employee.user:
                    if apply_user(employee.user, username):
                        updated += 1
                else:
                    if not dry_run:
                        user = User.objects.create_user(username=username, password=password)
                        employee.user = user
                        employee.save(update_fields=["user"])
                    created += 1
                self.stdout.write(f"Employee: {employee.full_name} -> {username}")

        def sync_clients():
            nonlocal created, updated
            for agency in Agency.objects.select_related("portal_user").order_by("id"):
                base = f"client{agency.id}"
                current = agency.portal_user.username if agency.portal_user else None
                username = reserve_username(base, current)
                if agency.portal_user:
                    if apply_user(agency.portal_user, username):
                        updated += 1
                else:
                    if not dry_run:
                        user = User.objects.create_user(username=username, password=password)
                        agency.portal_user = user
                        agency.save(update_fields=["portal_user"])
                    created += 1
                label = agency.agn_name or agency.fio_agn or f"Client {agency.id}"
                self.stdout.write(f"Client: {label} -> {username}")

        if dry_run:
            sync_employees()
            sync_clients()
            self.stdout.write("Dry-run mode: no changes applied.")
            return

        with transaction.atomic():
            sync_employees()
            sync_clients()

        self.stdout.write(
            f"Done. Created: {created}, updated: {updated}. Password: {password}"
        )
