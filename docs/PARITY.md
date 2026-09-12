# Rust port parity checklist

**Question this answers:** when is the pre-rewrite Python safe to delete?
**Answer:** the checklist is complete (2026-09-08) — every row is COVERED,
DIVERGED-with-rationale, or explicitly waived. The rewrite-branch `legacy/`
copy was deleted the same day (criterion met); the remaining Python on
`main` was removed 2026-09-09 when the Rust tree landed there. The two
release-contract suites were relocated to `tests/` and stay green.
Reference code survives in git history (`main` history holds the old
root-level files, e.g. `git show <sha>:orchestrator.py`; the `legacy/`
tree survives on the `backup/pc-208-20260907` branch); this file preserves
the *decisions*.

## Method

- Legacy oracle: `legacy/tests/` (45 files, ~293 cases) + `legacy/pipeline/dry_tests.py`
  (109 cases) ≈ **~400 pinned behaviors**, inventoried 2026-09-08.
- Rust coverage **at the time of the port**: 72 unit `#[test]` (incl. the
  full-program sim, 40+ asserts across phases A–G) + 4 binary-boundary
  integration tests + 21 stdlib-unittest release-contract cases. The tree has
  grown since: 108 unit tests + 4 integration tests (`cargo test`; `README.md`
  quotes the same pair) and 53 release-contract cases (`python3 -m unittest
  tests.test_immutable_release_contract tests.test_release_descriptor_execution`).
  This file records the port decision, not the current counts.
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
| 1 | Release contract (compose/build/deploy/rollback docs) | `test_immutable_release_contract` (18), `test_release_descriptor_execution` (3) | Oracle repaired AND live: fixed stale `legacy/`-relative paths (reorg breakage), evolved 2 Python-era pins to the rewrite contract (no huggingface mount / nvidia runtime — remote-only; `command: [daemon]`); 2026-09-09 evolved again to the GHCR CI contract (CI builds + pushes full-SHA tags, pull-based deploy, `deploy.sh` deleted); **21/21 green at the time of the port; 53 cases today** | COVERED | Resolved 2026-09-08, re-evolved 2026-09-09. Run: `python3 -m unittest tests.test_immutable_release_contract tests.test_release_descriptor_execution`. |
| 2 | Jimaku client (ranking, 429, cache, AniList) | `test_jimaku_api` (32) | 13 tests in `src/jimaku.rs`: shapes, pick_entry, full rank corpus, cache-key pin, stub-server auth/params/429/snippet/download/anilist-hit-miss-failure | COVERED | Resolved 2026-09-08. Fixed en route: unexpected shape now errors (was silent empty), HTTP errors carry status+snippet, 429 carries reset_after, download uses legacy `.part` naming + cleans up on failure, cache merge writes `fetched_at`. Recorded folds: typed `JimakuRateLimited` → anyhow message (no caller branches); strict `S##E##` tag kept over loose regex (pre-existing, reasoned); `download()` returns `()` (callers use the known dest); `JIMAKU_API_KEY` env fallback lives in config layer. |
| 3 | Jimaku hunt scheduling (budget, backoff, tombstone, state-file) | `test_jimaku_hunt` (43) | Middle path: per-stem 24h retry cooldown (`jimaku_tried` now maps stem→Instant; restart retries everything), predicate unit-tested | COVERED | Resolved 2026-09-08. Decision, not a port: budgets unnecessary (global 500ms pacing guards the rate limit by construction), 429s miss into the next cooldown, tombstones pointless with no upgrade pass (no Sonarr-404 orphans; a hopeless stem costs ~2 calls/day), flat-daily lands uploads within ~24h like the capped ramp did. Rationale recorded on `JIMAKU_RETRY_COOLDOWN`. |
| 4 | Registry core (upsert/delete/reconcile/locking) | reconciliation ×3 files (7), `test_registry_lock_and_tmp_ladder` (3), `test_registry_write_serialization` (1) | `src/state.rs` (6): roundtrip, consume, delete scoping, kind, **concurrent-appends serialization**, verified-targets | COVERED | Resolved 2026-09-08. Hash-trust/reconcile-by-hash obsolete by design (no hashes). Lock upgrade/tmp-ladder semantics replaced by fd-lock + atomic renames (pinned by the concurrency test). |
| 5 | Language aliases/compat (jpn/jp/ind, canonical rows) | adversarial 1–7 + compat + alias_gaps (~30) | `src/lang.rs` (3) + delete scoping + `m:/e:` routing + `missing_of` alias test + alias-adoption test (`sidecar_exists` jpn↔ja) + stale-row handling (`verified_targets`) | COVERED | Resolved 2026-09-08. Adversarial ladders that assumed the upgrade/reconcile passes are moot (no such passes); the behaviors that survive — normalization, canonical rows, stale distrust, alias adoption — are each pinned. |
| 6 | HI/canonical policy, delete + upgrade sidecars | `test_permanent_subtitle_policy_red` (26), `test_delete_upgrade_sidecar` (19) | HI-twin globs implemented + unit-tested (`replaceable_target_sidecar_paths`); delete path exercised every sim retry phase; hash-trust/retime-gate cases obsolete by design (no hashes, no retime) | COVERED | Resolved 2026-09-08. Upgrade-on-changed-JPN waived: without content hashes there is no change signal, and an adequate ASR sidecar is by definition good enough — a quality nicety, not correctness. Revisit if hashes ever return. |
| 7 | Webhook durability (SQLite ledger, claim/lease/token fencing, auth) | `test_webhook_durability_red` (17), `test_webhook_remediation_tdd` (15), `test_remaining_remediation_tdd` (7) | Auth restored on `/webhook` (either sender header, 401 otherwise) + validate-before-wake + in-flight dedup; ledger intentionally absent (filesystem-as-ledger: sidecars adopted via ladder guards) | COVERED | Resolved 2026-09-08. OPS: Tdarr/Sonarr notification must send the key header. Remaining waiver: lease-fencing/dupe semantics replaced by simpler dedup — recorded here. |
| 8 | Retime/align | `test_retime_safety` (10), dry_tests retime (~25) | INTENTIONALLY REMOVED (`RETIME_*`/`ALIGN_*` keys pruned) | DIVERGED | Signed off 2026-09-08. Rationale: Japanese keeps raw timing (ASR or Jimaku-direct, both already aligned to the video), external trusted as-is; the retime path existed to fix Bazarr-provider timing drift for a different source mix. If mistimed-external reports arrive, revisit — the legacy module is in git history. |
| 9 | Timeline gate (pileup/hole/shrink/monotonicity) | `test_timeline_gate` (8), marker-guard + recalc-order (2) | `src/srt.rs` (5 new): pileup>2, gap>30%, coverage<80%, monotonicity, healthy±duration — legacy thresholds as consts; wired at the episode call site with a once-per-episode duration probe | COVERED | Resolved 2026-09-08. Host-local backup-file case not portable (noted in test). Recalc-order/timeline-recalc cases moot: no recalc pass exists (see row 8 direction). |
| 10 | Translation guards/stitch/fallback/source-language | lirik_fix (4), source-language (5), dry_tests merge/echo/tail/per-line (~8) | `src/translate.rs` (6): guards, stitch order/oversize, prompt source pin, empty-on-persistent-failure | COVERED | Resolved 2026-09-08. `display_source_lang` extracted (ladder/ASR domain is exactly ja/en, so the mapping is exact, now pinned). "Fallback preserves source language" meant the `source_lang` parameter propagates through per-line retries (pinned), with `""` output on persistent failure (pinned) — not source-text output. |
| 11 | ASR track selection + embedded sweep roots | `test_embedded_target_languages` (5), `test_embedded_srt_sweep_root` (4) | `src/asr.rs` (3); webhook `extract_embedded` + `map_path` pinned; sweep-root chain waived (no sweep feature — webhook paths are sender-provided, `map_path` is the only mapping) | COVERED | Resolved 2026-09-08. Embedded-adoption semantics (canonical outputs, no false missing) hold via `sidecar_exists` replaceable variants, pinned in row 5. |
| 12 | Control API semantics (stale-done, false positives, wanted/library) | stale_done (2), false_positive_paths (3), source_target_registry (1), dry_tests status/library/activity (~8) | Fixed + pinned: `discover` verifies done rows against registry targets on disk (`verified_targets`, unit-tested) for series AND movies — feeds run_pass, wanted, and library together; sim Phase F proves stale-done self-heals end-to-end; Bazarr alias normalization pinned (`missing_of` test) | COVERED | Resolved 2026-09-08. Late-marker pathless-row guard waived: nothing reads the registry for claim decisions (provenance display only), so there is no claim path for a marker to hijack — marker handling is cue-level and tested. |
| 13 | Actions retry/skip round-trip | action_normalization (1), dry_tests actions/state (~8) | Consume unit test + sim Phase C | COVERED | Language scoping (`null` = whole episode, else explicit list) applies to both retry and delete; retries additionally reprocess inline the same pass (resolved from Sonarr/Radarr, skips/exclusions win). |
| 14 | In-memory job queue | `test_job_registry` (9) | No counterpart: loop daemon uses semaphores; dashboard queue widgets degrade gracefully (`data.queue \|\| {}`, empty job list); `current` shows the in-flight pass | DIVERGED | Resolved 2026-09-08. Rationale: the queue existed to schedule webhook workers; with wake+extract inline there is nothing to schedule. |
| 15 | Readiness (media check → not-ready) | `test_readiness` (1) | Adapted: `/status` now carries additive `media_ok` (NAS root present), pinned by a wiring test — a dead mount no longer looks like "idle" | COVERED | Resolved 2026-09-08. Full btrfs/ledger/paused-boot gates belonged to the retired Python boot (no paused boot, no ledger DB here); container healthcheck still hits `/health`. |
| 16 | Glossary v2/knowledge-block/cast | dry_tests glossary/knowledge/cast (~5) | `src/glossary.rs` (3): flat-map migration, few-match fallback, **knowledge-block rendering** (matched cast, aliases, kinds, empty-for-unknown) | COVERED | Resolved 2026-09-08. |
| 17 | Upload edge cases (204 vs differing sidecar, movie retry codes) | upload_collision (1), dry_tests upload (2) | NEW sim Phase G: 2×500→204 delivers exactly 3 attempts and completes; all-500 still completes (sidecar authoritative); shared 204/429/5xx policy covers series+movies identically (legacy movie test asserted exactly this sequence) | COVERED | Resolved 2026-09-08. Post-204 read-back waived: we upload bytes just written to the canonical sidecar, so there is no legacy-alias collision state to detect; staleness is caught at `discover` instead (row 12). Misleading `bazarr.rs` doc fixed to match. |
| 18 | Movies end-to-end | Scattered (~10): candidates, library, fileless recovery | NEW sim Phase E: Radarr movie discovered/transcribed/uploaded, `kind: movie` state+registry rows, series/movie id-100 collision proven | COVERED | Resolved 2026-09-08. Fileless-movie recovery has no counterpart (no sweep feature — see row 11); movie upload retry codes folded into the shared 204/429/5xx policy (see row 17). |
| 19 | Regen candidate pass | dry_tests regen (~8) | No counterpart — intentionally absent (`config.rs` documents `REGEN/` as dropped); the `retry` action covers the need (clears state + deletes sidecars → Bazarr re-wants → reprocessed) | DIVERGED | Resolved 2026-09-08. |
| 20 | Benchmarks/eval harness | `legacy/benchmarks/` (7 scripts) | No equivalent | DIVERGED | Decided 2026-09-08: waived as a parity item. The harness scores LOCAL models (faster-whisper, NLLB, COMET, Gemma FLORES) that cannot run in this deployment (no GPU, remote-only by design). Remote-provider quality monitoring is an unbuilt capability, not a port gap — file it as new-feature work if wanted, not here. |

