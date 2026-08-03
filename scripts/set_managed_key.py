"""Provision an admin-managed LLM key onto a user account.

The key is read from a FILE (never a CLI arg or chat), validated with a live
provider call, stored encrypted, and the file is deleted. The user then has a
working assistant they can't see, change, or remove (llm_key_managed=True).

  python scripts/set_managed_key.py --email tester@ollie.co.nz --file /tmp/ck.txt
  python scripts/set_managed_key.py --email tester@ollie.co.nz --file /tmp/ck.txt --provider anthropic
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.assistant import keys, providers   # noqa: E402
from app.db import SessionLocal              # noqa: E402
from app.models import User                  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--file", required=True, help="path to a file containing ONLY the API key")
    ap.add_argument("--provider", default="anthropic", choices=["anthropic", "openai"])
    ap.add_argument("--keep-file", action="store_true", help="don't delete the key file after")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        raise SystemExit(f"key file not found: {path}")
    api_key = path.read_text().strip()
    if not api_key:
        raise SystemExit("key file is empty")

    if msg := keys.looks_valid(args.provider, api_key):
        raise SystemExit(f"key rejected: {msg}")

    print(f"validating {args.provider} key with a live call…")
    try:
        providers.run(provider=args.provider, api_key=api_key,
                      system="Reply with the single word: ok",
                      messages=[{"role": "user", "content": "ok"}],
                      specs=[], dispatch=lambda *_: "")
    except providers.ProviderError as e:
        raise SystemExit(f"live check failed — key not saved: {e}")

    db = SessionLocal()
    u = db.query(User).filter(User.email == args.email.lower()).first()
    if not u:
        raise SystemExit(f"no user with email {args.email}")
    u.llm_provider = args.provider
    u.llm_api_key_encrypted = keys.encrypt(api_key)
    u.llm_key_updated_at = datetime.now(timezone.utc)
    u.llm_key_managed = True
    db.commit()
    print(f"provisioned managed {args.provider} key on {u.email} "
          f"(ending …{keys.last_four(u.llm_api_key_encrypted)}); user can't see or change it.")
    db.close()

    if not args.keep_file:
        os.remove(path)
        print(f"deleted key file {path}")


if __name__ == "__main__":
    main()
