import copy
import re

from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.utils import timezone
from django.views.generic import TemplateView

from audit.models import OrderAuditEntry, log_order_action, log_stock_move
from employees.access import RoleRequiredMixin, get_employee_for_user, get_request_role, resolve_cabinet_url

ALLOWED_ZONES = {"PR", "OTG", "MR", "OS"}
ALLOWED_ROLES = (
    "reachtruck_driver",
    "manager",
    "storekeeper",
    "processing_head",
    "head_manager",
    "director",
    "admin",
)
CREATE_ROLES = (
    "manager",
    "storekeeper",
    "processing_head",
    "head_manager",
    "director",
    "admin",
)


def _parse_int_value(raw) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return 0


def _normalize_zone_code(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    if re.search(r"^pr$", text, re.IGNORECASE) or re.search(
        r"зона приемки|поле приемки", text, re.IGNORECASE
    ):
        return "PR"
    if re.search(r"^otg?$", text, re.IGNORECASE) or re.search(
        r"зона отгрузки|отгрузк", text, re.IGNORECASE
    ):
        return "OTG"
    if re.search(r"^mr$", text, re.IGNORECASE) or re.search(
        r"между ряд", text, re.IGNORECASE
    ):
        return "MR"
    if re.search(r"^os$", text, re.IGNORECASE) or re.search(
        r"основн|стеллаж|ряд|полк|секци|ярус|ячейк", text, re.IGNORECASE
    ):
        return "OS"
    return text.upper()


def _location_parts(location_value, pallet=None) -> dict:
    pallet = pallet or {}
    zone = ""
    row = 0
    section = 0
    tier = 0
    cell = 0
    if isinstance(location_value, dict):
        zone = _normalize_zone_code(location_value.get("zone") or "")
        row = _parse_int_value(location_value.get("row") or pallet.get("row"))
        section = _parse_int_value(location_value.get("section"))
        tier = _parse_int_value(location_value.get("tier"))
        cell = _parse_int_value(location_value.get("cell"))
    elif isinstance(location_value, str):
        zone = _normalize_zone_code(location_value)
    if not zone:
        zone = _normalize_zone_code(pallet.get("zone") or "")
    if zone == "OS":
        row = row or _parse_int_value(pallet.get("row"))
        section = section or _parse_int_value(pallet.get("section"))
        tier = tier or _parse_int_value(pallet.get("tier"))
        cell = cell or _parse_int_value(pallet.get("cell"))
    if zone == "MR" and not row:
        row = _parse_int_value(pallet.get("row"))
    return {
        "zone": zone or "PR",
        "row": row,
        "section": section,
        "tier": tier,
        "cell": cell,
    }


def _build_location(zone: str, row: int, section: int, tier: int, cell: int) -> dict:
    zone = _normalize_zone_code(zone) or "PR"
    return {
        "zone": zone,
        "row": row if zone in {"MR", "OS"} else "",
        "section": section if zone == "OS" else "",
        "tier": tier if zone == "OS" else "",
        "cell": cell if zone == "OS" else "",
    }


def _location_label(location: dict | None) -> str:
    location = location or {}
    zone = _normalize_zone_code(location.get("zone") or "") or "PR"
    row = _parse_int_value(location.get("row"))
    section = _parse_int_value(location.get("section"))
    tier = _parse_int_value(location.get("tier"))
    cell = _parse_int_value(location.get("cell"))
    if zone == "PR":
        return "PR · Зона приемки"
    if zone == "OTG":
        return "OTG · Зона отгрузки"
    if zone == "MR":
        return f"MR · Между рядами · Ряд {row}" if row else "MR · Между рядами"
    if zone == "OS":
        if row and section and tier and cell:
            return f"OS · Ряд {row} · Секция {section} · Ярус {tier} · Ячейка {cell}"
        if row:
            return f"OS · Ряд {row}"
        return "OS · Основной склад"
    return zone or "PR"


def _latest_closed_placement_entries():
    entries = OrderAuditEntry.objects.filter(order_type="receiving").order_by("-created_at")
    latest_by_order = {}
    blocked_orders = set()
    for entry in entries:
        if entry.order_id in latest_by_order or entry.order_id in blocked_orders:
            continue
        payload = entry.payload or {}
        if payload.get("act") != "placement":
            continue
        state = (payload.get("act_state") or "closed").lower()
        if state != "closed":
            blocked_orders.add(entry.order_id)
            continue
        latest_by_order[entry.order_id] = entry
    return list(latest_by_order.values())


def _find_pallet_by_code(code: str):
    target = (code or "").strip()
    if not target:
        return None
    for entry in _latest_closed_placement_entries():
        payload = entry.payload or {}
        pallets = payload.get("act_pallets") or []
        for idx, pallet in enumerate(pallets):
            if not isinstance(pallet, dict):
                continue
            pallet_code = (pallet.get("code") or "").strip()
            if pallet_code and pallet_code == target:
                location = _location_parts(pallet.get("location"), pallet)
                return entry, idx, pallet, location
    return None


def _occupied_os_cells(exclude_code: str | None = None) -> set[tuple[int, int, int, int]]:
    occupied = set()
    exclude_code = (exclude_code or "").strip()
    for entry in _latest_closed_placement_entries():
        payload = entry.payload or {}
        pallets = payload.get("act_pallets") or []
        for pallet in pallets:
            if not isinstance(pallet, dict):
                continue
            pallet_code = (pallet.get("code") or "").strip()
            if exclude_code and pallet_code == exclude_code:
                continue
            parts = _location_parts(pallet.get("location"), pallet)
            if parts.get("zone") != "OS":
                continue
            if all(parts.get(key) for key in ("row", "section", "tier", "cell")):
                occupied.add(
                    (parts["row"], parts["section"], parts["tier"], parts["cell"])
                )
    return occupied


def _next_move_number() -> str:
    order_ids = (
        OrderAuditEntry.objects.filter(order_type="stock_move")
        .values_list("order_id", flat=True)
        .distinct()
    )
    max_number = 0
    for order_id in order_ids:
        candidate = str(order_id).strip()
        if not candidate.isdigit():
            continue
        number = int(candidate)
        if number > max_number:
            max_number = number
    next_number = max_number + 1
    while OrderAuditEntry.objects.filter(order_type="stock_move", order_id=str(next_number)).exists():
        next_number += 1
    return str(next_number)


def _latest_move_entry(order_id: str):
    if not order_id:
        return None
    return (
        OrderAuditEntry.objects.filter(order_type="stock_move", order_id=str(order_id))
        .order_by("-created_at")
        .first()
    )


def _collect_moves(employee_id: int | None, driver_view: bool) -> tuple[list[dict], list[dict]]:
    entries = (
        OrderAuditEntry.objects.filter(order_type="stock_move")
        .select_related("user", "agency")
        .order_by("order_id", "created_at")
    )
    latest_by_order = {}
    created_at_by_order = {}
    for entry in entries:
        created_at_by_order.setdefault(entry.order_id, entry.created_at)
        latest_by_order[entry.order_id] = entry

    moves = []
    for order_id, entry in latest_by_order.items():
        payload = entry.payload or {}
        status = (payload.get("status") or payload.get("submit_action") or "").strip().lower()
        status_label = (payload.get("status_label") or "").strip() or status or "-"
        pallet_code = (payload.get("pallet_code") or "").strip()
        from_location = payload.get("from_location") or {}
        to_location = payload.get("to_location") or {}
        assigned_to_id = payload.get("assigned_to_id")
        assigned_to_name = payload.get("assigned_to_name") or "-"
        if assigned_to_id:
            try:
                assigned_to_id = int(assigned_to_id)
            except (TypeError, ValueError):
                assigned_to_id = None
        move = {
            "order_id": order_id,
            "created_at": created_at_by_order.get(order_id) or entry.created_at,
            "updated_at": entry.created_at,
            "status": status or "-",
            "status_label": status_label,
            "pallet_code": pallet_code or "-",
            "from_location": from_location,
            "to_location": to_location,
            "from_label": _location_label(from_location),
            "to_label": _location_label(to_location),
            "assigned_to_id": assigned_to_id,
            "assigned_to_name": assigned_to_name,
            "requested_by_name": payload.get("requested_by_name") or "-",
            "receiving_order_id": payload.get("receiving_order_id") or "-",
        }
        if driver_view:
            if status == "done":
                moves.append(move)
                continue
            if assigned_to_id and employee_id and assigned_to_id != employee_id:
                continue
        moves.append(move)

    moves.sort(key=lambda item: item["updated_at"], reverse=True)
    active = [move for move in moves if move["status"] != "done"]
    done = [move for move in moves if move["status"] == "done"][:10]
    for move in active:
        move["can_take"] = move["status"] == "created" and not move["assigned_to_id"]
        move["can_complete"] = (
            move["status"] == "in_progress" and move["assigned_to_id"] and move["assigned_to_id"] == employee_id
        )
    for move in done:
        move["can_take"] = False
        move["can_complete"] = False
    return active, done


class ReachtruckDashboardView(RoleRequiredMixin, TemplateView):
    template_name = "reachtruck/dashboard.html"
    allowed_roles = ALLOWED_ROLES

    def _render_error(self, message: str):
        ctx = self.get_context_data(error=message)
        return self.render_to_response(ctx)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        role = get_request_role(self.request)
        employee = get_employee_for_user(self.request.user)
        employee_id = employee.id if employee else None
        ctx["role"] = role
        ctx["is_driver"] = role == "reachtruck_driver"
        ctx["can_create"] = role in CREATE_ROLES
        ctx["cabinet_url"] = resolve_cabinet_url(role)
        ctx["error"] = kwargs.get("error")
        if self.request.GET.get("ok") == "1":
            order_id = (self.request.GET.get("order") or "").strip()
            ctx["ok_message"] = (
                f"Задание №{order_id} создано." if order_id else "Задание создано."
            )
        active_moves, done_moves = _collect_moves(employee_id, ctx["is_driver"])
        ctx["moves_active"] = active_moves
        ctx["moves_done"] = done_moves
        return ctx

    def post(self, request, *args, **kwargs):
        action = (request.POST.get("action") or "").strip()
        role = get_request_role(request)
        employee = get_employee_for_user(request.user)
        employee_id = employee.id if employee else None
        employee_name = employee.full_name if employee else request.user.get_full_name() or request.user.username
        if action == "create_move":
            if role not in CREATE_ROLES:
                return HttpResponseForbidden("Доступ запрещен")
            pallet_code = (request.POST.get("pallet_code") or "").strip()
            zone = _normalize_zone_code(request.POST.get("to_zone") or "")
            row = _parse_int_value(request.POST.get("to_row"))
            section = _parse_int_value(request.POST.get("to_section"))
            tier = _parse_int_value(request.POST.get("to_tier"))
            cell = _parse_int_value(request.POST.get("to_cell"))
            if not pallet_code:
                return self._render_error("Укажите код паллеты.")
            if zone not in ALLOWED_ZONES:
                return self._render_error("Выберите зону хранения.")
            if zone == "MR" and not row:
                return self._render_error("Для зоны MR укажите ряд.")
            if zone == "OS" and not (row and section and tier and cell):
                return self._render_error("Для зоны OS укажите ряд, секцию, ярус и ячейку.")
            if zone == "OS":
                occupied = _occupied_os_cells(exclude_code=pallet_code)
                if (row, section, tier, cell) in occupied:
                    return self._render_error("Указанная ячейка уже занята.")
            found = _find_pallet_by_code(pallet_code)
            if not found:
                return self._render_error("Паллета не найдена в размещении.")
            placement_entry, _, pallet, from_parts = found
            from_location = _build_location(
                from_parts.get("zone"),
                from_parts.get("row"),
                from_parts.get("section"),
                from_parts.get("tier"),
                from_parts.get("cell"),
            )
            to_location = _build_location(zone, row, section, tier, cell)
            move_id = _next_move_number()
            payload = {
                "status": "created",
                "status_label": "Ожидает перевозки",
                "pallet_code": pallet_code,
                "from_location": from_location,
                "to_location": to_location,
                "from_label": _location_label(from_location),
                "to_label": _location_label(to_location),
                "receiving_order_id": placement_entry.order_id,
                "requested_by_name": employee_name,
                "requested_by_role": role,
            }
            log_order_action(
                "create",
                order_id=move_id,
                order_type="stock_move",
                user=request.user if request.user.is_authenticated else None,
                agency=placement_entry.agency,
                description=f"Задание на перемещение паллеты {pallet_code}",
                payload=payload,
            )
            log_stock_move(
                "create",
                user=request.user if request.user.is_authenticated else None,
                agency=placement_entry.agency,
                description=f"Создано задание на перемещение паллеты {pallet_code}",
                snapshot={
                    "move_id": move_id,
                    "pallet_code": pallet_code,
                    "from_location": from_location,
                    "to_location": to_location,
                    "from_label": _location_label(from_location),
                    "to_label": _location_label(to_location),
                    "receiving_order_id": placement_entry.order_id,
                    "status": "created",
                },
            )
            return redirect(f"/reachtruck/?ok=1&order={move_id}")
        if action == "take_move":
            if role != "reachtruck_driver":
                return HttpResponseForbidden("Доступ запрещен")
            if not employee_id:
                return self._render_error("Профиль сотрудника не найден.")
            order_id = (request.POST.get("order_id") or "").strip()
            entry = _latest_move_entry(order_id)
            if not entry:
                return self._render_error("Задание не найдено.")
            payload = dict(entry.payload or {})
            status = (payload.get("status") or "").strip().lower()
            assigned_to_id = payload.get("assigned_to_id")
            if assigned_to_id:
                try:
                    assigned_to_id = int(assigned_to_id)
                except (TypeError, ValueError):
                    assigned_to_id = None
            if status == "done":
                return self._render_error("Задание уже выполнено.")
            if status == "in_progress" and assigned_to_id and assigned_to_id != employee_id:
                return self._render_error("Задание уже взято другим водителем.")
            payload["status"] = "in_progress"
            payload["status_label"] = "В работе"
            payload["assigned_to_id"] = employee_id
            payload["assigned_to_name"] = employee_name
            payload["taken_at"] = timezone.localtime().isoformat()
            log_order_action(
                "status",
                order_id=order_id,
                order_type="stock_move",
                user=request.user if request.user.is_authenticated else None,
                agency=entry.agency,
                description=f"Задание {order_id} взято в работу",
                payload=payload,
            )
            log_stock_move(
                "update",
                user=request.user if request.user.is_authenticated else None,
                agency=entry.agency,
                description=f"Задание {order_id} взято в работу",
                snapshot={
                    "move_id": order_id,
                    "pallet_code": payload.get("pallet_code"),
                    "from_location": payload.get("from_location"),
                    "to_location": payload.get("to_location"),
                    "from_label": payload.get("from_label"),
                    "to_label": payload.get("to_label"),
                    "receiving_order_id": payload.get("receiving_order_id"),
                    "status": "in_progress",
                    "assigned_to": employee_name,
                },
            )
            return redirect("/reachtruck/")
        if action == "complete_move":
            if role != "reachtruck_driver":
                return HttpResponseForbidden("Доступ запрещен")
            if not employee_id:
                return self._render_error("Профиль сотрудника не найден.")
            order_id = (request.POST.get("order_id") or "").strip()
            entry = _latest_move_entry(order_id)
            if not entry:
                return self._render_error("Задание не найдено.")
            payload = dict(entry.payload or {})
            status = (payload.get("status") or "").strip().lower()
            assigned_to_id = payload.get("assigned_to_id")
            if assigned_to_id:
                try:
                    assigned_to_id = int(assigned_to_id)
                except (TypeError, ValueError):
                    assigned_to_id = None
            if status != "in_progress":
                return self._render_error("Задание еще не взято в работу.")
            if assigned_to_id and employee_id and assigned_to_id != employee_id:
                return self._render_error("Задание назначено другому водителю.")
            pallet_code = (payload.get("pallet_code") or "").strip()
            if not pallet_code:
                return self._render_error("Не найден код паллеты в задании.")
            found = _find_pallet_by_code(pallet_code)
            if not found:
                return self._render_error("Паллета не найдена в размещении.")
            placement_entry, _, _, _ = found
            placement_payload = copy.deepcopy(placement_entry.payload or {})
            pallets = placement_payload.get("act_pallets") or []
            to_location = payload.get("to_location") or {}
            updated = False
            for pallet in pallets:
                if not isinstance(pallet, dict):
                    continue
                if (pallet.get("code") or "").strip() == pallet_code:
                    pallet["location"] = _build_location(
                        to_location.get("zone"),
                        _parse_int_value(to_location.get("row")),
                        _parse_int_value(to_location.get("section")),
                        _parse_int_value(to_location.get("tier")),
                        _parse_int_value(to_location.get("cell")),
                    )
                    updated = True
                    break
            if not updated:
                return self._render_error("Не удалось обновить локацию паллеты.")
            placement_payload["act"] = "placement"
            placement_payload["act_state"] = "closed"
            log_order_action(
                "status",
                order_id=placement_entry.order_id,
                order_type="receiving",
                user=request.user if request.user.is_authenticated else None,
                agency=placement_entry.agency,
                description=f"Перемещение паллеты {pallet_code}",
                payload=placement_payload,
            )
            payload["status"] = "done"
            payload["status_label"] = "Перемещено"
            payload["completed_at"] = timezone.localtime().isoformat()
            payload["completed_by_name"] = employee_name
            log_order_action(
                "status",
                order_id=order_id,
                order_type="stock_move",
                user=request.user if request.user.is_authenticated else None,
                agency=entry.agency,
                description=f"Задание {order_id} выполнено",
                payload=payload,
            )
            log_stock_move(
                "update",
                user=request.user if request.user.is_authenticated else None,
                agency=entry.agency,
                description=f"Задание {order_id} выполнено",
                snapshot={
                    "move_id": order_id,
                    "pallet_code": pallet_code,
                    "from_location": payload.get("from_location"),
                    "to_location": payload.get("to_location"),
                    "from_label": payload.get("from_label"),
                    "to_label": payload.get("to_label"),
                    "receiving_order_id": placement_entry.order_id,
                    "status": "done",
                    "completed_by": employee_name,
                },
            )
            return redirect("/reachtruck/")
        return self.get(request, *args, **kwargs)
