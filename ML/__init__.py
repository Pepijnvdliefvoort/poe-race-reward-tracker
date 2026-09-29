"""
Investment ranking for legacy unique items: expected % return per day held.

Modules:
- market:    load per-variant poll + sale history from SQLite
- features:  point-in-time snapshot of one variant (uses data <= t only)
- estimator: transparent expected-return-per-day formula (the baseline ranking)
- simulate:  trading simulation used to evaluate any ranking on history
- model:     learned return-per-day model, only enabled when it beats the estimator
- pipeline:  walk-forward evaluation + training entry point (weekly retrain)

The same features/estimator code runs in training, backtests and the server, so
what the model was evaluated on is exactly what the dashboard computes.
"""
