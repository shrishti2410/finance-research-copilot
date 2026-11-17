"""Typed application settings, loaded from environment / `.env`.

The inference proxy predates this module and reads `os.getenv` directly. New
code goes through here instead: secrets and DSNs deserve to fail loudly at boot
rather than silently at the first request.
"""

import secrets
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Sentinel default so the app still boots for `alembic --sql` and unit tests,
# while `insecure_jwt_secret` can shout about it in any real deployment.
DEV_JWT_SECRET = "dev-only-insecure-change-me"

# The shortest JWT secret production accepts: 32 bytes is the HS256 key size.
MIN_JWT_SECRET_LENGTH = 32

# What SEC_EDGAR_USER_AGENT defaults to. Production refuses it: SEC asks for a
# real contact, and blocks clients that do not give one.
_PLACEHOLDER_USER_AGENT = "finance-research-copilot (contact: set SEC_EDGAR_USER_AGENT)"

# Query parameters libpq understands and asyncpg does not. A hosted Postgres
# (Neon, Supabase) hands out a URL ending in "?sslmode=require&channel_binding=
# require"; passed through, asyncpg fails at the first connection with
# "unexpected keyword argument 'sslmode'". They are removed from the URL and
# sslmode becomes asyncpg's own `ssl` argument (`database_connect_args`).
_LIBPQ_ONLY_PARAMS = ("sslmode", "channel_binding", "sslrootcert", "sslcert", "sslkey")


def _asyncpg_url(url: str) -> tuple[str, str | None]:
    """(the URL with the asyncpg driver and no libpq-only parameters, sslmode).

    Accepts what a provider's dashboard gives: postgres://, postgresql://, or
    postgresql+asyncpg://.
    """
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+asyncpg://" + url[len(prefix):]
            break
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    sslmode = next((value for key, value in query if key == "sslmode"), None)
    kept = [(key, value) for key, value in query if key not in _LIBPQ_ONLY_PARAMS]
    return urlunsplit(parts._replace(query=urlencode(kept))), sslmode


