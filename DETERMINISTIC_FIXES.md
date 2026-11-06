# Deterministic executor fixes (12-item acceptance matrix)

The legacy `full15` directory is retained as **AUDIT_FAIL_EXPLORATORY**. It is
not rewritten and its -2.526% is not treated as a post-fix result.

| # | Defect reproduced from old audit | Root cause / code | Fix / evidence | Regression test | Impact on old score |
|---|---|---|---|---|---|
| 1 | 96 SEC + 92 Assets reached AI despite 3,249+ news | `exam_replay/full.py::_news_and_facts` first-two traversal | v3 `select_evidence`: latest 3 news, 2 SEC, 12 diverse facts, body excerpt; r4 input source audit | `test_newest_news_and_financial_diversity`; r4 audit `True` | Old input remains wrong; old score not comparable |
| 2 | 26 intraday quotes with `end_at == as_of` | old `_quote` used just-completed bar; no 30s buffer | v3 `get_v2_snapshot` + `quote.available_at` + `Engine` publish at end+30s; recursive audit | `findings quote_buffer_violations` r4 = 0; `test_recursive_integrity...` | Could change old decisions; not replayed |
| 3 | 985 history objects used `end_at` as `ts` | old `_history.compact` aliases start/end | v3 `packed()` keeps start/end/available; audit checks end>start and availability | `test_recursive_integrity...`; r4 audit no end<=start | Old history invalid; old score retained only as fail |
| 4 | 10/10 old checks were weak self-assertions | old `verify_exam.py` scanned names/strings | `exam_v3.integrity.audit_payload` recursively checks bars/news/facts/account/quote and `audit_source` matches DuckDB rows | `test_source_rows_match_duckdb...`; r4 `audit_source` 158/176/181 rows True | Old acceptance cannot be used |
| 5 | add short at 100 then 120 -> avg 120 not 110 | old `ExamBroker._apply_fill` `max(0, prior)` | v3 `Broker.fill` uses `abs(prior)` and signed position | `test_short_average_cover_release` | No old short trades; no return effect, but executor untrusted |
| 6 | short stop cover left `short_proceeds=2200` | old release only in `COVER_SHORT` | v3 `fill` releases proceeds for any buy-to-close; test checks both empty | `test_short_average_cover_release` | No old short exposure; no return effect |
| 7 | 100 long, 40 stop -> stop qty 60, remaining 20 | old `_sync_stop_qty` overwrote qty | v3 linked group uses shared protected quantity and partial fills; test | `test_partial_protection_shared_quantity_and_gap`, `test_partial_close_keeps...` | No partial old fills; still critical |
| 8 | order created 09:40 filled on 09:31 bar | old `ExamBroker.process_bar` no `submitted_at` guard | v3 `start<o.at` guard | `test_same_bar_cannot_fill_new_decision` | No old fill contradicts, but interface unsafe |
| 9 | limit slippage and gap mislabeled | old fills had only `slippage` | v3 fill records `price_vs_limit_per_share`, `slippage_vs_open_per_share`, `gap_cost`, fee; equity uses actual fill | `test_fill_cost_components...` | Old cost attribution wrong; old return cash still reconciles |
| 10 | QQQ start 08:21, not 09:30 | old benchmark took first minute | `evaluation.qqq_session_prices` uses 09:30 open, 16:00 close; `exposure_matched` uses intraday exposure points | `test_qqq_benchmark_uses_regular_session...` | Old QQQ comparison not comparable; old retained |
| 11 | `NONE` vs `BROKER_STOP` conflict; guard not explicit | old packet contract vs nested stop policy | v3 execution contract lists linked protection, explicit short locate; system prompt contains historical guard; test | `test_prompt_contains_historical_guard...`, `test_native_linked_request...` | Old contract invalid; old score unchanged |
| 12 | incomplete freeze | old freeze omitted deps/evaluator | r4 freeze includes all `exam_v3` files, prompt, config, validator, V2 snapshot/store, `requirements.lock.txt`, `pyproject.toml`, DB hash | `freeze.json` source list; no call | Old freeze remains historical only |

## Deterministic replay status

- Old 15-day outputs are **not replayed as a new score**.
- The old raw model outputs cannot be converted to v3 decisions without changing
  their schema or interpreting them; doing so would be a new model result.
- v3 r3 validation: 1 valid, 2 contract-shape failures. No order was executed.
- v3 r4 freeze is prepared but **not called**; the three-call budget is spent.
- 337 offline tests pass; source audit of r4 inputs passes.

## What this does and does not prove

It proves the deterministic executor defects in the matrix have regression
coverage and that the frozen r4 inputs match the source database. It does **not**
prove that the repaired prompt improves returns, short selection or capital
utilization. Those require the independent exam, which has not been started.
