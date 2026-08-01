"""The seed admin must be reachable by the sign-in lookup that will search for it.

Sign-in does `User.email == form.username.lower()`. Anything that creates a user
has to normalise the same way, or the account exists but can never authenticate.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost:5432/unused")
os.environ.setdefault("JWT_SECRET", "test-secret")


@pytest.fixture()
def db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def test_seed_admin_email_is_stored_lowercase(db, monkeypatch):
    from app import security
    from app.models import User

    monkeypatch.setattr(security.settings, "seed_admin_email", "Matthew.Grant@Example.COM")
    monkeypatch.setattr(security.settings, "seed_admin_password", "s3cret")
    security.ensure_seed_admin(db)

    u = db.query(User).first()
    assert u.email == "matthew.grant@example.com"


def test_seed_admin_can_actually_sign_in(db, monkeypatch):
    """The regression itself: create with capitals, look up the way sign-in does."""
    from app import security
    from app.models import User

    monkeypatch.setattr(security.settings, "seed_admin_email", "Matthew@Example.com")
    monkeypatch.setattr(security.settings, "seed_admin_password", "s3cret")
    security.ensure_seed_admin(db)

    typed = "Matthew@Example.com"
    found = db.query(User).filter(User.email == typed.lower()).first()
    assert found is not None, "seed admin is unreachable by the sign-in lookup"
    assert security.verify_password("s3cret", found.password_hash)


def test_existing_mixed_case_row_is_repaired(db, monkeypatch):
    """An account already written with capitals by the old code gets fixed on boot."""
    from app import security
    from app.models import User, UserRole, UserStatus

    db.add(User(email="Old.Admin@Example.com",
                password_hash=security.hash_password("pw"),
                full_name="Seed Admin",
                role=UserRole.ADMIN.value, status=UserStatus.APPROVED.value))
    db.commit()

    security.ensure_seed_admin(db)

    assert db.query(User).filter(User.email == "old.admin@example.com").first() is not None
    assert db.query(User).filter(User.email == "Old.Admin@Example.com").first() is None


def test_repair_skips_collisions_rather_than_clobbering(db):
    """Never delete or merge an account to satisfy the unique index."""
    from app import security
    from app.models import User, UserRole, UserStatus

    for e in ("Bob@x.com", "bob@x.com"):
        db.add(User(email=e, password_hash=security.hash_password("pw"),
                    full_name="x", role=UserRole.USER.value,
                    status=UserStatus.APPROVED.value))
    db.commit()

    security._normalise_emails(db)
    assert db.query(User).count() == 2
    assert db.query(User).filter(User.email == "Bob@x.com").first() is not None
