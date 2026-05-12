# TODO
- Backtesting Mode
	Replay historical snapshots and evaluate “if I bought alerts from last 30/60/90 days, what would happen?” This will quickly tune alert
- Auto Health Monitoring
	Detect API failures, stale polling, DB integrity issues, and send a dedicated ops alert channel.
- Test Suite Expansion
	Unit tests for pricing/inference rules and integration tests for API responses to prevent regressions.
- ML-assisted ranking
	Train a simple model on historical outcomes to predict “chance of profitable flip in N days.”
