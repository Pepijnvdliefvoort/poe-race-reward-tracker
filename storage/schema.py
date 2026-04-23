from __future__ import annotations

SCHEMA_VERSION = 8


def migration_001_initial() -> str:
    # Notes:
    # - Timestamps are stored as ISO-8601 UTC strings to match existing CSV/json behavior.
    # - `currency` in listing snapshots is the normalized token used by the UI preview:
    #   mirror|divine|exalted|unknown
    return """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY NOT NULL,
  applied_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  icon_path TEXT,
  created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS item_variants (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  mode TEXT NOT NULL CHECK (mode IN ('aa','normal','any')),
  display_name TEXT NOT NULL,
  sort_order INTEGER,
  active_from_utc TEXT,
  active_to_utc TEXT,
  UNIQUE(item_id, mode)
);

CREATE INDEX IF NOT EXISTS idx_item_variants_item ON item_variants(item_id);
CREATE INDEX IF NOT EXISTS idx_item_variants_sort ON item_variants(sort_order, display_name);

CREATE TABLE IF NOT EXISTS poll_runs (
  id INTEGER PRIMARY KEY,
  cycle_number INTEGER NOT NULL,
  league TEXT NOT NULL,
  started_at_utc TEXT NOT NULL,
  divines_per_mirror REAL,
  top_ids_limit INTEGER,
  inference_fetch_cap INTEGER,
  app_version TEXT,
  UNIQUE(cycle_number, league)
);

CREATE INDEX IF NOT EXISTS idx_poll_runs_started ON poll_runs(started_at_utc);

CREATE TABLE IF NOT EXISTS item_polls (
  id INTEGER PRIMARY KEY,
  poll_run_id INTEGER NOT NULL REFERENCES poll_runs(id) ON DELETE CASCADE,
  item_variant_id INTEGER NOT NULL REFERENCES item_variants(id) ON DELETE CASCADE,

  requested_at_utc TEXT NOT NULL,
  query_id TEXT NOT NULL,
  total_results INTEGER NOT NULL DEFAULT 0,
  used_results INTEGER NOT NULL DEFAULT 0,
  unsupported_price_count INTEGER NOT NULL DEFAULT 0,

  mirror_count INTEGER NOT NULL DEFAULT 0,
  lowest_mirror REAL,
  median_mirror REAL,
  highest_mirror REAL,

  divine_count INTEGER NOT NULL DEFAULT 0,
  lowest_divine REAL,
  median_divine REAL,
  highest_divine REAL,

  inf_confirmed_transfer INTEGER NOT NULL DEFAULT 0,
  inf_likely_instant_sale INTEGER NOT NULL DEFAULT 0,
  inf_relist_same_seller INTEGER NOT NULL DEFAULT 0,
  inf_non_instant_removed INTEGER NOT NULL DEFAULT 0,
  inf_reprice_same_seller INTEGER NOT NULL DEFAULT 0,
  inf_multi_seller_same_fingerprint INTEGER NOT NULL DEFAULT 0,
  inf_new_listing_rows INTEGER NOT NULL DEFAULT 0,

  fetched_for_inference INTEGER NOT NULL DEFAULT 0,

  UNIQUE(poll_run_id, item_variant_id)
);

CREATE INDEX IF NOT EXISTS idx_item_polls_variant_time ON item_polls(item_variant_id, requested_at_utc);
CREATE INDEX IF NOT EXISTS idx_item_polls_query_id ON item_polls(query_id);
CREATE INDEX IF NOT EXISTS idx_item_polls_run ON item_polls(poll_run_id);

CREATE TABLE IF NOT EXISTS listing_snapshots (
  id INTEGER PRIMARY KEY,
  item_poll_id INTEGER NOT NULL REFERENCES item_polls(id) ON DELETE CASCADE,
  rank INTEGER NOT NULL,
  seller_name TEXT NOT NULL,
  price_text TEXT NOT NULL,
  amount REAL,
  currency TEXT NOT NULL,
  is_instant_buyout INTEGER NOT NULL DEFAULT 0,
  posted TEXT,
  indexed TEXT,
  fingerprint TEXT,
  UNIQUE(item_poll_id, rank)
);

CREATE INDEX IF NOT EXISTS idx_listing_snapshots_poll ON listing_snapshots(item_poll_id);

CREATE TABLE IF NOT EXISTS inference_events (
  id INTEGER PRIMARY KEY,
  item_poll_id INTEGER NOT NULL REFERENCES item_polls(id) ON DELETE CASCADE,
  rule TEXT NOT NULL,
  fingerprint TEXT,
  seller TEXT,
  from_seller TEXT,
  to_seller TEXT,
  prev_mirror_equiv REAL,
  curr_mirror_equiv REAL,
  count INTEGER,
  meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_inference_events_poll ON inference_events(item_poll_id);

-- Persisted inference working set (replacement for sale_inference_state.json)
CREATE TABLE IF NOT EXISTS inference_state_signals (
  id INTEGER PRIMARY KEY,
  item_variant_id INTEGER NOT NULL REFERENCES item_variants(id) ON DELETE CASCADE,
  fingerprint TEXT NOT NULL,
  seller TEXT NOT NULL,
  is_instant INTEGER NOT NULL DEFAULT 0,
  mirror_equiv REAL,
  last_seen_cycle INTEGER,
  UNIQUE(item_variant_id, fingerprint, seller)
);

CREATE INDEX IF NOT EXISTS idx_inf_state_signals_variant ON inference_state_signals(item_variant_id);

CREATE TABLE IF NOT EXISTS inference_state_pending (
  id INTEGER PRIMARY KEY,
  item_variant_id INTEGER NOT NULL REFERENCES item_variants(id) ON DELETE CASCADE,
  fingerprint TEXT NOT NULL,
  seller TEXT NOT NULL,
  removed_cycle INTEGER NOT NULL,
  counted_immediate INTEGER NOT NULL DEFAULT 0,
  UNIQUE(item_variant_id, fingerprint, seller)
);

CREATE INDEX IF NOT EXISTS idx_inf_state_pending_variant ON inference_state_pending(item_variant_id);
"""


