from pricing import apply_tax\n\ndef total(line_items):\n    return apply_tax(sum(line_items))
