"""Typed application settings, loaded from environment / `.env`.

The inference proxy predates this module and reads `os.getenv` directly. New
code goes through here instead: secrets and DSNs deserve to fail loudly at boot
rather than silently at the first request.
"""

import secrets
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Sentinel default so the app still boots for `alembic --sql` and unit tests,
# while `insecure_jwt_secret` can shout about it in any real deployment.
DEV_JWT_SECRET = "dev-only-insecure-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # --- Database ---
    # asyncpg for the app. Alembic reuses this URL and swaps the driver itself.
    database_url: str = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/finance_copilot"
    db_echo: bool = False
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # --- SEC EDGAR ---
    # EDGAR rejects requests without a descriptive User-Agent carrying a real
    # contact address. See https://www.sec.gov/os/webmaster-faq#developers
    sec_edgar_user_agent: str = "finance-research-copilot (contact: set SEC_EDGAR_USER_AGENT)"
    # SEC's published ceiling is 10 requests/second. Staying under it is the
    # difference between being a good citizen and being IP-blocked.
    sec_requests_per_second: float = 5.0
    # Downloaded filings are cached here so reruns cost SEC nothing. `data/` is
    # gitignored.
    edgar_cache_dir: str = "data/edgar"

    # --- Inference ---
    # Any OpenAI-compatible server: vLLM, Ollama, or llama.cpp.
    inference_base_url: str = "http://127.0.0.1:8001/v1"
    inference_api_key: str = ""
    inference_model: str = "Qwen/Qwen2.5-1.5B-Instruct"
    # Connect stays short so a dead upstream fails fast; read is generous because
    # a CPU-hosted model can take many seconds before the first token appears.
    inference_connect_timeout: float = 5.0
    inference_read_timeout: float = 300.0

    @field_validator("inference_base_url")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        # httpx joins base_url and path with exactly one separator; a trailing
        # slash here produces "/v1//models".
        return v.rstrip("/")

    # --- Agent ---
    # The loop calls the app's own OpenAI-compatible proxy rather than Ollama
    # directly, so agent traffic goes through the same timeouts, logging and
    # upstream config as every other client.
    agent_inference_base_url: str = "http://127.0.0.1:8000/v1"
    # Tool calling is the whole mechanism here. 7B is the smallest that both
    # picks tools and writes the answer; see agent_router_model for what a
    # smaller model measurably can and cannot do.
    agent_model: str = "qwen2.5:7b"
    # A smaller model for the tool-selection steps only, with agent_model
    # reserved for writing the answer. Measured on this host: 1.5b picks the
    # right tool on 6 of 6 representative questions at 13.9 tok/s against 7b's
    # 2.8 -- a 13.7s routing step becomes 3.7s.
    #
    # What it cannot do is recover. Asked for a current stock price it called
    # get_stock_price without the required end_date, read the bad_input
    # envelope, and answered "I couldn't find the current stock price" instead
    # of retrying with the date; 7b got it right first time. So the loop
    # escalates to agent_model after any failed tool call -- recovery is
    # reasoning, which is what the larger model is for.
    #
    # Empty disables the split and every step runs on agent_model.
    agent_router_model: str = "qwen2.5:1.5b"
    # Generated-token caps. Ollama is unbounded by default, which on a CPU host
    # means one runaway answer can generate for minutes at ~4 tok/s. Across the
    # 40 eval questions the longest final answer was ~368 tokens and the median
    # ~57, so 512 clears every observed answer with room to spare and binds only
    # on a runaway. 384 would have clipped the longest real answer.
    agent_max_tokens: int = 512
    # A routing step emits a tool call, about 35 tokens. The cap is generous
    # against that because the router is also what writes the draft that gets
    # discarded when it stops asking for tools -- a low cap makes that draft
    # cheap to throw away.
    agent_router_max_tokens: int = 128
    agent_max_iterations: int = 5
    # Bounds work, where agent_max_iterations bounds round-trips: Qwen
    # emits parallel tool calls, and one iteration held 16 of them in the
    # M6 audit. See agent/orchestrator.py for why the default is 8.
    agent_max_tool_calls_per_iteration: int = 8
    # Prior messages replayed to the model so a follow-up can resolve a
    # reference ("what about Apple's?"). Set to 0 to make every /ask a
    # standalone question again. See agent/memory.py for why this is a
    # window rather than a summary.
    agent_history_messages: int = 10

    # --- Browser client ---
    # Origins allowed to call this API from a browser. The Next.js dev
    # server is the only one by default. Comma-separated in the env; an
    # empty value disables CORS entirely rather than defaulting to '*',
    # because credentials ride on these requests.
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # Lets the process recognise its own loopback calls and skip rate limiting
    # on them. Random per process and never written down: it is not a
    # credential anyone provisions, and a restart invalidates it. Without it a
    # single /ask would spend five of the caller's twenty anonymous requests a
    # minute on the app talking to itself.
    internal_token: str = Field(default_factory=lambda: secrets.token_urlsafe(32))

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @field_validator("agent_inference_base_url")
    @classmethod
    def strip_agent_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    # --- Auth ---
    jwt_secret: str = DEV_JWT_SECRET
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 30
    bcrypt_rounds: int = 12

    # --- Rate limiting ---
    redis_url: str = "redis://127.0.0.1:6379/0"
    rate_limit_enabled: bool = True
    rate_limit_window_seconds: int = 60
    # Authenticated callers, keyed by user id -- the limit that follows a person
    # across devices and IPs.
    rate_limit_per_minute: int = 60
    # Unauthenticated callers, keyed by IP. Lower, because an IP is a much
    # weaker identity and is all an anonymous caller has.
    rate_limit_anon_per_minute: int = 20
    # /auth/* specifically, keyed by IP regardless of token. Login and signup are
    # the brute-force surface, and they are reachable without credentials.
    rate_limit_auth_per_minute: int = 10
    # When Redis is unreachable: True serves the request anyway, False returns
    # 503. See docs/RATE_LIMITING.md -- this is a real availability trade-off.
    rate_limit_fail_open: bool = True
    # Only enable behind a proxy you control that overwrites X-Forwarded-For.
    # With it on and no such proxy, any caller can spoof their own rate-limit key.
    rate_limit_trust_proxy: bool = False

    @property
    def insecure_jwt_secret(self) -> bool:
        return self.jwt_secret == DEV_JWT_SECRET

    @property
    def sync_database_url(self) -> str:
        """Same DSN with a blocking driver, for tools that can't do async."""
        return self.database_url.replace("+asyncpg", "+psycopg")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
