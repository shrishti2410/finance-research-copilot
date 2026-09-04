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
