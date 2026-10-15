# Verification checklist (2026-10-15)

**Method.**
- `py_compile` was run on all modules.
- All 21 test files (`ls test_*.py`) were run in parallel, each with a fresh STATE_DIR.
- Replay (1,100 min), mockstream (600 min) and the DOUBLE_CONFIRM on/off replays were run.
- Every command and menu button was simulated (test_all_commands, test_menu, test_tz_commands).

**Result: all PASS.**

| Area | Test | Result |
|---|---|---|
| Restart mid-day keeps day stats; STATE_DIR warning | test_restart_midday | PASS |
| /reset scopes, confirm, backup | test_reset | PASS |
| Public session 10:30, close rules, safety stops, restart | test_public_cap | PASS |
| Bug group always on, cards, summary | test_bugsignals | PASS |
| Shadow fix (L4 + single B/C), auto cap 4 | test_ensemble_levels_bugs | PASS |
| /sched (all types, events, rate limit, errors) | test_sched | PASS |
| :57 never skip (CONFIRM / SWITCH ≥ 0.8 ATR) | test_channels_stats, test_resolver | PASS |
| History gate (all/bugs/off), backfill, tiers | test_history | PASS |
| Paced Tier-2 fill (1 per 15 min, Tier-1 head start, no blocked-minute catch-up) | test_history, test_filters | PASS |
| Loss-streak cooldown (live and bug separately, ties, persistence) | test_filters | PASS |
| Blackouts (DST summer/winter/mismatch week) and news (mocked feed, cache, one alert) | test_filters | PASS |
| Double confirmation (strict and fill), /confirm2, menu | test_filters | PASS |
| Pattern family P (detectors, causality, mandatory history, admin-only until 20 fwd ≥ BE, P13 2m expiry, P11 30s, /patterns, menu, /best) | test_patterns | PASS |
| Daily loss stop (5 net) and profit alert +15 / close +20 (live and bug separately, 08:00 UTC+3 trading day, public session closing post, persistence) | test_extras | PASS |
| Payout re-check at send time (ensemble and bug group) | test_extras | PASS |
| Filter precedence (/filters) | test_extras | PASS |
| Weekly report and self-tune (Sunday 03:00; hard blocks never loosened) | test_extras | PASS |
| Volume mode / Tier 3 (10-min pace, cap 6/h, 1 per pair per 10 min, UB ≥ BE only, hard blocks, admin-only tier tag, safeguard alert at 40 trades, /volume + menu + persistence, /stats /best weekly) | test_volume | PASS |
| Quiet drop counters + /verbose; summary header shows budget day (03:00) and trading day (08:00) separately | test_filters, test_patterns, test_history | PASS |
| All commands and menu buttons | test_all_commands, test_menu, test_tz_commands | PASS |
| Stream, recovery, modes, bench, auto level | remaining tests | PASS |
| Replay / mockstream end-to-end | runall | rc=0 |

**Simulation files in `sim/`.**
- Ablation table: `ablation_results.txt`.
- Volume-mode simulation: `volume_results.txt` (`volume_sim.py`).
- Pattern backtests: `pattern_backtest_fx.txt` and `pattern_backtest_synthetic.txt`.
- `frequency_results.txt` holds the first fill-tuning grid. It was produced before the final pacing tweak (slot wait 6→3 and the blocked-minute cap). `ablation_results.txt` reflects the final logic.
| test_shadow_channel.py | shadow routing, :57 line, summary section, routing line, startup check, unset fallback |
| test_warmup.py | bug-group warm-up thresholds, 4/h cap, bug-group-only, admin tag, /history days |
| test_feedback.py | feedback jsonl store, rotation, /feedback, export CSV+JSON, menu, /reset feedback + backup |
| test_payout_floor.py | NZDUSD 58%/unknown/stale → blocked at candidate + send time, no default payout, refresh while paused, payout in feedback log |
