"""JWT auth + password hashing + role guards."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from passlib.exc import UnknownHashError
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import User, UserRole, UserStatus

log = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/sign-in", auto_error=True)

# The bcrypt library, used directly as a version-robust fallback. passlib 1.7.4's
# bcrypt backend can break across bcrypt releases (it reads bcrypt.__about__,
# removed in bcrypt 4.1) and then throws on EVERY hash/verify — so a redeploy that
# pulled a newer bcrypt makes all logins fail with "not a valid hash" even though
# the stored $2b$ hashes are perfectly valid. Calling bcrypt directly sidesteps
# that entirely. bcrypt caps the password at 72 bytes; we mirror that.
try:
    import bcrypt as _bcrypt
except Exception:  # pragma: no cover - bcrypt is a hard dependency
    _bcrypt = None


def hash_password(password: str) -> str:
    try:
        return pwd_context.hash(password)
    except Exception:
        if _bcrypt is None:
            raise
        return _bcrypt.hashpw(password.encode("utf-8")[:72], _bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a password, tolerating a malformed stored hash OR a passlib/bcrypt
    version mismatch.

    passlib raises UnknownHashError when password_hash holds something that
    isn't a recognisable hash — an empty string, or plain text written by a
    direct database edit — and can raise ValueError when its bcrypt backend is
    incompatible with the installed bcrypt library. Uncaught, that surfaces as a
    500 (or a mass login failure) rather than the credentials being wrong.

    First try passlib; if it throws, fall back to verifying a real $2 bcrypt hash
    directly with the bcrypt library, which is robust across bcrypt versions.
    Only a hash that BOTH paths reject is treated as unverifiable (failed login).
    """
    if not hashed or not isinstance(hashed, str):
        return False
    try:
        return pwd_context.verify(plain, hashed)
    except (UnknownHashError, ValueError):
        pass
    except Exception:
        pass
    if _bcrypt is not None and hashed.startswith("$2"):
        try:
            return _bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("utf-8"))
        except (ValueError, TypeError):
            pass
    log.warning("password_hash is not a valid hash; treating as failed login")
    return False


def create_access_token(user_id: int) -> tuple[str, datetime]:
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expiry_minutes)
    payload = {"sub": str(user_id), "exp": expires}
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires


def _credentials_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def current_user(token: str = Depends(oauth2), db: Session = Depends(get_db)) -> User:
    """Any authenticated, non-rejected/deactivated user. PENDING users are allowed
    through so they can complete self-serve onboarding (verify + add card); product
    access is gated separately by require_active."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        user_id = int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise _credentials_error()
    user = db.get(User, user_id)
    if user is None:
        raise _credentials_error()
    if user.status in (UserStatus.REJECTED.value, UserStatus.DEACTIVATED.value):
        raise HTTPException(status_code=403, detail=f"Account is {user.status}")
    return user


# Stripe states that mean the customer is in their trial or actively paying.
ACTIVE_SUBSCRIPTION_STATES = {"trialing", "active"}


def has_product_access(user: User) -> bool:
    """True if the user may see the product. Admins and admin-approved users get
    in outright (they bypass billing); everyone else needs a live subscription —
    i.e. they've added a card and are trialing or paying."""
    if user.role == UserRole.ADMIN.value:
        return True
    if user.status == UserStatus.APPROVED.value:
        return True
    return (user.subscription_status or "") in ACTIVE_SUBSCRIPTION_STATES


def require_active(user: User = Depends(current_user)) -> User:
    """Gate for the actual product. Returns 402 (Payment Required) when the user
    is authenticated but hasn't finished onboarding, so the frontend can route
    them to the paywall rather than treating it as a hard forbidden."""
    if not has_product_access(user):
        raise HTTPException(status_code=402, detail="onboarding_incomplete")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != UserRole.ADMIN.value:
        raise HTTPException(status_code=403, detail="Admin only")
    if user.status != UserStatus.APPROVED.value:
        raise HTTPException(status_code=403, detail=f"Account is {user.status}")
    return user


def _repair_plaintext_hashes(db: Session) -> None:
    """Re-hash any password_hash that isn't a real bcrypt hash.

    A direct database edit can leave plain text in password_hash. That user can
    then never log in: passlib can't identify the value, and every attempt is a
    failed login. Rather than locking the account out permanently, we treat the
    stored text as the intended password and hash it properly.

    This only ever moves a row from plain text (bad) to bcrypt (good) — it never
    weakens an existing hash. A real bcrypt hash starts with $2 and is 60 chars,
    so anything else is broken by definition.
    """
    try:
        rows = db.query(User).all()
    except Exception:
        return  # table may not exist yet on a first boot
    fixed = 0
    for u in rows:
        h = u.password_hash or ""
        if isinstance(h, str) and h.startswith("$2") and len(h) == 60:
            continue  # already a valid bcrypt hash
        if not h:
            continue  # nothing to recover; leave it for a password reset
        u.password_hash = hash_password(h)
        fixed += 1
    if fixed:
        db.commit()
        log.warning("repaired %d password hash(es) that were not bcrypt", fixed)


def ensure_seed_admin(db: Session) -> None:
    """Create the seed admin user on first boot if no admin exists."""
    _repair_plaintext_hashes(db)
    has_admin = db.query(User).filter(User.role == UserRole.ADMIN.value).first()
    if has_admin:
        return
    admin = User(
        email=settings.seed_admin_email,
        password_hash=hash_password(settings.seed_admin_password),
        full_name="Seed Admin",
        role=UserRole.ADMIN.value,
        status=UserStatus.APPROVED.value,
    )
    db.add(admin)
    db.commit()
