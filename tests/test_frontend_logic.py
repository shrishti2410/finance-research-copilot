"""The front end's plain-JavaScript logic tests, run with Node's own test runner.

frontend/tests/*.test.mjs hold unit tests for logic kept free of React and of
TypeScript (src/lib/sessionCheck.mjs: when a stored session is kept, retried
or cleared). Running them from here puts them in the full suite and in CI,
which run pytest only. Each Node test is reported as its own case.

Needs `node` on PATH. Without it these skip, and a skip is not a pass (CI fails
on any skip).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = sorted((ROOT / "frontend" / "tests").glob("*.test.mjs"))
NODE = shutil.which("node")


def _run(path: Path) -> dict[str, bool]:
    out = subprocess.run([NODE, "--test", "--test-reporter=tap", str(path)],
                         capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=ROOT)
    results = {}
    for line in out.stdout.splitlines():
        m = re.match(r"^(not ok|ok) \d+ - (.+?)(?:\s+#.*)?$", line)
        if m:
            results[m.group(2)] = m.group(1) == "ok"
    assert results, f"no TAP results from {path.name}:\n{out.stdout}\n{out.stderr}"
    return results


def _cases():
    if not NODE:
        return [pytest.param(None, None, marks=pytest.mark.skip(reason="node is not on PATH"))]
    return [pytest.param(path, name, id=f"{path.stem}::{name}")
            for path in FILES for name in _run(path)]


_RESULTS: dict[Path, dict[str, bool]] = {}


@pytest.mark.parametrize("path, name", _cases())
def test_frontend_logic(path, name):
    results = _RESULTS.setdefault(path, _run(path))
    assert results[name], f"{path.name}: {name} failed; run: node --test {path.relative_to(ROOT)}"


def test_there_are_frontend_logic_tests():
    assert FILES, "frontend/tests/*.test.mjs is empty"


def test_the_auth_provider_clears_a_stored_token_only_on_a_401():
    """The boot-time check used to drop the token on any failure, so a 429 or a
    sleeping API signed the user out. It must go through checkStoredSession."""
    auth = (ROOT / "frontend/src/lib/auth.tsx").read_text(encoding="utf-8")
    assert "checkStoredSession" in auth
    assert ".catch(() => localStorage.removeItem(TOKEN_KEY))" not in auth