def migration_002_app_config() -> str:
    return """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS app_config (
  key TEXT PRIMARY KEY NOT NULL,
  value_json TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_app_config_updated ON app_config(updated_at_utc);
"""


def migration_003_visitors() -> str:
    return """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS visits (
  id INTEGER PRIMARY KEY,
  ts_utc TEXT NOT NULL,
  ip TEXT NOT NULL,
  path TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_visits_ts ON visits(ts_utc);
CREATE INDEX IF NOT EXISTS idx_visits_ip ON visits(ip);

CREATE TABLE IF NOT EXISTS ip_geo_cache (
  ip TEXT PRIMARY KEY NOT NULL,
  lat REAL NOT NULL,
  lon REAL NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ip_geo_cache_updated ON ip_geo_cache(updated_at_utc);
"""


def migration_004_sales() -> str:
    return """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sales (
  id INTEGER PRIMARY KEY,
  item_poll_id INTEGER NOT NULL REFERENCES item_polls(id) ON DELETE CASCADE,
  item_variant_id INTEGER NOT NULL REFERENCES item_variants(id) ON DELETE CASCADE,

  -- The poll timestamp (when we observed the transition).
  occurred_at_utc TEXT NOT NULL,
  -- When we wrote this row (can differ from occurred_at_utc on retries/backfills).
  recorded_at_utc TEXT NOT NULL,

  -- Which inference rule produced the sale signal.
  rule TEXT NOT NULL CHECK (rule IN ('confirmed_transfer', 'likely_instant_sale')),
  fingerprint TEXT NOT NULL DEFAULT '',

  seller TEXT NOT NULL,
  buyer TEXT NOT NULL DEFAULT '',

  -- Price as observed on the ladder row (if present), plus normalized mirror equivalent.
  price_amount REAL,
  price_currency TEXT,
  mirror_equiv REAL,

  quantity INTEGER NOT NULL DEFAULT 1,

  -- If an instant-sale signal was later negated (e.g. relist), we mark it reverted instead of deleting.
  reverted_at_utc TEXT,
  reverted_by_item_poll_id INTEGER REFERENCES item_polls(id) ON DELETE SET NULL,
  reverted_reason TEXT,

  UNIQUE(item_variant_id, rule, fingerprint, seller, buyer, occurred_at_utc)
);

CREATE INDEX IF NOT EXISTS idx_sales_variant_time ON sales(item_variant_id, occurred_at_utc);
CREATE INDEX IF NOT EXISTS idx_sales_time ON sales(occurred_at_utc);
CREATE INDEX IF NOT EXISTS idx_sales_poll ON sales(item_poll_id);
"""


def migration_005_sales_reverts() -> str:
    """Applied via a Python idempotent migration in `storage/db.py`."""
    return ""


def migration_006_inference_price_state() -> str:
    """Applied via a Python idempotent migration in `storage/db.py`."""
    return ""


def migration_007_price_alert_cooldown() -> str:
    """Last price-drop Discord alert per variant (survives poller restarts; uses global cycle_number)."""
    return """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS price_alert_cooldown (
  item_variant_id INTEGER PRIMARY KEY NOT NULL REFERENCES item_variants(id) ON DELETE CASCADE,
  last_alert_cycle INTEGER NOT NULL,
  last_alert_low_mirror REAL NOT NULL,
  updated_at_utc TEXT NOT NULL
);
"""


def migration_008_non_instant_online_inference() -> str:
    """Applied via a Python idempotent migration in `storage/db.py`."""
    return ""

