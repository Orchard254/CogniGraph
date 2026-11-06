from config import TAX_RATE

def apply_tax(amount):
    return amount * (1 + TAX_RATE)
