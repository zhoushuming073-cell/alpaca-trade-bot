# Decision-v3 remediation report (2026-11-06)

This report uses the frozen `full15` exam, the independent audit, and the
versioned v3 code. It does **not** use a new return as proof of skill. The old
15-day exam remains unchanged and is only a failure-discovery sample.

## A. Root-cause audit

### A1. Direction asymmetry / no shorts

| Layer | Evidence | Classification | Confidence |
|---|---|---|---|---|
| Old prompt | `prompts/premarket_plan.txt` (frozen old hash) contained “If no short setup… HOLD/WAIT/NO_TRADE” and “Do not short merely because the broad market is down”; `universe_scan.txt` said shorts needed special asset support and not to infer from weakness | Prompt bias | High |
| Old schema | `SetupType` had only `PULLBACK_RECLAIM` and `EVENT_MISPRICING`; old scan candidates: 82 pullback, 13 event | Structural/design bias | High |
| Old scan output | 95 candidates: 31 `OPEN_LONG`, 32 `WAIT`, 32 `HOLD`, **0 `OPEN_SHORT`** | AI behavior | High |
| Old account | `shorting_enabled=true`, permits short, isolate assumption in old replay | Not an account restriction | High |
| Old focus packet | `_focus()` did not attach per-symbol `shortability` to the plan focus rows; scan rows did have it | Interface defect | High |
| Old executor | No short order rejected; simulator supports `OPEN_SHORT`; no evidence of a direction block | Not an executor block | High |
| AI reasoning | 9 short-related text fragments but no actual short order; reasons include “no short without fresh negative catalyst” etc. | AI decision; exact motive UNKNOWN | Medium |

The old system did not prove “AI cannot short.” It proved that the prompt,
setup taxonomy, focus packet, and model choice produced no short attempt. The
current v3 prompt and schema are direction-neutral and include
`FAILED_RALLY`/`BREAKDOWN`/`EVENT_REPRICING`; validation still must not be
interpreted as proof that shorts will occur.

### A2. Profit giveback

- TSM MFE **+8.37%** at 2026-10-05 14:36 ET; outside the local 09:30–10:00
  window. The only protection was the original 442.50 GTC stop.
- MRVL MFE **+14.05%** at 2026-10-06 09:44 ET; inside the window, but the old
  replay performed only one regular position review at 09:31, before the peak.
- LITE MFE **+16.88%** at 2026-10-09 09:33 ET; inside the window, but again the
  old review was at 09:31 and the old prompt defaulted to HOLD on “no new
  information.”
- All three ended at their original, far-below-entry stop. The old system had no
  profit milestone, no peak/giveback field, no original-thesis field in the
  review packet, and no `PROTECT` action. This is an interface and prompt
  design defect, not a claim that the exact MFE price was executable.

### A3. News and fundamentals

For three audited dates, the V2 snapshot had thousands of visible news items
(3,249 / 3,743 / 4,434), hundreds of SEC filings, and thousands of financial
facts. The old AI input contained **96 SEC metadata items** and **92 `Assets`
facts** because `runner.py::_news_and_facts` took the first two items per symbol
from a list ordered by publication time and had no news/SEC quota or financial
tag diversity. It also sent only titles, not news bodies, and the intraday
recheck packet had no news/facts at all.

Classification: **data interface selection/sorting and truncation defect**, not
a database absence. Historical content versions remain `UNKNOWN`.

### A4. Narrative intent vs order

- TSM: narrative said “entry above 454” but the structured trigger was
  `last <= 454` and limit 454. The fill at 449.81 was legal for that structure,
  but the narrative was not a precise claim of a breakout confirmation.
- MU: narrative said limit 1110 only fills if strength persists, while the
  structured trigger was `last >= 1100`, limit 1110. A print at 1100 could fill
  at 1100 even though the stated confirmation was 1110.

Classification: **old schema/prompt did not separate a confirmation gate from a
limit price**, plus model reasoning error. Python followed the explicit
structured order. It did not silently resize or change direction.

### A5. Time

The four old recheck reasons that claim the window was already closed at
09:31/09:33/09:35 ET are model reasoning errors. The old packet had mixed
UTC/offset fields and no single `time_state`; Python kept the actual window open
to 09:59:30. The correct design is to give the model one authoritative time
object and never let narrative time change execution permission.

### A6. Low capital utilization

The old 15-day account averaged 15.233% utilization and 84.767% cash. There is
no 10%/50% hard cap in the reconstructed execution path. MU chose 5 shares and
explicitly called it 0.4% equity risk; other days had no triggered order. The
main causes are model sizing/selectivity, one-decision-per-day review, small
candidate set, and no opportunity-quality allocation input. It is not valid to
infer a single Python rejection from the final cash balance.

## B. Changes

| Area | Old | v3 |
|---|---|---|
| Direction | Old asymmetric short cautions; only two setups | Neutral long/short comparison; `TREND_CONTINUATION`, `PULLBACK_RECLAIM`, `FAILED_RALLY`, `BREAKDOWN`, `EVENT_REPRICING`; no forced direction or minimum exposure |
| Time | Model inferred time | `time_state` with `decision_time_et`, `market_phase`, `trading_window_open`, `seconds_until_window_close`, `new_order_allowed`; Python is authoritative |
| Confirmation | Limit and narrative conflated | Separate `confirmation` ABOVE/BELOW, `limit_price`, `persistence=LATCHED/WHILE_VALID`; validator checks buy limit >= ABOVE level and sell limit <= BELOW level |
| Protection | Separate stop; no target group | One linked stop/take-profit group, shared quantity, partial-fill and gap handling; `PROTECT`/`REMOVE_PROTECTION` |
| Profit | One 09:31 position review | `ProfitTracker` at 5%/10% milestones, review-packet peak/giveback, original `position_theses`, and AI choice among HOLD/reduce/close/PROTECT |
| News | First 2 SEC + first 2 Assets; no bodies | Latest 3 news + 2 SEC + up to 12 diverse financial facts; news body excerpt 1,400 chars; per-symbol `selection_audit`; recheck packet includes current evidence |
| Risk | No explicit model-visible policy | `risk_policy` plus `DEFAULT_PAPER`/`AGGRESSIVE_PAPER`; aggressive profile disabled and unapproved |
| Validation | No versioned v3 validation | r3 inputs frozen and called; r4 corrected freeze prepared but not called |

