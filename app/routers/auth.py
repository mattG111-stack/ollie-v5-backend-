"""Auth + self-serve onboarding + admin user-management endpoints."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import User, UserRole, UserStatus
from ..notify import send_email, send_sms, verification_email_html
from ..security import (
    create_access_token,
    current_user,
    has_product_access,
    hash_password,
    require_admin,
    verify_password,
)
from ..verification import check_code, issue_code

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _next_step(u: User) -> str:
    """Where this user is in the self-serve onboarding funnel."""
    if has_product_access(u):
        return "done"
    if u.email_verified_at is None:
        return "verify_email"
    if u.phone_verified_at is None:
        return "verify_phone"
    return "add_card"


def _send_email_code(db: Session, u: User) -> None:
    code = issue_code(db, u.id, "email")
    send_email(u.email, "Your Ollie verification code",
               verification_email_html(code, settings.verify_code_ttl_minutes),
               text=f"Your Ollie verification code is {code} (valid {settings.verify_code_ttl_minutes} min).")


def _send_phone_code(db: Session, u: User) -> None:
    code = issue_code(db, u.id, "phone")
    send_sms(u.phone or "", f"Your Ollie code is {code}")


class SignUpIn(BaseModel):
    email: EmailStr
    password: str
    full_name: str | None = None
    company: str | None = None
    phone: str | None = None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime


class UserOut(BaseModel):
    id: int
    email: str
    full_name: str | None
    company: str | None
    phone: str | None
    role: str
    status: str

    class Config:
        from_attributes = True


class MeOut(UserOut):
    """UserOut plus onboarding state, so the app can route to the right step."""
    email_verified: bool
    phone_verified: bool
    subscription_status: str | None
    trial_ends_at: datetime | None
    has_access: bool
    next_step: str
    llm_key_managed: bool = False


def _me_out(u: User) -> MeOut:
    return MeOut(
        id=u.id, email=u.email, full_name=u.full_name, company=u.company, phone=u.phone,
        role=u.role, status=u.status,
        email_verified=u.email_verified_at is not None,
        phone_verified=u.phone_verified_at is not None,
        subscription_status=u.subscription_status,
        trial_ends_at=u.trial_ends_at,
        has_access=has_product_access(u),
        next_step=_next_step(u),
        llm_key_managed=bool(u.llm_key_managed),
    )


class SignUpOut(BaseModel):
    """Sign-up authenticates immediately so the client can start onboarding."""
    token: TokenOut
    user: MeOut


@router.post("/sign-up", response_model=SignUpOut, status_code=201)
def sign_up(body: SignUpIn, db: Session = Depends(get_db)) -> SignUpOut:
    existing = db.query(User).filter(User.email == str(body.email).lower()).first()
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")
    u = User(
        email=str(body.email).lower(),
        password_hash=hash_password(body.password),
        full_name=body.full_name,
        company=body.company,
        phone=body.phone,
        role=UserRole.USER.value,
        status=UserStatus.PENDING.value,
        signup_source="self",
    )
    db.add(u); db.commit(); db.refresh(u)
    _send_email_code(db, u)                       # kick off email verification
    token, expires = create_access_token(u.id)
    return SignUpOut(token=TokenOut(access_token=token, expires_at=expires), user=_me_out(u))


@router.post("/sign-in", response_model=TokenOut)
def sign_in(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)) -> TokenOut:
    u = db.query(User).filter(User.email == form.username.lower()).first()
    if not u or not verify_password(form.password, u.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    # Rejected/deactivated can't sign in. PENDING self-serve users CAN — they need
    # to finish onboarding (verify + add card); the product itself is gated by
    # require_active, so signing in just lets them reach the onboarding steps.
    if u.status in (UserStatus.REJECTED.value, UserStatus.DEACTIVATED.value):
        raise HTTPException(status_code=403, detail=f"Account {u.status}")
    token, expires = create_access_token(u.id)
    u.last_login_at = datetime.now(timezone.utc)
    u.login_count = (u.login_count or 0) + 1
    db.commit()
    return TokenOut(access_token=token, expires_at=expires)


@router.get("/me", response_model=MeOut)
def me(user: User = Depends(current_user)) -> MeOut:
    return _me_out(user)


# ---- Self-serve onboarding: verify email + phone ----
class CodeIn(BaseModel):
    code: str


@router.post("/verify/email/send")
def send_email_code(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    if user.email_verified_at is not None:
        return {"sent": False, "already_verified": True}
    _send_email_code(db, user)
    return {"sent": True}


@router.post("/verify/email", response_model=MeOut)
def verify_email(body: CodeIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> MeOut:
    if user.email_verified_at is not None:
        return _me_out(user)
    ok, reason = check_code(db, user.id, "email", body.code)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)
    user.email_verified_at = datetime.now(timezone.utc)
    db.commit(); db.refresh(user)
    return _me_out(user)


@router.post("/verify/phone/send")
def send_phone_code(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    if user.phone_verified_at is not None:
        return {"sent": False, "already_verified": True}
    if not user.phone:
        raise HTTPException(status_code=400, detail="no_phone_on_file")
    _send_phone_code(db, user)
    return {"sent": True}


@router.post("/verify/phone", response_model=MeOut)
def verify_phone(body: CodeIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> MeOut:
    if user.phone_verified_at is not None:
        return _me_out(user)
    ok, reason = check_code(db, user.id, "phone", body.code)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)
    user.phone_verified_at = datetime.now(timezone.utc)
    db.commit(); db.refresh(user)
    return _me_out(user)


# ---- Admin user management ----
admin_router = APIRouter(prefix="/api/admin/users", tags=["admin"])


@admin_router.get("", response_model=list[UserOut])
def list_users(
    status: str | None = None,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[User]:
    q = db.query(User).order_by(User.created_at.desc())
    if status:
        q = q.filter(User.status == status)
    return q.all()


class AdminCreateUserIn(BaseModel):
    email: EmailStr
    password: str
    full_name: str | None = None
    company: str | None = None
    phone: str | None = None
    role: str = "user"          # 'user' | 'admin'
    status: str = "approved"    # admin-created users are active by default


@admin_router.post("", response_model=UserOut, status_code=201)
def admin_create_user(body: AdminCreateUserIn, _: User = Depends(require_admin),
                      db: Session = Depends(get_db)) -> User:
    """Admin creates a user directly (no sign-up/approval flow)."""
    email = body.email.lower().strip()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="A user with that email already exists")
    if body.role not in (UserRole.USER.value, UserRole.ADMIN.value):
        raise HTTPException(status_code=400, detail="Invalid role")
    if body.status not in {s.value for s in UserStatus}:
        raise HTTPException(status_code=400, detail="Invalid status")
    if len(body.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
    u = User(
        email=email, password_hash=hash_password(body.password),
        full_name=body.full_name, company=body.company, phone=body.phone,
        role=body.role, status=body.status, signup_source="admin",
    )
    db.add(u); db.commit(); db.refresh(u)
    return u


class ApprovalIn(BaseModel):
    status: str  # 'approved' | 'rejected' | 'deactivated'


@admin_router.patch("/{user_id}", response_model=UserOut)
def update_user_status(
    user_id: int,
    body: ApprovalIn,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> User:
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    if body.status not in (UserStatus.APPROVED.value, UserStatus.REJECTED.value, UserStatus.DEACTIVATED.value, UserStatus.PENDING.value):
        raise HTTPException(status_code=400, detail="Invalid status")
    u.status = body.status
    db.commit(); db.refresh(u)
    return u
