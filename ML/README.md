# Investment ranking (ML/)

Ranks items for the companion by **expected % return per day held**: a quick small gain ranks above a
larger gain that takes months, and cheap and expensive items compete on the same % scale.

## Market assumptions

- Legacy unique items: no new copies are created; supply only changes when holders list or sell.
- Slow and thin: some items sell weekly, others sit for months. Listing prices are asks, recorded
  sales (from sale inference) are the only execution evidence.
- Dormant holders can list at any time, so an item's price can drop without warning.

## How a recommendation is computed

`features.snapshot()` builds a point-in-time view of one variant at time `t` using only data `<= t`:
cheapest instant-buyout listing (entry; non-instant floors are often unresponsive sellers), 30-day
median floor (listing anchor), recency-weighted 90-day sale median (sale anchor), sale counts,
listing counts and their change, new-listing rate, momentum.

`estimator.estimate()` turns a snapshot into:

| Quantity | Formula |
|---|---|
| fair value | sale anchor, shrunk toward the listing anchor when there are few sales (`w = n / (n + 3)`) |
| ask | fair value x (1 - `invest_undercut_pct`) |
| buyer flow at the ask | (90d sales + market rate x 60d prior) / 150d x share of recent sales at >= ask |
| queue | other instant listings priced at or below our ask (buyers take those first) |
| sale rate for our copy | buyer flow / (queue + 1) |
| P(sell within H) | `1 - exp(-rate x H)` (H = `invest_horizon_days`) |
| expected days held | `P / rate` + listing lag, capped at H |
| expected return | `P x (ask x (1 - fee) / entry - 1) + (1 - P) x unsold return` |
| **return per day** | expected return / expected days held |

Sparse items borrow the market-wide sale rate, so one lucky sale doesn't make a rare item look liquid.

A recorded sale is ignored when, in the poll just before it, another seller listed the **same roll**
(fingerprint) at least 10% cheaper: a buyer would have taken that copy (`market.sale_filter="roll"`).
In the Sep 2026 production DB this removed 460 of 2,664 sales, mostly "1 mirror" listings vanishing
while the same roll sat at ~0.4 mirror. Sales of *different* rolls priced above the floor are kept,
because better rolls can sell at a premium; whether 1-mirror sales of near-identical rolls are real
cannot be decided from this data alone (see "Known uncertainty" below).

Listing episodes (each seller + roll + price from first seen to gone) are also built. Using them to
update the sell rate (`use_listing_evidence`) made sell-chance predictions worse on the production
DB (predicted ~45% vs ~77% actual: it measures an average listing at that price, while ours is the
cheapest), so it is off by default; the counts are still model inputs.

## How it is evaluated (`simulate.py`)

A trading simulation on recorded history: every week, a strategy picks its top 5 items; we buy at
the floor and list at the estimator's ask. A position sells at the first later recorded sale at or
above the ask (that buyer would have taken our cheaper copy). Otherwise it is marked to market at the
floor when the horizon ends, never above the purchase price (a risen floor is not a realized gain).
Only weeks whose full horizon lies inside the data are used, so every outcome is fully observed.
The headline metric is total % return / total days held.

Strategies compared: the estimator, the learned model, and random picks.

## The learned model (`model.py`)

A small gradient-boosted regressor predicts the realized return/day of the simulated trade from the
snapshot features plus the estimator's own outputs (so it learns *corrections* to the formula).
Walk-forward: at each decision week it is refit only on rows whose outcome was already known.

The model replaces the estimator for ranking only when the last retrain showed, on at least 8
evaluated weeks, that it beat the estimator's return/day by 10% **and** won at least 75% of those
weeks. On synthetic data with no learnable signal, this gate enabled the model in 0 of 20 runs (a
looser 4-week gate enabled it in 3 of 12). Until then, and whenever the model was trained with a
different scikit-learn version, the transparent estimator ranks.

## Known uncertainty: sales far above the floor

Some items (e.g. Wurm's Molt, Karui Ward) have many recorded sales at exactly 1 mirror while near-
identical copies are listed around 0.5 mirror. If those are real, buying at the floor and listing
near 1 mirror is very profitable; if they are delistings, it is not. On the production DB the top-5
backtest gives +1.01%/day treating them as real and +0.42%/day treating every sale above 1.5x the
floor as fake; both beat random picks (about -0.45%/day).

## Operations

- Weekly retrain (poller, `poller/ml_retrain.py`) runs `scripts/retrain_ml_pipeline.py`, which writes
  `ML/models/profit_model.pkl` + `profit_model.json` (gate result, backtest summary). It exits 0
  whenever it completes, including when the model stays disabled.
- Admin page > ML retrain shows which ranking is live and the latest backtest return/day.
- Config (`app_config` key `market`): `invest_horizon_days` (60), `invest_fee_pct` (0),
  `invest_undercut_pct` (5).
- Run locally: `python scripts/retrain_ml_pipeline.py [--db path] [--root path]`.
- The server loads the last 130 days in light mode (no listing episodes, ~2s on the production DB)
  unless the learned model is enabled, which needs listing episodes as inputs.
- `ML/synthetic.py` generates a synthetic market for tests and experiments.
