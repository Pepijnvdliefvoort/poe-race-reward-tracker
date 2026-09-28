"""
Synthetic market generator for tests and local experiments (never used in production).

Each variant has a fixed price level and sale rate. Optionally, "dormant supply" shocks add a
burst of new listings that undercut the market and push the next sales below the old price;
the burst shows up in `inf_new_listing_rows` a few days before prices react, which the formula
does not use but a learned model can.
"""

from __future__ import annotations

import math
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from storage.db import Database


def build_synthetic_db(
    root: Path,
    *,
    variants: int = 20,
    days: int = 200,
    poll_hours: float = 6.0,
    seed: int = 7,
    supply_shocks: bool = False,
    end: datetime | None = None,
) -> Path:
    rng = random.Random(seed)
    db = Database(root_dir=Path(root))
    con = db.connect()
    end = (end or datetime.now(timezone.utc)).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    step = timedelta(hours=poll_hours)

    specs = []
    for i in range(variants):
        con.execute("INSERT INTO items(name, created_at_utc) VALUES (?, ?)", (f"Item {i}", start.isoformat()))
        item_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.execute(
            "INSERT INTO item_variants(item_id, mode, display_name, sort_order) VALUES (?, 'aa', ?, ?)",
            (item_id, f"Item {i}", i),
        )
        vid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        specs.append(
            {
                "vid": vid,
                "price": math.exp(rng.uniform(math.log(1), math.log(60))),
                "daily_sales": rng.choice([0.01, 0.03, 0.08, 0.2, 0.5]),
                "listings": rng.randint(2, 25),
                "shock_until": None,
                "shock_start": None,
            }
        )

    t = start
    cycle = 0
    while t <= end:
        cycle += 1
        con.execute(
            "INSERT INTO poll_runs(cycle_number, league, started_at_utc, divines_per_mirror) VALUES (?, 'Standard', ?, 1650)",
            (cycle, t.isoformat()),
        )
        run_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        for s in specs:
            new_rows = 0
            price_mult = 1.0
            if supply_shocks:
                if s["shock_until"] is None and rng.random() < 0.004:
                    s["shock_start"], s["shock_until"] = t, t + timedelta(days=30)
                    new_rows = rng.randint(4, 8)
                if s["shock_until"] is not None:
                    if t > s["shock_until"]:
                        s["shock_until"] = s["shock_start"] = None
                    elif t > s["shock_start"] + timedelta(days=4):
                        price_mult = 0.8  # undercut supply lands a few days after it appears
            p_sale = 1 - math.exp(-s["daily_sales"] * step.total_seconds() / 86400)
            sold = rng.random() < p_sale
            low = s["price"] * rng.uniform(0.97, 1.08)
            con.execute(
                """INSERT INTO item_polls(poll_run_id, item_variant_id, requested_at_utc, query_id, total_results,
                       used_results, mirror_count, lowest_mirror, median_mirror, highest_mirror,
                       inf_likely_instant_sale, inf_new_listing_rows)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, s["vid"], t.isoformat(), f"q{s['vid']}", s["listings"] + new_rows, 10, 10,
                 low * price_mult, low * 1.2, low * 2, int(sold), new_rows),
            )
            poll_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
            if sold:
                sale_price = s["price"] * price_mult * rng.uniform(0.95, 1.15)
                con.execute(
                    """INSERT INTO sales(item_poll_id, item_variant_id, occurred_at_utc, recorded_at_utc, rule,
                           fingerprint, seller, mirror_equiv, price_amount, price_currency)
                       VALUES (?, ?, ?, ?, 'likely_instant_sale', ?, 'S', ?, ?, 'mirror')""",
                    (poll_id, s["vid"], t.isoformat(), t.isoformat(), f"fp{poll_id}", sale_price, sale_price),
                )
        t += step
    con.commit()
    con.close()
    return db.path


def open_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
