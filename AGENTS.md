# AGENTS.md

This repository is the Alpaca AI Paper Trader. The only production trading decision engine is `OneShotEngine`. It connects only to Alpaca Paper, uses DeepSeek for research and decisions, SQLite for durable state, and a loopback dashboard.

## Safety rules

- Do not place any real or Paper order without explicit user authorization.
- Do not automatically resume trading, Autopilot, or a paused worker.
- Do not put credentials in YAML, SQLite, logs, prompts, tests, or Git.
- Every production order follows `OneShotEngine` → `OrderService` → `PaperGateway`, with strict Pydantic parsing, current account and quote checks, risk validation, state transitions, idempotency, and reconciliation.
- 09:00–09:30 ET is premarket preparation. 09:30–12:00 ET is the active window. New orders and active cancellations stop at 11:59:30 ET. After cutoff, monitor broker orders and positions without local order writes. Accepted DAY orders remain with the broker.
- Do not infer trade direction mechanically from price movement. Python strategy text is advisory; the fixed pool, funds, legal order shape, session, freeze, and duplicate checks are hard constraints.
- Preserve historical orders, runtime databases, backups, and credentials. Historical exam artifacts under `archive/` and `src/exam_*` are not production paths.

## Production structure

- `src/trading_bot/main.py` → `PaperStrategy` in `strategy.py` → `OneShotEngine.tick()`.
- `src/trading_bot/one_shot_scheduler.py` runs read-only premarket research retries.
- `src/trading_bot/one_shot_engine.py` owns AI decision framing, account context, cross-process decision locking, and immediate handoff to `OrderService`.
- `src/trading_bot/execution.py` owns safe, durable order submission and reconciliation.
- `src/trading_bot/broker.py` is the Alpaca Paper gateway; `dry_run.py` supplies a synthetic broker for offline tests.
- `src/trading_bot/storage.py` retains historical legacy tables for database compatibility and new OneShot decision reservations.
- `src/trading_bot/watchdog.py`, `dashboard.py`, and `web/` provide reconciliation and observation.

## Validation

```powershell
.venv/Scripts/python -m pytest -q
node --check web/app.js
.venv/Scripts/python -m compileall -q src tests
```

Offline fixtures do not prove live API connectivity or a Paper order. Keep the runtime paused after migration until the owner explicitly authorizes a first Paper test.
