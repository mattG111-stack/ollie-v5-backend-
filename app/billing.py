"""Stripe-backed billing metrics for the admin dashboard.

Reads real numbers from Stripe when STRIPE_SECRET_KEY is set — active
subscribers, monthly recurring revenue, and this-month income. Returns a
`connected=False` shell when no key is configured, so the dashboard renders a
"Connect Stripe" state rather than fake figures.

Nothing here creates charges; it only reads. Subscriptions/checkout are created
elsewhere (or in the Stripe dashboard) — this just reports them.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import settings


@dataclass
class PayingCustomer:
    email: str | None = None
    name: str | None = None
    amount_monthly: float = 0.0
    currency: str = "nzd"
    status: str = "active"
    since: str | None = None
    customer_id: str = ""


@dataclass
class BillingMetrics:
    connected: bool = False
    active_subscribers: int = 0
    mrr: float = 0.0                      # monthly recurring revenue, NZD
    income_this_month: float = 0.0        # actually-collected this calendar month
    currency: str = "nzd"
    error: str | None = None


_INTERVAL_TO_MONTHLY = {"day": 30.0, "week": 4.345, "month": 1.0, "year": 1 / 12.0}


def _monthly_amount(item) -> float:
    """A subscription item's contribution to MRR, normalised to per-month."""
    price = item.get("price") or {}
    recurring = price.get("recurring") or {}
    unit = (price.get("unit_amount") or 0) / 100.0
    qty = item.get("quantity") or 1
    per = _INTERVAL_TO_MONTHLY.get(recurring.get("interval"), 1.0)
    count = recurring.get("interval_count") or 1
    return unit * qty * per / count


def billing_metrics() -> BillingMetrics:
    key = settings.stripe_secret_key
    if not key:
        return BillingMetrics(connected=False)

    try:
        import stripe
    except ImportError:
        return BillingMetrics(connected=False, error="stripe library not installed")

    stripe.api_key = key
    m = BillingMetrics(connected=True)
    try:
        # Active subscribers + MRR — paginate through active subscriptions.
        subs = stripe.Subscription.list(status="active", limit=100, expand=["data.items.data.price"])
        for s in subs.auto_paging_iter():
            m.active_subscribers += 1
            for it in (s.get("items") or {}).get("data", []):
                m.mrr += _monthly_amount(it)

        # This calendar month's collected income — sum paid invoices.
        import calendar
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        month_start = int(datetime(now.year, now.month, 1, tzinfo=timezone.utc).timestamp())
        inv = stripe.Invoice.list(status="paid", created={"gte": month_start}, limit=100)
        for i in inv.auto_paging_iter():
            m.income_this_month += (i.get("amount_paid") or 0) / 100.0
            m.currency = i.get("currency") or m.currency
    except Exception as e:            # any Stripe/API error → connected but degraded
        m.error = str(e)[:200]
    return m


def paying_users() -> list[PayingCustomer]:
    """Every active Stripe subscriber, for the admin 'paying users' view.
    Empty list when Stripe isn't configured."""
    key = settings.stripe_secret_key
    if not key:
        return []
    try:
        import stripe
    except ImportError:
        return []
    from datetime import datetime, timezone
    stripe.api_key = key
    out: list[PayingCustomer] = []
    try:
        subs = stripe.Subscription.list(
            status="active", limit=100,
            expand=["data.items.data.price", "data.customer"])
        for s in subs.auto_paging_iter():
            cust = s.get("customer")
            cust = cust if isinstance(cust, dict) else {}
            monthly = sum(_monthly_amount(it) for it in (s.get("items") or {}).get("data", []))
            start = s.get("start_date")
            out.append(PayingCustomer(
                email=cust.get("email"), name=cust.get("name"),
                amount_monthly=round(monthly, 2), currency=(s.get("currency") or "nzd"),
                status=s.get("status") or "active",
                since=datetime.fromtimestamp(start, timezone.utc).isoformat() if start else None,
                customer_id=cust.get("id") or "",
            ))
    except Exception:
        pass
    return out