class Settings(BaseSettings):
    # hide_input_in_errors: a failed validation otherwise prints every setting
    # it was given -- JWT_SECRET, the provider keys, the database password --
    # into whatever log captures the boot.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False,
                                      hide_input_in_errors=True)

    # --- Environment ---
    # "production" turns the warnings this app prints about unsafe settings into
    # a refusal to start. See `production_is_configured` and docs/DEPLOY.md.
    environment: Literal["development", "production"] = "development"

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
    # The read timeout above bounds *silence*: every byte restarts it, so a stream
    # that keeps trickling is never stopped by it. This bounds one model call's
    # total wall-clock time instead, and the agent ends the turn as an
    # inference_error. 900s is 2.2x the slowest call that ever completed on the dev
    # host with the machine awake (411s, of 1,859 logged) and 4x the slowest
    # uncontended one (225s). See docs/INFERENCE.md for the data.
    inference_call_deadline: float = Field(default=900.0, gt=0)

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
    # Which provider serves each of the two models. "ollama" is whatever
    # INFERENCE_BASE_URL points at, and is the default so that tests and CI need
    # no key. "groq" and "cerebras" are hosted providers, reached through the
    # same proxy with GROQ_API_KEY or CEREBRAS_API_KEY. The router's provider
    # defaults to the answer model's; set it to run a hosted router in front of
    # a local answer model, or the reverse.
    #
    # A hosted provider only works when the agent calls the app's own proxy
    # (AGENT_INFERENCE_BASE_URL), because the proxy is what holds the key.
    agent_provider: Literal["ollama", "groq", "cerebras"] = "ollama"
    agent_router_provider: Literal["", "ollama", "groq", "cerebras"] = ""
    # The hosted providers. Each key is read from .env and nowhere else; it has
    # no default, and a provider that needs it refuses to start without it.
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    # Cerebras serves the same open-weight model under a different id:
    # "gpt-oss-120b" there, "openai/gpt-oss-120b" on Groq. AGENT_MODEL has to
    # be the id of the provider it is sent to.
    cerebras_api_key: str = ""
    cerebras_base_url: str = "https://api.cerebras.ai/v1"
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

    # --- External APIs ---
    # Every outbound call names its own timeout. A library default is not a
    # policy: it is invisible at the call site and changes under a pip upgrade.
    #
    # yfinance 1.2.0 defaults to 10s on history and 30s on the metadata behind
    # fast_info, and ships with its own retry loop *disabled*
    # (YfConfig.network.retries = 0). Both are set explicitly in tools/_yahoo.py.
    # Retries go to that built-in loop rather than a wrapper around it, which
    # would have multiplied the two. It backs off 2**attempt between tries.
    yfinance_timeout: float = 15.0
    yfinance_retries: int = 2
    # Google News RSS. Separate connect and read: a feed that accepts the socket
    # and then stalls is the common failure, and one combined number cannot
    # express "fail fast on unreachable, be patient once connected".
    news_connect_timeout: float = 5.0
    news_read_timeout: float = 10.0
    news_retries: int = 3
    # SEC EDGAR. Read is generous because a 10-K is megabytes; the rate limiter
    # in ingestion/edgar_client.py spaces the requests either way.
    edgar_connect_timeout: float = 10.0
    edgar_read_timeout: float = 60.0
    edgar_retries: int = 3

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

    @field_validator("internal_token")
    @classmethod
    def empty_internal_token_means_generate_one(cls, v: str) -> str:
        # "INTERNAL_TOKEN=" in a .env is an empty string, not an absent variable,
        # and an empty token matches nothing: the agent's own calls would then be
        # rate limited and refused the hosted provider. Empty means unset.
        return v or secrets.token_urlsafe(32)

    @field_validator("groq_base_url", "cerebras_base_url")
    @classmethod
    def strip_hosted_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def router_provider(self) -> str:
        """The router model's provider, resolved: its own setting, or the answer model's."""
        return self.agent_router_provider or self.agent_provider

    @model_validator(mode="after")
    def hosted_provider_has_its_key(self) -> "Settings":
        # Fail at boot, with the fix in the message. Without this the first
        # question after a deploy is what discovers the missing key, as a 503
        # from the proxy several layers away from the cause.
        for provider, setting in self._missing_provider_keys():
            if self.environment != "production":   # production lists it with the rest
                raise ValueError(
                    f"AGENT_PROVIDER or AGENT_ROUTER_PROVIDER is {provider!r} but {setting} "
                    f"is empty. Set {setting} in .env, or set the provider back to 'ollama'."
                )
        return self

    def _missing_provider_keys(self) -> list[tuple[str, str]]:
        """(provider, setting) for each hosted provider in use without its key."""
        keys = {"groq": ("GROQ_API_KEY", self.groq_api_key),
                "cerebras": ("CEREBRAS_API_KEY", self.cerebras_api_key)}
        return [(provider, setting) for provider, (setting, key) in keys.items()
                if provider in (self.agent_provider, self.router_provider) and not key]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @field_validator("agent_inference_base_url")
    @classmethod
    def strip_agent_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    # The sslmode a hosted provider's URL asked for, kept when the URL is
    # normalised. See _asyncpg_url.
    database_sslmode: str | None = None

    @model_validator(mode="after")
    def normalise_database_url(self) -> "Settings":
        url, sslmode = _asyncpg_url(self.database_url)
        object.__setattr__(self, "database_url", url)
        if sslmode and not self.database_sslmode:
            object.__setattr__(self, "database_sslmode", sslmode)
        return self

    @property
    def database_connect_args(self) -> dict:
        """asyncpg's connect arguments for the sslmode the URL asked for.

        "require" encrypts without checking the certificate, as libpq's does;
        "verify-full" checks it against the system's trust store.
        """
        mode = (self.database_sslmode or "").lower()
        if mode in ("require", "verify-ca", "verify-full"):
            return {"ssl": "require" if mode == "require" else True}
        return {}

    # --- Query embeddings (rag/embeddings.py) ---
    # torch's intra-op threads. 0 leaves torch's default, one per core it sees.
    # On a host with a fractional CPU quota that default is ruinous: torch
    # sees every core of the machine, starts that many threads, and the quota
    # throttles them all. Measured 2026-11-16 in the deploy image under
    # `docker run --cpus=0.1`: one query embedding took 55 s with the default
    # and 0.4 to 0.6 s with one thread. docs/DEPLOY.md sets 1.
    embedding_threads: int = Field(default=0, ge=0)
    # Hybrid retrieval: fuse the vector ranking with Postgres full-text search
    # by Reciprocal Rank Fusion (rag/retriever.py). Needs migration 0009.
    retrieval_hybrid: bool = False
    # Filter retrieval to the one indexed company a question names
    # (rag/retriever.py companies_named), when the caller gives none.
    retrieval_infer_company: bool = False
    # Load the encoder in the background as the app starts, rather than in the
    # first request that searches the filings. The same measurement: 180 s to
    # import and load at 0.1 CPU, which a visitor would otherwise wait out.
    embedding_preload: bool = False

    # --- Market data (market/) ---
    # How long a cached price range is served before it is fetched again. Not
    # forever, even for old history: the adjusted close is restated back
    # through history on every ex-dividend date. See market/store.py.
    market_cache_ttl_hours: float = Field(default=24.0, gt=0)

    # --- Public access (see api/quotas.py and docs/DEPLOY.md) ---
    # Open signup. A public demo turns it off and offers the demo account only:
    # with signup open, a visitor's way round a per-user cap is another account.
    signup_enabled: bool = True
    # POST /auth/demo hands out a fresh, isolated demo account.
    demo_enabled: bool = False
    # Questions one demo account may ask per UTC day, and all demo accounts
    # together. 0 means no cap.
    demo_questions_per_account: int = Field(default=5, ge=0)
    demo_questions_per_day: int = Field(default=60, ge=0)
    # Questions any other account may ask per UTC day. 0 means no cap.
    user_questions_per_day: int = Field(default=0, ge=0)
    # Prompt plus completion tokens the hosted provider may be sent per UTC day,
    # across every user. 0 means no cap; production with a hosted provider
    # requires one. A question is refused once the day's total has reached it,
    # so the overshoot is at most the questions already running at that moment.
    hosted_tokens_per_day: int = Field(default=0, ge=0)

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
    def hosted(self) -> bool:
        """Whether any model call goes to a metered hosted provider."""
        return self.agent_provider != "ollama" or self.router_provider != "ollama"

    @model_validator(mode="after")
    def production_is_configured(self) -> "Settings":
        """In production, refuse to start on a setting that is unsafe to expose.

        Every problem is listed at once, each with the variable to set, so a
        first deploy fails once rather than once per missing setting. Values
        are never echoed: several of these are secrets.
        """
        if self.environment != "production":
            return self
        problems = [f"{setting} must be set: AGENT_PROVIDER or AGENT_ROUTER_PROVIDER is "
                    f"{provider!r}." for provider, setting in self._missing_provider_keys()]
        if self.insecure_jwt_secret or len(self.jwt_secret) < MIN_JWT_SECRET_LENGTH:
            problems.append(
                f"JWT_SECRET must be set to a random value of at least "
                f"{MIN_JWT_SECRET_LENGTH} characters (python -c \"import secrets; "
                f"print(secrets.token_urlsafe(48))\").")
        if not self.cors_origin_list:
            problems.append("CORS_ORIGINS must name the front end's origin, "
                            "e.g. https://your-app.vercel.app.")
        for origin in self.cors_origin_list:
            if origin == "*" or not origin.startswith("https://") or origin.endswith("/"):
                problems.append(
                    f"CORS_ORIGINS entry {origin!r} is not allowed in production: each entry "
                    f"must be an exact https:// origin with no path or trailing slash.")
        if "@127.0.0.1" in self.database_url or "@localhost" in self.database_url:
            problems.append("DATABASE_URL points at this machine. Set it to the hosted "
                            "Postgres URL.")
        if not self.database_connect_args:
            problems.append("DATABASE_URL must ask for TLS (?sslmode=require), as hosted "
                            "Postgres URLs do.")
        if self.redis_url.startswith("redis://127.0.0.1") or self.redis_url.startswith(
                "redis://localhost"):
            problems.append("REDIS_URL points at this machine. Set it to the hosted Redis "
                            "URL (rediss://...).")
        if self.sec_edgar_user_agent == _PLACEHOLDER_USER_AGENT:
            problems.append("SEC_EDGAR_USER_AGENT must name a real contact.")
        if not self.rate_limit_enabled:
            problems.append("RATE_LIMIT_ENABLED must stay true in production.")
        if self.hosted and self.hosted_tokens_per_day <= 0:
            problems.append("HOSTED_TOKENS_PER_DAY must be a positive cap when a hosted "
                            "provider is used, or visitors can spend the whole allowance.")
        if self.demo_enabled and self.demo_questions_per_day <= 0:
            problems.append("DEMO_QUESTIONS_PER_DAY must be a positive cap when the demo "
                            "account is on.")
        if problems:
            raise ValueError("ENVIRONMENT=production, but:\n- " + "\n- ".join(problems))
        return self

    @property
    def sync_database_url(self) -> str:
        """Same DSN with a blocking driver, for tools that can't do async."""
        return self.database_url.replace("+asyncpg", "+psycopg")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
