# AGENTS.md

This repository is the Alpaca AI Paper Trader. It connects only to Alpaca
Paper, uses DeepSeek for research/decisions, SQLite for state and a loopback
Flask dashboard.

## Non-negotiable safety rules

- Do not place a real or Paper order without explicit user authorization.
- Do not auto-resume trading, `autopilot`, or a paused worker.
- Do not put API keys/secrets in YAML, SQLite, logs, prompts, tests or Git.
- Do not bypass `OrderService`, order-state transitions, reconciliation, risk
  validation or the 09:30-10:00 ET window.
- Do not infer a trade direction merely from market movement.
- Do not delete history, runtime databases, backups or tests.
- Do not run `eval()` on model output. All model output passes strict,
  versioned Pydantic schemas first.
- The single-agent baseline and all multi-agent research calls are shadow-only
  unless the existing execution path explicitly persists an approved plan.

## Current structure

- `src/trading_bot/engine.py` - premarket/active orchestration.
- `src/trading_bot/decision_schema.py` - strict plan/order schemas.
- `src/trading_bot/agent_schemas.py` - researcher/skeptic schemas.
- `src/trading_bot/multi_agent.py` - in-process three-role orchestrator.
- `src/trading_bot/execution.py` - idempotent order service.
- `src/trading_bot/storage.py` - SQLite WAL state and agent audit tables.
- `src/trading_bot/dashboard.py` / `web/` - local dashboard.
- `config/multi_agent.yaml` - multi-agent configuration, disabled by default.

## Validation

Run the offline suite before changing trading code:

```powershell
.venv/Scripts/python -m pytest -q
node --check web/app.js
.venv/Scripts/python -m compileall -q src tests
```

Real DeepSeek calls, Alpaca reads and Paper orders are separate validation
levels. Never report an offline fixture as a real API/order verification.
