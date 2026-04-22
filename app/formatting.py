from datetime import date, datetime
from decimal import Decimal


def format_money(amount, currency: str) -> str:
    sym = "₽"
    if isinstance(amount, Decimal):
        v = amount.quantize(Decimal("0.01"))
    else:
        try:
            v = Decimal(str(amount)).quantize(Decimal("0.01"))
        except Exception:
            v = Decimal("0.00")
    s = f"{v:,.2f}".replace(",", " ")
    return f"{sym}{s}"


def format_date_user(d: date | datetime | None, fmt: str) -> str:
    if d is None:
        return "—"
    if isinstance(d, datetime):
        d = d.date()
    if fmt == "MM/DD/YYYY":
        return d.strftime("%m/%d/%Y")
    return d.strftime("%d.%m.%Y")
