"""Suite-wide fixtures.

Tests run against local Ollama and need no API key, whatever `.env` says.

A developer running hosted evals may have AGENT_PROVIDER=groq in `.env` or in
the shell. Without this, the live-model tests would then call the hosted
provider: they would spend the developer's key, fail in CI where there is no
key, and measure a different model from the one the assertions were written
against. So the provider is pinned to "ollama" for every test, and when the
environment had pointed at a hosted provider the model ids are put back to the
local defaults too, since a hosted model id does not exist on Ollama.

Tests that are about the provider setting itself build their own `Settings` or
monkeypatch these fields; a monkeypatch inside a test still wins over this.
"""

from __future__ import annotations

import pytest

from core.config import Settings, settings


@pytest.fixture(autouse=True)
def local_provider_only(monkeypatch):
    hosted = settings.agent_provider != "ollama" or settings.router_provider != "ollama"
    monkeypatch.setattr(settings, "agent_provider", "ollama")
    monkeypatch.setattr(settings, "agent_router_provider", "")
    if hosted:
        fields = Settings.model_fields
        monkeypatch.setattr(settings, "agent_model", fields["agent_model"].default)
        monkeypatch.setattr(settings, "agent_router_model",
                            fields["agent_router_model"].default)
        monkeypatch.setattr(settings, "agent_max_tokens", fields["agent_max_tokens"].default)