## Port queue (highest value first)

1. Row 7 — verify webhook auth/dedup threat model; port or fix. ✅ done 2026-09-08
2. Row 2 — port `test_jimaku_api` ranking/error/cache cases to `src/jimaku.rs`. ✅ done 2026-09-08
3. Row 3 — decision: accept hunt simplification (waive with rationale) or re-add backoff. ✅ done 2026-09-08 (middle path: 24h retry cooldown)
4. Row 18 — movie phase in `src/sim.rs`. ✅ done 2026-09-08 (Phase E + stale-heal Phase F)
5. Rows 9, 12, 10 — small pure-function ports (timeline gate, API semantics, source-lang). ✅ done 2026-09-08
6. Rows 6, 17 — `actions.rs`/`episode.rs` edge tests (HI globs, upgrades, upload guards). ✅ done 2026-09-08 (sim Phase G; upgrades + read-back waived with rationale)
7. Rows 19, 14, 4-locking, 11-roots, 15, 16 — verify-and-waive or port. ✅ done 2026-09-08
8. Rows 1, 8, 20 — sign-off decisions (release assertions, retime removal, eval strategy). ✅ done 2026-09-08

## Deletion criterion for the pre-rewrite Python — MET 2026-09-08, deleted same day; `main` cleared 2026-09-09

~~Delete the working-tree copy when~~ every row above is COVERED,
DIVERGED-with-rationale, or explicitly waived — and the waivers are recorded
in this file. Git history preserves the code regardless (`main` history
holds the old root-level files; the `legacy/` tree is on the
`backup/pc-208-20260907` branch); this file preserves the *decisions*. The
release-contract oracle moved to `tests/test_immutable_release_contract.py`
+ `tests/test_release_descriptor_execution.py` (run: `python3 -m unittest
tests.test_immutable_release_contract tests.test_release_descriptor_execution`).
