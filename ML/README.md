# Investment ranking (ML/)

Ranks items for the companion by **expected % return per day held**: a quick small gain ranks above a
larger gain that takes months, and cheap and expensive items compete on the same % scale.

## Market assumptions

- Legacy unique items: no new copies are created; supply only changes when holders list or sell.
- Slow and thin: some items sell weekly, others sit for months. Listing prices are asks, recorded
  sales (from sale inference) are the only execution evidence.
- Dormant holders can list at any time, so an item's price can drop without warning.
- Rolls rarely matter: copies of an item are interchangeable.
- Two sales channels, split by listing currency. The **divine channel** is listings priced in divines
  (or fractional mirrors); the **mirror channel** is listings at exactly k whole mirrors. Many buyers
  pay in mirrors and take whole-mirror listings without comparing them to divine listings, so the
  channels have their own prices, sale rates and queues. Mirror-channel sales are real but slow.
  Buyers pick among equal whole-mirror listings at random (the sold listing's position and age were
  uniform on the production DB), and the trade site sorts k mirrors roughly at the market divine rate
  (1 mirror after ~1,400-1,500 div, before ~1,800-2,000 div listings in Apr-Sep 2026).

## How a recommendation is computed

`features.snapshot()` builds a point-in-time view of one variant at time `t` using only data `<= t`:
cheapest instant-buyout listing in any currency (entry; non-instant floors are often unresponsive
sellers), the divine-channel 30-day median floor (listing anchor) and recency-weighted 90-day sale
median (sale anchor), divine sale counts, whole-mirror listings and sale amounts, listing counts and
their change, new-listing rate, momentum.

`estimator.estimate()` turns a snapshot into:

| Quantity | Formula |
|---|---|
| fair value | divine sale anchor, shrunk toward the listing anchor when there are few sales (`w = n / (n + 3)`) |
| ask | fair value x (1 - `invest_undercut_pct`) |
| buyer flow at the ask | (90d sales + market rate x 30d prior) / 120d x share of recent sales at >= ask |
| queue | listings shown before ours (buyers take those first) |
| sale rate for our copy | buyer flow / (queue + 1) |
| P(sell within H) | `1 - exp(-rate x H)` (H = `invest_horizon_days`) |
| expected days held | `P / rate` + listing lag, capped at H |
| unsold return | min(entry, fair) x (1 - 5% per 30 days held) / entry - 1 |
| expected return | `P x (ask x (1 - fee) / entry - 1) + (1 - P) x unsold return` |
| **return per day** | expected return / expected days held |

Sparse items borrow the market-wide sale rate, so one lucky sale doesn't make a rare item look liquid.

The unsold markdown was measured: marking every copy at min(later floor, entry) after 60 days lost
~10% on average (median 0). Without it, long-shot listings looked like free lottery tickets.

Listing plans are evaluated per item and the best return/day wins:
- **undercut** (list in divines): just under the divine-channel fair value. The queue is the other
  divine listings at or below our ask plus the whole-mirror listings worth <= our ask, which the
  trade site shows first. Whole-mirror sales never set the divine fair value.
- **mirror** (list at exactly k mirrors): buyer flow comes only from past whole-mirror sales at
  >= k (prior: the pooled whole-mirror rate across items); the queue is every whole-mirror listing
  at <= k. Candidates: the next whole mirror above the entry, plus amounts the item sold at up to
  1.5x its value. "Sales" far above value (e.g. 100 mirrors for a 20-mirror item) were mostly
  withdrawn fantasy listings: those picks never sold. Confidence counts sales at >= k.

### Which recorded sales are used (`market.load_market`)

- **Anomaly days are excluded.** When a day's market-wide sales exceed 4x the median of the previous
  14 days (min 30), listings vanished en masse rather than sold: 2026-07-21..25 around GGG's search
  rate-limit change (up to 594 "sales" from 196 sellers in a day, vs ~5-25 normally) and 2026-06-24.
  That removed 1,277 of 2,664 recorded sales.
- **Transfer ping-pong is excluded.** When the same two sellers "transfer" a roll to each other more
  than once, those `confirmed_transfer` sales are traders relisting interchangeable copies (38 of 85).
- Everything else is kept (`sale_filter="none"`). Alternative filters ("roll": same roll listed
  cheaper by someone else; "floor_ratio": above 1.5x the cheapest listing) remain available for
  experiments; with whole-mirror buyers being real, they removed genuine sales.

Listing episodes (each seller + roll + price from first seen to gone) are also built. Using them to
update the sell rate (`use_listing_evidence`) made sell-chance predictions worse (it measures an
average listing at that price, while ours is the cheapest), so it is off by default; the counts are
still model inputs.

## How it is evaluated (`simulate.py`)

A trading simulation on recorded history: every week, a strategy picks its top 5 items; we buy at
the cheapest instant listing and list with the estimator's plan. Undercut: our copy sells at the
(queue + 1)-th later sale among divine sales and whole-mirror sales worth <= our ask, if that sale
was a divine sale at or above our ask. Mirror (k): at the (queue + 1)-th later whole-mirror sale, if
it was at >= k mirrors. Otherwise it is marked to market at the floor when the horizon ends,
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
| Estimator | +0.22% | +9.3% (median +4.4%) | 55% |
| Estimator, divine plan only | +0.17% | +6.6% | 59% |
| Random picks (20 seeds) | median -0.14% (best +0.05%) | | |

Sell-chance calibration on the picks with a positive estimate: whole-mirror plan predicted 44%,
actual 43%; divine plan predicted 86%, actual 77%. Over all item-weeks: predicted 17%, actual 14%.
Earlier, much higher numbers (+0.4 to +1.5%/day) came from the July mass-vanish artifact and from
mixing whole-mirror sales into divine prices; they were not real.

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
