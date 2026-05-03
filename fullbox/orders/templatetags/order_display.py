from django import template

from fullbox.order_numbers import format_order_number


register = template.Library()


@register.filter
def display_order_number(order_id, order_type):
    return format_order_number(order_type, order_id)
