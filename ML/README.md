# Investment ranking (ML/)

Ranks items for the companion by **expected % return per day held**: a quick small gain ranks above a
larger gain that takes months, and cheap and expensive items compete on the same % scale.

## Market assumptions

- Legacy unique items: no new copies are created; supply only changes when holders list or sell.
- Slow and thin: some items sell weekly, others sit for months. Listing prices are asks, recorded
  sales (from sale inference) are the only execution evidence.
- Dormant holders can list at any time, so an item's price can drop without warning.
- Rolls rarely matter: copies of an item are interchangeable.
- Some buyers take listings at exactly 1 mirror without comparing prices, even when copies are listed
  for less in divines. Those sales are real but rare and slow, so 1 mirror is its own sales channel.
  Buyers pick among equal 1-mirror listings at random (the sold listing's position and age were
  uniform on the production DB), and the trade site sorts 1 mirror roughly at the market divine rate
  (after ~1,400-1,500 div, before ~1,800-2,000 div listings in Apr-Sep 2026).

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

Two listing plans are evaluated per item and the better return/day wins:
- **undercut**: list just under the normal-channel fair value; the queue is every instant listing at
  or below our ask. For items normally worth under ~0.9 mirror, 1-mirror sales are excluded from the
  normal channel so they don't inflate its fair value.
- **one_mirror**: list at exactly 1 mirror; the sale rate comes only from past 1-mirror sales (prior:
  the pooled 1-mirror rate across items, far below the normal rate) divided by the number of 1-mirror
  listings + 1. Confidence is based on the number of 1-mirror sales.

### Which recorded sales are used (`market.load_market`)

- **Anomaly days are excluded.** When a day's market-wide sales exceed 4x the median of the previous
  14 days (min 30), listings vanished en masse rather than sold: 2026-07-21..25 around GGG's search
  rate-limit change (up to 594 "sales" from 196 sellers in a day, vs ~5-25 normally) and 2026-06-24.
  That removed 1,277 of 2,664 recorded sales.
- **Transfer ping-pong is excluded.** When the same two sellers "transfer" a roll to each other more
  than once, those `confirmed_transfer` sales are traders relisting interchangeable copies (38 of 85).
- Everything else is kept (`sale_filter="none"`). Alternative filters ("roll": same roll listed
  cheaper by someone else; "floor_ratio": above 1.5x the cheapest listing) remain available for
  experiments; with 1-mirror buyers being real, they removed genuine sales.

Listing episodes (each seller + roll + price from first seen to gone) are also built. Using them to
update the sell rate (`use_listing_evidence`) made sell-chance predictions worse (it measures an
average listing at that price, while ours is the cheapest), so it is off by default; the counts are
still model inputs.

## How it is evaluated (`simulate.py`)

A trading simulation on recorded history: every week, a strategy picks its top 5 items; we buy at
the cheapest instant listing and list with the estimator's plan. Undercut: our copy sells at the
(queue + 1)-th later normal-channel sale, if that sale was at or above our ask. 1 mirror: at the
(N + 1)-th later 1-mirror sale. Otherwise it is marked to market at the floor when the horizon ends,
never above the purchase price (a risen floor is not a realized gain).
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

## Results on the production DB (Sep 2026, 60-day horizon, top 5 per week, 10 weeks)

| | Return/day | Per trade | Sold within 60d |
|---|---|---|---|
| Estimator | +0.09% | +3.6% (median +8.1%) | 54% |
| Random picks (20 seeds) | median -0.21% (best -0.15%) | | |

Sell-chance calibration over all item-weeks: predicted 45%, actual 45%. Earlier, much higher numbers
(+0.4 to +1.5%/day) came from the July mass-vanish artifact and from mixing 1-mirror sales into
normal prices; they were not real.

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
