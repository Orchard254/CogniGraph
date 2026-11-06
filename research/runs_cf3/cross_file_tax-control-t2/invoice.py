from pricing import apply_tax


def total(line_items):
    return apply_tax(sum(line_items))
