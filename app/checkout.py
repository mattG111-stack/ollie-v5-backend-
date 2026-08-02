"""Stripe Checkout for the 7-day trial, plus the webhook that keeps each user's
subscription state in sync.

Flow: a verified user hits /api/billing/checkout → we create (or reuse) their
Stripe customer and open a subscription Checkout Session with a trial. Stripe
collects the card, starts the trial, and charges automatically when it ends.
Stripe then calls our webhook on every subscription change; we cache the status
onto the user so access can be gated without calling Stripe on each request.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .config import settings
from .models import User


class CheckoutError(Exception):
    """Configuration or Stripe problem that should surface to the caller."""


def _stripe():
    if not settings.stripe_secret_key:
        raise CheckoutError("Billing is not configured (missing STRIPE_SECRET_KEY).")
    if not settings.stripe_price_id:
        raise CheckoutError("Billing is not configured (missing STRIPE_PRICE_ID).")
    try:
        import stripe
    except ImportError:
        raise CheckoutError("The stripe library is not installed on the server.")
    stripe.api_key = settings.stripe_secret_key
    return stripe


def ensure_customer(db: Session, user: User) -> str:
    """Return the user's Stripe customer id, creating the customer if needed."""
    stripe = _stripe()
    if user.stripe_customer_id:
        return user.stripe_customer_id
    cust = stripe.Customer.create(
        email=user.email,
        name=user.full_name or None,
        phone=user.phone or None,
        metadata={"app_user_id": str(user.id)},
    )
    user.stripe_customer_id = cust["id"]
    db.commit()
    return cust["id"]


def create_checkout_session(db: Session, user: User) -> str:
    """Create a subscription Checkout Session with a trial and return its URL."""
    stripe = _stripe()
    customer_id = ensure_customer(db, user)
    base = settings.app_base_url.rstrip("/")
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": settings.stripe_price_id, "quantity": 1}],
        subscription_data={
            "trial_period_days": settings.trial_days,
            "metadata": {"app_user_id": str(user.id)},
        },
        # Require a card up front even though the trial is free, so the first
        # charge goes through automatically when the trial ends.
        payment_method_collection="always",
        success_url=f"{base}/onboarding?checkout=success",
        cancel_url=f"{base}/onboarding?checkout=cancel",
        client_reference_id=str(user.id),
    )
    return session["url"]


def _ts(v) -> datetime | None:
    return datetime.fromtimestamp(v, timezone.utc) if v else None


def _apply_subscription(db: Session, sub: dict) -> None:
    """Update the matching user from a Stripe subscription object."""
    customer_id = sub.get("customer")
    app_user_id = (sub.get("metadata") or {}).get("app_user_id")
    user = None
    if app_user_id:
        try:
            user = db.get(User, int(app_user_id))
        except (TypeError, ValueError):
            user = None
    if user is None and customer_id:
        user = db.query(User).filter(User.stripe_customer_id == customer_id).first()
    if user is None:
        return
    user.subscription_status = sub.get("status")
    user.trial_ends_at = _ts(sub.get("trial_end"))
    user.current_period_end = _ts(sub.get("current_period_end"))
    if customer_id and not user.stripe_customer_id:
        user.stripe_customer_id = customer_id
    db.commit()


def handle_webhook(db: Session, payload: bytes, sig_header: str | None) -> str:
    """Verify and process a Stripe webhook. Returns the event type handled."""
    stripe = _stripe()
    secret = settings.stripe_webhook_secret
    if secret:
        event = stripe.Webhook.construct_event(payload, sig_header, secret)
    else:
        # No signing secret configured — parse without verification (dev only).
        import json
        event = json.loads(payload)

    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}

    if etype.startswith("customer.subscription."):
        _apply_subscription(db, obj)
    elif etype == "checkout.session.completed":
        sub_id = obj.get("subscription")
        if sub_id:
            _apply_subscription(db, stripe.Subscription.retrieve(sub_id))
    return etype
