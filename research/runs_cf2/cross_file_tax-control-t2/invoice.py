from pricing import apply_tax


def total(line_items):
    subtotal = sum(line_items)
    return apply_tax(subtotal)
