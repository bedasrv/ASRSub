# Rust port parity checklist

**Question this answers:** when is `legacy/` safe to delete?
**Answer so far:** not yet — see the gaps below.

## Method

- Legacy oracle: `legacy/tests/` (45 files, ~293 cases) + `legacy/pipeline/dry_tests.py`
  (109 cases) ≈ **~400 pinned behaviors**, inventoried 2026-09-08.
- Rust coverage: 37 unit `#[test]` + 1 `#[tokio::test]` sim (25 asserts across
  phases A/B/C/D) + 4 binary-boundary integration tests.
- Each row maps one legacy behavior area to its Rust status. Live contracts
  remain `README.md` + `docs/HEALTH.md`; `docs/PLAN.md` is provenance only.

## Legend

| Status | Meaning |
|---|---|
| COVERED | Rust tests pin the same behavior |
| PARTIAL | Core ported, edge cases untested |
| GAP | Behavior exists in Rust but is untested, or unverified |
| DIVERGED | Deliberately changed or removed in the port |
| ABSENT | Legacy feature with no Rust counterpart at all |

## Checklist

| # | Area | Legacy source (cases) | Rust coverage | Status | Notes |
|---|---|---|---|---|---|
| 1 | Release contract (compose/build/deploy/rollback docs) | `test_immutable_release_contract` (18), `test_release_descriptor_execution` (3) | Scripts unchanged and still used; Rust side has 4 CLI boundary tests only, no compose/build assertions | PARTIAL | Behavior preserved, enforcement missing. Low risk. Port the text-shape assertions or waive. |
| 2 | Jimaku client (ranking, 429, cache, AniList) | `test_jimaku_api` (32) | 13 tests in `src/jimaku.rs`: shapes, pick_entry, full rank corpus, cache-key pin, stub-server auth/params/429/snippet/download/anilist-hit-miss-failure | COVERED | Resolved 2026-09-08. Fixed en route: unexpected shape now errors (was silent empty), HTTP errors carry status+snippet, 429 carries reset_after, download uses legacy `.part` naming + cleans up on failure, cache merge writes `fetched_at`. Recorded folds: typed `JimakuRateLimited` → anyhow message (no caller branches); strict `S##E##` tag kept over loose regex (pre-existing, reasoned); `download()` returns `()` (callers use the known dest); `JIMAKU_API_KEY` env fallback lives in config layer. |
| 3 | Jimaku hunt scheduling (budget, backoff, tombstone, state-file) | `test_jimaku_hunt` (43) | Middle path: per-stem 24h retry cooldown (`jimaku_tried` now maps stem→Instant; restart retries everything), predicate unit-tested | COVERED | Resolved 2026-09-08. Decision, not a port: budgets unnecessary (global 500ms pacing guards the rate limit by construction), 429s miss into the next cooldown, tombstones pointless with no upgrade pass (no Sonarr-404 orphans; a hopeless stem costs ~2 calls/day), flat-daily lands uploads within ~24h like the capped ramp did. Rationale recorded on `JIMAKU_RETRY_COOLDOWN`. |
| 4 | Registry core (upsert/delete/reconcile/locking) | reconciliation ×3 files (7), `test_registry_lock_and_tmp_ladder` (3), `test_registry_write_serialization` (1) | `src/state.rs` (4): roundtrip, consume, delete scoping, kind | PARTIAL | Hash-trust cases (~10) are obsolete by design (hashes removed in `2d4afd6`). fd-lock serialization untested. |
| 5 | Language aliases/compat (jpn/jp/ind, canonical rows) | adversarial 1–7 + compat + alias_gaps (~30) | `src/lang.rs` (3), delete scoping, `m:/e:` routing | PARTIAL | Core normalization covered; stale-row/legacy-alias adoption ladders untested. |
| 6 | HI/canonical policy, delete + upgrade sidecars | `test_permanent_subtitle_policy_red` (26), `test_delete_upgrade_sidecar` (19) | Sim A/B (uploads `[en,id]`, marker), `replaceable_lists_canonical_first` | PARTIAL | Untested: HI-twin delete globs, upgrade-on-changed-JPN, forced-sibling preservation. `actions.rs` has zero tests. |
| 7 | Webhook durability (SQLite ledger, claim/lease/token fencing, auth) | `test_webhook_durability_red` (17), `test_webhook_remediation_tdd` (15), `test_remaining_remediation_tdd` (7) | Auth restored on `/webhook` (either sender header, 401 otherwise) + validate-before-wake + in-flight dedup; ledger intentionally absent (filesystem-as-ledger: sidecars adopted via ladder guards) | COVERED | Resolved 2026-09-08. OPS: Tdarr/Sonarr notification must send the key header. Remaining waiver: lease-fencing/dupe semantics replaced by simpler dedup — recorded here. |
| 8 | Retime/align | `test_retime_safety` (10), dry_tests retime (~25) | INTENTIONALLY REMOVED (`RETIME_*`/`ALIGN_*` keys pruned) | DIVERGED | Rationale: Japanese keeps raw timing, external trusted as-is. Needs explicit sign-off, then waive. |
| 9 | Timeline gate (pileup/hole/shrink/monotonicity) | `test_timeline_gate` (8), marker-guard + recalc-order (2) | `src/srt.rs` (9): parse, marker, clamp, CPS merge, sanitize, split — gate specifics untested | PARTIAL | Port the 8 gate cases; they are small and pure. |
| 10 | Translation guards/stitch/fallback/source-language | lirik_fix (4), source-language (5), dry_tests merge/echo/tail/per-line (~8) | `src/translate.rs` (4) + marker filter | PARTIAL | Untested: prompt carries actual source language, per-line fallback language preservation. |
| 11 | ASR track selection + embedded sweep roots | `test_embedded_target_languages` (5), `test_embedded_srt_sweep_root` (4) | `src/asr.rs` (3); webhook `extract_embedded` exists, untested | PARTIAL | NAS-mount root preference logic — verify it was ported at all. |
| 12 | Control API semantics (stale-done, false positives, wanted/library) | stale_done (2), false_positive_paths (3), source_target_registry (1), dry_tests status/library/activity (~8) | `src/api.rs` (3): routing + activity shape; sim asserts state rows | PARTIAL | Untested: stale-done hiding, late-marker claim guards, Bazarr-alias normalization in wanted. |
| 13 | Actions retry/skip round-trip | action_normalization (1), dry_tests actions/state (~8) | Consume unit test + sim Phase C | COVERED | Closest to full. Remaining: language-scoped delete via actions. |
| 14 | In-memory job queue | `test_job_registry` (9) | Loop daemon has no queue; no counterpart | DIVERGED | Verify nothing external (dashboard) depends on queue semantics, then waive. |
| 15 | Readiness (media check → not-ready) | `test_readiness` (1) | `health` is config-only | GAP | One-line port. Low risk. |
| 16 | Glossary v2/knowledge-block/cast | dry_tests glossary/knowledge/cast (~5) | `src/glossary.rs` (2): flat-map migration, fallback | PARTIAL | Knowledge-block rendering + cast resolution untested. |
| 17 | Upload edge cases (204 vs differing sidecar, movie retry codes) | upload_collision (1), dry_tests upload (2) | Sim asserts upload set only | GAP | Small ports, real corruption-guard value. |
| 18 | Movies end-to-end | Scattered (~10): candidates, library, fileless recovery | Kind roundtrip only; sim is series-only | GAP | Add a movie phase to `src/sim.rs` or port key cases. |
| 19 | Regen candidate pass | dry_tests regen (~8) | No regen pass found in `src/` | GAP | Verify: intentionally dropped? If yes, mark DIVERGED with rationale; if no, port. |
| 20 | Benchmarks/eval harness | `legacy/benchmarks/` (7 scripts) | No equivalent | DIVERGED | Local-model evals are obsolete under remote-only inference — but no remote-provider quality eval exists either. Decide: build or waive. |

## Port queue (highest value first)

1. Row 7 — verify webhook auth/dedup threat model; port or fix. ✅ done 2026-09-08
2. Row 2 — port `test_jimaku_api` ranking/error/cache cases to `src/jimaku.rs`. ✅ done 2026-09-08
3. Row 3 — decision: accept hunt simplification (waive with rationale) or re-add backoff. ✅ done 2026-09-08 (middle path: 24h retry cooldown)
4. Row 18 — movie phase in `src/sim.rs`.
5. Rows 9, 12, 10 — small pure-function ports (timeline gate, API semantics, source-lang).
6. Rows 6, 17 — `actions.rs`/`episode.rs` edge tests (HI globs, upgrades, upload guards).
7. Rows 19, 14, 4-locking, 11-roots, 15, 16 — verify-and-waive or port.
8. Rows 1, 8, 20 — sign-off decisions (release assertions, retime removal, eval strategy).

## Deletion criterion for `legacy/`

Delete the working-tree copy when every row above is COVERED, DIVERGED-with-rationale,
or explicitly waived — and the waivers are recorded in this file. Git history
preserves the code regardless; this file preserves the *decisions*.
