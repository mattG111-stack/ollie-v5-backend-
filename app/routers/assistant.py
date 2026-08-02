"""Ask-anything endpoint, plus the Settings screen's key management.

Answers are grounded in tool calls against real data. The user brings their own
Claude or OpenAI key; it is stored encrypted and never returned to the client.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..assistant import keys, providers
from ..assistant.agent import AssistantUnavailable, Turn, ask
from ..db import get_db
from ..models import User
from ..security import require_active

router = APIRouter(prefix="/api/assistant", tags=["assistant"])


# --- settings --------------------------------------------------------------

class KeyStatus(BaseModel):
    configured: bool
    provider: str | None = None
    # Enough to recognise which key is saved, never the key itself.
    key_last_four: str | None = None
    updated_at: datetime | None = None
    detail: str


class SaveKeyIn(BaseModel):
    provider: str = Field(pattern="^(anthropic|openai)$")
    api_key: str = Field(min_length=10, max_length=400)


@router.get("/settings", response_model=KeyStatus)
def get_settings(me: User = Depends(require_active)) -> KeyStatus:
    plain = keys.decrypt(me.llm_api_key_encrypted)
    if me.llm_api_key_encrypted and not plain:
        # Stored value exists but won't decrypt — jwt_secret was rotated.
        return KeyStatus(configured=False, provider=me.llm_provider,
                         detail="Your saved key could not be read. Please re-enter it.")
    if not plain:
        return KeyStatus(configured=False,
                         detail="Add a Claude or OpenAI API key to use the assistant.")
    return KeyStatus(
        configured=True, provider=me.llm_provider,
        key_last_four=keys.last_four(me.llm_api_key_encrypted),
        updated_at=me.llm_key_updated_at,
        detail=f"Connected to {me.llm_provider}.",
    )


@router.put("/settings", response_model=KeyStatus)
def save_key(body: SaveKeyIn, me: User = Depends(require_active),
             db: Session = Depends(get_db)) -> KeyStatus:
    """Validate the key with a live call, then store it encrypted.

    Testing before saving means a typo is caught here rather than surfacing as
    a confusing failure on the user's first question.
    """
    if me.llm_key_managed:
        raise HTTPException(status_code=403, detail="The AI assistant is enabled by your administrator and can't be changed here.")
    api_key = body.api_key.strip()
    if msg := keys.looks_valid(body.provider, api_key):
        raise HTTPException(status_code=400, detail=msg)

    try:
        providers.run(
            provider=body.provider, api_key=api_key,
            system="Reply with the single word: ok",
            messages=[{"role": "user", "content": "ok"}],
            specs=[], dispatch=lambda *_: "",
        )
    except providers.ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    user = db.get(User, me.id)
    user.llm_provider = body.provider
    user.llm_api_key_encrypted = keys.encrypt(api_key)
    user.llm_key_updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(user)

    return KeyStatus(
        configured=True, provider=user.llm_provider,
        key_last_four=keys.last_four(user.llm_api_key_encrypted),
        updated_at=user.llm_key_updated_at,
        detail=f"Connected to {user.llm_provider}. Your key is saved.",
    )


@router.delete("/settings", response_model=KeyStatus)
def delete_key(me: User = Depends(require_active),
               db: Session = Depends(get_db)) -> KeyStatus:
    if me.llm_key_managed:
        raise HTTPException(status_code=403, detail="The AI assistant is enabled by your administrator and can't be removed here.")
    user = db.get(User, me.id)
    user.llm_provider = None
    user.llm_api_key_encrypted = None
    user.llm_key_updated_at = None
    db.commit()
    return KeyStatus(configured=False, detail="Key removed.")


# --- asking ----------------------------------------------------------------

class TurnIn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    # Prior turns, oldest first. The API is stateless — the client owns history.
    history: list[TurnIn] = Field(default_factory=list, max_length=20)


class AskOut(BaseModel):
    answer: str
    tools_used: list[str]
    iterations: int
    # The SQL the model ran, so any answer can be audited.
    queries: list[str]


@router.post("", response_model=AskOut)
def ask_question(body: AskIn, me: User = Depends(require_active)) -> AskOut:
    try:
        result = ask(me, body.question,
                     [Turn(role=t.role, content=t.content) for t in body.history])
    except AssistantUnavailable as exc:
        # 428 Precondition Required — the UI reads this as "send them to Settings".
        raise HTTPException(status_code=428, detail=str(exc)) from exc
    except providers.ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return AskOut(answer=result.text, tools_used=result.tools_used,
                  iterations=result.iterations, queries=result.queries)
