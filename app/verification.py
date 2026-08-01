"""Issue and check the 6-digit codes used to verify email and phone.

The newest unconsumed, unexpired code for a (user, channel) is the valid one.
Codes are single-use and capped at a few attempts to blunt guessing.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from .config import settings
from .models import VerificationCode

MAX_ATTEMPTS = 6


def _now() -> datetime:
    return datetime.now(timezone.utc)


def issue_code(db: Session, user_id: int, channel: str) -> str:
    """Create a fresh code, invalidating any prior live codes on that channel."""
    (db.query(VerificationCode)
       .filter(VerificationCode.user_id == user_id,
               VerificationCode.channel == channel,
               VerificationCode.consumed_at.is_(None))
       .update({VerificationCode.consumed_at: _now()}))
    code = f"{secrets.randbelow(1_000_000):06d}"
    row = VerificationCode(
        user_id=user_id, channel=channel, code=code,
        expires_at=_now() + timedelta(minutes=settings.verify_code_ttl_minutes),
    )
    db.add(row)
    db.commit()
    return code


def check_code(db: Session, user_id: int, channel: str, code: str) -> tuple[bool, str]:
    """Verify a submitted code. Returns (ok, reason). Consumes it on success."""
    row = (db.query(VerificationCode)
             .filter(VerificationCode.user_id == user_id,
                     VerificationCode.channel == channel,
                     VerificationCode.consumed_at.is_(None))
             .order_by(VerificationCode.id.desc())
             .first())
    if row is None:
        return False, "no_code"
    if row.expires_at < _now():
        return False, "expired"
    if row.attempts >= MAX_ATTEMPTS:
        return False, "too_many_attempts"
    row.attempts += 1
    if secrets.compare_digest(row.code, (code or "").strip()):
        row.consumed_at = _now()
        db.commit()
        return True, "ok"
    db.commit()
    return False, "mismatch"
