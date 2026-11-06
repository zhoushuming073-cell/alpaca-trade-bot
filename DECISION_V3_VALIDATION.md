# Decision v3 isolated validation (2026-11-06)

## Scope

Three pre-frozen historical inputs were sent to `deepseek-flash` after the
offline suite passed. This was a **contract/input validation only**:

- no broker client;
- no Alpaca keys;
- no order execution;
- no performance scoring;
- no changes to the frozen 15-day exam.

Frozen directory: `data/decision_v3_validation_r3/`.
Freeze version: `decision-v3-2026-11-06-r3`.
Three calls, no retries, temperature 0.2, max 3,000 output tokens.

## Results

| # | Historical input | Result | Actual decision |
|---|---|---|---|
| 0 | 2026-09-29 09:00 ET AMD premarket | **VALID** | `OPEN_LONG` 47 shares, confirmation above 630, limit 632, stop 585, target 720 |
| 1 | 2026-10-06 09:35 ET MRVL +6.73% profit review | **FAILED** | model returned one `HOLD` and one `PROTECT` for MRVL |
| 2 | 2026-10-09 09:35 ET LITE +16.05% profit review | **FAILED** | model returned `PROTECT` with `qty=5` |

Usage: 70,937 tokens total (22,202 + 24,513 + 24,222). Estimated upper cost
from the frozen byte model was CNY 0.40578; actual billing was not available.

## Why the two calls failed

These are **contract-shape failures**, not missing evidence or broker failures:

1. `validate_response` enforces one decision per symbol. MRVL emitted `HOLD`
   plus `PROTECT`; the two are semantically compatible but the schema treats
   them as conflicting.
2. `Decision` forbids `qty` on a non-trade action. LITE emitted `PROTECT` with
   `qty=5`; protection quantity is the whole current position and is not an
   order quantity.

## Correction

`prompts/decision_v3.txt` now states explicitly:

- exactly one decision per symbol;
- if a position remains open but protection changes, use `PROTECT` alone, not
  `HOLD` + `PROTECT`;
- `PROTECT` carries `stop_price` and optional `take_profit`, no `qty` and no
  `limit_price`;
- it preserves the current whole-share position and one linked exit group.

The corrected version is frozen as `data/decision_v3_validation_r4/`
(`decision-v3-2026-11-06-r4`), with `NOT_CALLED.md`.

**No fourth DeepSeek call was made.** The three-call budget was already used.
The corrected r4 input has **not** been validated against the model.

## What the valid AMD response demonstrates

The valid response used:

- the new time state (`PREMARKET`, `new_order_allowed=false`);
- recent dated news and diverse facts;
- a separate `confirmation` (above 630) and `limit_price` (632), rather than
  pretending the limit itself was confirmation;
- explicit 47-share sizing, $29,704 notional (29.7% of equity), and $2,209
  single-trade loss budget (2.2% of equity);
- a stop at 585 and target at 720;
- `WHILE_VALID` confirmation, which is simulation-only and explicitly rejected
  by the live request builder.

## What is not established

- No claim that the corrected r4 prompt will pass the model.
- No claim that the system performs better.
- No claim that short-side behavior is calibrated; the three cases did not
  contain a short setup.
- No actual orders, fills or P&L from this validation.
