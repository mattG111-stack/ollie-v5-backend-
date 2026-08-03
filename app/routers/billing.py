"""Self-serve billing endpoints: start the trial checkout + receive Stripe webhooks."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..checkout import CheckoutError, create_checkout_session, handle_webhook
from ..db import get_db
from ..models import User
from ..security import current_user, has_product_access

router = APIRouter(prefix="/api/billing", tags=["billing"])


class CheckoutOut(BaseModel):
    url: str


@router.post("/checkout", response_model=CheckoutOut)
def start_checkout(user: User = Depends(current_user), db: Session = Depends(get_db)) -> CheckoutOut:
    """Open a Stripe Checkout Session for the trial. Requires the user to have
    verified email + phone first (the last gate before taking a card)."""
    if has_product_access(user):
        raise HTTPException(status_code=409, detail="already_subscribed")
    if user.email_verified_at is None:
        raise HTTPException(status_code=403, detail="verify_email_first")
    if user.phone_verified_at is None:
        raise HTTPException(status_code=403, detail="verify_phone_first")
    try:
        url = create_checkout_session(db, user)
    except CheckoutError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return CheckoutOut(url=url)


@router.post("/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    """Stripe calls this on every subscription change. Keeps subscription_status,
    trial_ends_at and current_period_end in sync on the user record."""
    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    try:
        etype = handle_webhook(db, payload, sig)
    except CheckoutError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:                        # bad signature / malformed → 400
        raise HTTPException(status_code=400, detail=str(e)[:200])
    return {"received": True, "type": etype}
