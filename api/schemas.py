"""Pydantic request/response models for the API.

Implemented: auth, conversations, messages.
Still to come (the copilot surface itself):
- AskRequest (question, session_id?, filters?)
- AskResponse (answer, citations[], tool_calls[], usage)
- Citation (source_type, filing/tool ref, url, snippet)
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from auth.security import BCRYPT_MAX_BYTES

Role = Literal["user", "assistant", "system", "tool"]


# ─────────────────────────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────────────────────────

class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    display_name: str | None = Field(default=None, max_length=100)

    @field_validator("password")
    @classmethod
    def password_fits_bcrypt(cls, v: str) -> str:
        # min_length/max_length count characters; bcrypt truncates at 72 *bytes*,
        # and one emoji is four of them. Reject rather than silently truncate.
        if len(v.encode("utf-8")) > BCRYPT_MAX_BYTES:
            raise ValueError(f"password must be at most {BCRYPT_MAX_BYTES} bytes")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int  # seconds


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    display_name: str | None
    created_at: datetime
    # password_hash is absent by construction: this model is the only thing the
    # user routes return, so the column cannot leak through a response.


# ─────────────────────────────────────────────────────────────────────────────
# Conversations
# ─────────────────────────────────────────────────────────────────────────────

class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str | None
    created_at: datetime
    updated_at: datetime


# ─────────────────────────────────────────────────────────────────────────────
# Messages
# ─────────────────────────────────────────────────────────────────────────────

class MessageCreate(BaseModel):
    role: Role = "user"
    content: str = Field(min_length=1)
    meta: dict | None = None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: Role
    content: str
    meta: dict | None
    created_at: datetime


class MessagePage(BaseModel):
    """One page of history, oldest first.

    `next_cursor` is the id to pass back as `after_id`. It is None when this page
    is the end of the conversation -- keyset pagination, so page 500 costs the
    same as page 1 (OFFSET would make it 500x worse).
    """

    messages: list[MessageOut]
    next_cursor: int | None


# ── the agent surface ────────────────────────────────────────────────────────

class AskRequest(BaseModel):
    """A question put to the agent, in the context of an existing conversation."""

    conversation_id: uuid.UUID
    message: str = Field(min_length=1, max_length=8000)
    # Off by default: a trace is a debugging artifact, sometimes larger than the
    # answer, and most callers want the answer. It is stored on the assistant
    # message either way, so declining it here loses nothing.
    include_trace: bool = False


class AskStep(BaseModel):
    """One row of the agent's trace."""

    iteration: int
    tool: str
    arguments: dict
    result: object
    latency_ms: float
    model_latency_ms: float
    ok: bool


class AskResponse(BaseModel):
    conversation_id: uuid.UUID
    # Ids of the two messages this call persisted, so a client can fetch or
    # render them without re-reading the thread.
    user_message_id: int
    assistant_message_id: int
    answer: str
    # False when the loop hit its iteration limit or the model was unreachable.
    # The answer field still holds a written explanation in that case, never an
    # empty string -- but a caller must be able to tell the two apart.
    completed: bool
    stop_reason: str
    iterations: int
    model: str
    total_ms: float
    # Summed over every model call in the run. 0 with usage_measured False means
    # the upstream did not report usage, not that the run was free.
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    usage_measured: bool = False
    steps: list[AskStep] | None = None
