# Claude Instructions for poe-market-flips

## Goal
This repository tracks Path of Exile unique-item market listings, infers sales/reprices, and serves a dashboard + admin API.
Focus on safe, minimal diffs that preserve inference behavior and data integrity.

## Tech Stack
- Python 3.10+
- SQLite database at data/market.db
- Backend packages: poller/, server/, storage/
- Frontend assets: web/ (plain HTML/CSS/JS modules)

## Main Entry Points
- Poller: python -m poller
- Server: python -m server.server
- ML retrain pipeline: python scripts/retrain_ml_pipeline.py

## Repository Map
- poller/: trade polling, sale/relist/reprice inference, exports, weekly jobs
- server/: HTTP routing, admin endpoints, recommendation logic
- storage/: schema, migrations, repository and service layer
- web/: dashboard/admin/compare pages and static JS/CSS
- ML/: model training, candidates, reports, backtests

## High-Impact Rules
- Keep changes scoped. Do not refactor unrelated modules in the same patch.
- Preserve API response shapes unless explicitly requested.
- Do not rename DB columns/tables without migration updates in storage/schema.py.
- If schema changes, bump schema version and include backward-safe migration logic.
- Treat data/market.db as production-like state; avoid destructive reset logic.
- Prefer deterministic logic over probabilistic heuristics in sale inference paths.
- items.txt entries are unique items; currency items are not supported by this flow.

## Poller and Inference Safety
When editing poller/sale_inference_engine.py or poller/poll_item_prices.py:
- Keep relist-revert behavior intact for pending inferred sales.
- Keep truncation/cutoff guardrails intact before inferring disappearances.
- Avoid changing sale counting semantics unless explicitly requested.
- Maintain conservative behavior when evidence is ambiguous.

## Server and Admin Safety
When editing server/admin_service.py, server/http_handler.py, or auth-sensitive routes:
- Keep ADMIN_TOKEN enforcement fail-closed behavior.
- Do not open admin or config mutation endpoints without auth.
- Preserve existing endpoint URLs unless explicitly requested.

## Frontend Safety
- Keep existing page structure and vanilla JS module style.
- Prefer small, focused CSS/JS changes over broad restyling.
- Preserve mobile and desktop behavior in web/css/*.css split styles.

## Validation Checklist (Run What Applies)
- Python syntax check on changed files.
- Targeted tests for touched logic, for example:
  - python -m unittest poller.test_sale_inference_engine
  - python -m unittest ML.test_hybrid_gate
- If HTTP/API behavior changed, start server and verify impacted endpoint(s).
- If inference logic changed, verify no obvious regressions in inferred sale/relist flow.

## Style and Change Policy
- Follow existing code style in each file.
- Add brief comments only for non-obvious logic.
- Avoid adding new dependencies unless necessary.
- Include concise rationale in PR summary for behavior changes.

## Preferred Working Approach for AI
1. Read only the files needed for the task.
2. Propose and implement the smallest viable fix.
3. Run targeted validation.
4. Report what changed, why, and what was verified.
