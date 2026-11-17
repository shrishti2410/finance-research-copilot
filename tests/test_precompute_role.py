"""The precompute job's database role and its workflow: grants exactly what the
job needs, read-only repository permissions, nothing that prints the
connection string. The role was rehearsed on the local database with
scripts/create_precompute_role.py --no-secret.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# --- the role and the workflow ----------------------------------------------------------

def test_the_job_role_is_granted_exactly_what_the_job_needs():
    sql = (ROOT / "deploy/neon-precompute-role.sql").read_text(encoding="utf-8")
    code = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    grants = re.findall(r"GRANT\s+([A-Z, ]+?)\s+ON\s+([\w, ]+?)\s+TO\s+signals_precompute", code)
    tables = {}
    for privileges, objects in grants:
        for obj in (o.strip() for o in objects.split(",")):
            tables[obj] = {p.strip() for p in privileges.split(",")}
    assert tables == {
        "SCHEMA public": {"USAGE"},
        "price_bars": {"SELECT"},
        "price_fetches": {"SELECT"},
        "strategy_cards": {"SELECT"},
        "precomputed_views": {"SELECT", "INSERT", "UPDATE"},
    }
    assert not re.search(r"ALL PRIVILEGES|SUPERUSER|CREATEROLE|BYPASSRLS|neon_superuser\s+TO", code)
    assert "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM signals_precompute" in code


def test_the_workflow_runs_after_the_close_with_the_role_secret_and_read_only_permissions():
    wf = (ROOT / ".github/workflows/precompute-signals.yml").read_text(encoding="utf-8")
    assert 'cron: "0 11 * * 1-5"' in wf
    assert re.search(r"^permissions:\n\s+contents: read\b", wf, re.M)
    assert not re.search(r":\s*write\b", wf)             # no write permission anywhere
    assert "secrets.NEON_PRECOMPUTE_DATABASE_URL" in wf
    assert "pip install -r requirements-precompute.txt" in wf
    assert "python scripts/precompute_views.py" in wf
    # Nothing that would print the connection string into the job log.
    assert not re.search(r"echo\s+\$|set -x|printenv|env\s*$", wf, re.M)
