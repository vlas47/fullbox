from audit.models import OrderAuditEntry
from orders.views import _processing_receiving_items, _processing_card_sets, _latest_payload_from_entries

qs = list(OrderAuditEntry.objects.filter(order_id="7", order_type="processing").order_by("created_at"))
print("entries", len(qs))
if qs:
    last = qs[-1]
    print("last_id", last.id, "action", last.action, "created", last.created_at)
    print("last_payload_keys", list((last.payload or {}).keys())[:20])
    print("last_payload_len", len(last.payload or {}))
payload = _latest_payload_from_entries(qs) if qs else {}
print("latest_payload_keys", list(payload.keys())[:20])
print("status", payload.get("status"), payload.get("status_label"))

def _count(value):
    return len(value) if isinstance(value, list) else 0

print("stock_rows", _count(payload.get("stock_rows")),
      "size_rows", _count(payload.get("size_rows")),
      "cards", _count(payload.get("cards")),
      "processing_results", _count(payload.get("processing_results")),
      "processed_cards", _count(payload.get("processed_cards")),
      "placed_cards", _count(payload.get("placed_cards")))

processed, placed = _processing_card_sets(payload)
ready = processed - placed if processed else set()
items = _processing_receiving_items(payload, qs[-1].agency_id if qs else None, ready if ready else None)
print("ready_cards", len(ready), "items", len(items))
print("items_sample", items[:3])
