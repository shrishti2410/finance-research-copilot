-- The daily precompute job's database role: read what the job needs, write
-- only the precomputed pages. Applied by scripts/create_precompute_role.py
-- (as the database owner, after migration 0008); the role itself is created
-- there with a generated password, never written into a file.
--
-- Created with SQL, not in the Neon Console: Neon makes Console-created roles
-- members of neon_superuser, which would give this job far more than it needs.
--
-- What the job does (scripts/precompute_views.py, market/precompute.py):
--   reads   price_bars, price_fetches   the daily price cache
--   reads   strategy_cards              card excerpts on the stock pages
--   writes  precomputed_views           INSERT ... ON CONFLICT DO UPDATE
-- Everything else (users, conversations, messages, filing_chunks, xbrl_facts,
-- usage_counters, alembic_version) stays out of reach: no grant, and new
-- tables in public grant nothing to PUBLIC by default.

-- Start from nothing, so re-running this file cannot leave an old grant behind.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM signals_precompute;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM signals_precompute;
REVOKE CREATE ON SCHEMA public FROM signals_precompute;

DO $$ BEGIN
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO signals_precompute', current_database());
END $$;
GRANT USAGE ON SCHEMA public TO signals_precompute;

GRANT SELECT ON price_bars, price_fetches, strategy_cards TO signals_precompute;
GRANT SELECT, INSERT, UPDATE ON precomputed_views TO signals_precompute;

-- A runaway job cannot hold a connection or a query open for long.
ALTER ROLE signals_precompute CONNECTION LIMIT 2;
ALTER ROLE signals_precompute SET statement_timeout = '120s';
ALTER ROLE signals_precompute SET idle_in_transaction_session_timeout = '60s';