New files: `src/exam_v3/*`, `prompts/decision_v3.txt`,
`config/aggressive_paper_v3.yaml`, `tests/test_decision_v3.py`,
`tools/validate_decision_v3*.py`, `DECISION_V3_ROOT_CAUSE.md`,
`EXAM15_INDEPENDENT_AUDIT.md`, this report.

`AGGRESSIVE_PAPER` is **disabled, unapproved, no leverage, no forced minimum**.
Its limits remain `null`; the user must approve exact values before activation.

## C. Regression results

- **337 passed, 1 third-party warning**.
- 35 v3 tests cover long/short/wait, confirmation vs limit, same-bar leakage,
  partial stop, linked target, short average/cover, profit hold/giveback,
  overnight protection, capital/duplicate/oversell, future injection, news
  selection, DST/time boundaries, PROTECT uniqueness and unapproved profiles.
- Three real DeepSeek validation calls were made against r3:
  - AMD premarket: **VALID**, `OPEN_LONG` 47 shares, confirmation above 630,
    limit 632, stop 585, target 720; this is a real-contract validation, not an
    order.
  - MRVL profit review: **FAILED contract shape** — returned HOLD + PROTECT for
    the same symbol; one decision per symbol is enforced.
  - LITE profit review: **FAILED contract shape** — PROTECT included `qty=5`;
    PROTECT must be the whole existing position and carries no qty/limit.
- r4 prompt corrected the one-decision/PROTECT rule; r4 is frozen at
  `data/decision_v3_validation_r4/` with `NOT_CALLED.md`.
- **No fourth DeepSeek call was made.** The three-call budget is exhausted.
- No broker/AI order execution occurred in validation.

## D. Old vs v3

| Dimension | Old 15-day | v3 |
|---|---|---|
| Short capability | 0 short trades; asymmetric prompt/setup/focus | Symmetric schema/prompt; not yet proven by a short AI output |
| Profit | Winners gave back to entry stops | 5/10% review, peak/giveback, original thesis, PROTECT |
| News | 96 SEC + 92 Assets, no body | Latest news bodies + SEC + diverse facts; TSM/QQQ facts may be absent |
| Intent | Narrative/limit mismatch | Confirmation separate, validator and tests |
| Time | 4 wrong window claims | One authoritative time state; boundary tests |
| Capital | 15.233% average utilization | No forced minimum; AMD validation sized 29.7% equity with explicit 2.2% risk |
| Execution | Old replay only 09:30–10:00; no profit loop | v3 deterministic engine with 10:00+ broker replay |
| Return | -2.526% | **No new return claim** |

The v3 validation is not an out-of-sample performance test and cannot be used
to claim improvement.

## E. Independent next exam

Proposed dates: **2026-08-17 through 2026-09-04**, 15 consecutive NYSE
sessions. These are outside the original 9/28–10/16 sample and outside the
three validation dates.

Pre-register before running:

- model `deepseek-flash`, version and provider-reported model;
- `prompts/decision_v3.txt` hash;
- `src/exam_v3` source hashes and `exam_v2.duckdb` hash;
- `AGGRESSIVE_PAPER` enabled/approved or DEFAULT_PAPER, all numeric limits;
- fee/slippage/borrow assumptions;
- max calls/tokens/cost and retry policy;
- no external web, no production research memory;
- no Alpaca credentials in the exam process;
- freeze all inputs before the first call.

The run should be a full 15-session simulation with 09:00 premarket,
09:30–10:00 monitoring/rechecks, after-10:00 broker replay, daily state carry,
and complete audit. It must not be started until the user approves the profile
and budget.

## Six core answers

1. **Why no shorts?** Old prompt/setup/focus design was asymmetric; 0/95 scan
   candidates were OPEN_SHORT; no Python short rejection exists. The exact
   model motive is UNKNOWN.
2. **Why did profits become losses?** The old system had no 5/10% profit review
   and only one 09:31 review; MRVL/LITE peaked after that review; TSM peaked
   outside the local window; stops stayed at the original levels.
3. **Why did news/fundamentals not reach the AI?** The old selector took the
   first two SEC/Assets records rather than the latest news/diverse facts; the
   database had thousands of news items.
4. **Why did narrative and execution differ?** Old schema conflated a
   confirmation level with a limit price; MU used trigger 1100 vs limit 1110.
5. **Why wrong time?** Old prompt forced the model to infer time from mixed
   fields; four reasons were wrong. v3 supplies authoritative `time_state`.
6. **Why idle cash?** Model sizing and selectivity, one daily decision, small
   candidate set, and no opportunity-based allocation input; no evidence of a
   blanket 10%/50% limit. v3 does not force trades.

## Limits

- `EXPLORATORY` historical news/fact versions remain `UNKNOWN`; IEX is not SIP.
- The current watchlist was selected retrospectively.
- Model pretraining may contain later knowledge.
- The 15-day sample is a failure-discovery regression, not a performance proof.
- r3 validation has two contract-shape failures; r4 is not model-validated.
- No real/Paper order was submitted.
