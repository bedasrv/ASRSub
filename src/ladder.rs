//! Ladder text sources: adequate on-disk sidecars or Jimaku direct.
//!
//! Both skip remote ASR entirely, which is the cheapest win in the pipeline:
//! a suitable ja/en sidecar (embedded extract, prior Jimaku fetch, or an
//! earlier pass's raw source) translates straight from text. Adequacy gates
//! mirror the Python ladder (cue/char minimums, CJK fraction, span vs
//! container duration); the Jimaku candidate resolves via the shared
//! AniList cache, downloads once per stem per daemon lifetime, and persists
//! the raw source for idempotent reuse.

use std::path::Path;

use crate::asr;
use crate::lang::{normalize_lang, sidecar_paths};
use crate::pipeline::Pipeline;
use crate::srt::{self, Cue};

/// A cheap text source for one language: on-disk sidecar or Jimaku direct.
/// `source` is the registry provenance (`jpn`/`eng`); `source_kind` is
/// `external` for these paths (the embedded sweep is a separate writer).
#[derive(Debug, Clone)]
pub(crate) struct LadderHit {
    pub(crate) cues: Vec<Cue>,
    pub(crate) src_lang: String,
    pub(crate) source: String,
    pub(crate) source_kind: Option<String>,
}

/// Ladder lookup key: everything `ladder_source` needs beyond `&self`.
/// A struct (not 7 positional args) so call sites stay readable.
pub(crate) struct LadderQuery<'a> {
    pub(crate) media_path: &'a str,
    pub(crate) stem: &'a str,
    pub(crate) target: &'a str,
    pub(crate) series_title: &'a str,
    pub(crate) season: Option<i64>,
    pub(crate) episode: i64,
    pub(crate) is_movie: bool,
}

impl Pipeline {
    /// Ladder text source: adequate on-disk ja/en sidecar, else a Jimaku
    /// direct candidate (series episodes only). Both skip ASR entirely.
    ///
    /// Adequacy gates mirror the Python ladder: minimum cues/chars, minimum
    /// CJK fraction for Japanese, and a subtitle span within tolerance of the
    /// container duration when ffprobe succeeds.
    pub(crate) async fn ladder_source(&self, q: LadderQuery<'_>) -> Option<LadderHit> {
        // ja source for id/en targets; en source additionally for en targets.
        let src_langs: &[&str] = if normalize_lang(q.target) == "en" {
            &["ja", "en"]
        } else {
            &["ja"]
        };
        for src in src_langs {
            for p in sidecar_paths(q.stem, src) {
                if !Path::new(&p).is_file() {
                    continue;
                }
                let Ok(text) = tokio::fs::read_to_string(&p).await else {
                    continue;
                };
                let cues = srt::parse_srt(&text);
                if !self.adequate(&cues, src, q.media_path).await {
                    continue;
                }
                // Strip a leading AI-marker cue so it is never translated.
                let cues: Vec<Cue> = cues
                    .into_iter()
                    .filter(|c| !srt::has_ai_marker_text(&c.text))
                    .collect();
                if cues.is_empty() {
                    continue;
                }
                let src_lang = if normalize_lang(src) == "en" {
                    "en".to_string()
                } else {
                    "ja".to_string()
                };
                let source = if src_lang == "en" {
                    "eng".to_string()
                } else {
                    "jpn".to_string()
                };
                return Some(LadderHit {
                    cues,
                    src_lang,
                    source,
                    source_kind: Some("external".to_string()),
                });
            }
        }
        // Jimaku direct (series only): attempted at most once per
        // JIMAKU_RETRY_COOLDOWN per stem; ~3 calls per attempt, paced
        // globally far below the 25 req/min limit.
        if !q.is_movie
            && self.cfg.jimaku_direct_enabled
            && self.jimaku.enabled()
            && q.episode > 0
            && !q.series_title.is_empty()
            && q.series_title != "?"
        {
            if let Some(hit) = self
                .jimaku_candidate(q.series_title, q.season, q.episode, q.stem, q.media_path)
                .await
            {
                return Some(hit);
            }
        }
        None
    }

    /// Direct Jimaku REST candidate: AniList id (cache-first) -> entry search
    /// -> per-episode file listing -> rank -> download -> adequacy gate.
    /// The raw Japanese source lands in `{stem}.ja.hi.srt` (no-clobber) so
    /// future passes reuse it without network. Misses become re-eligible
    /// after `JIMAKU_RETRY_COOLDOWN` (a sub uploaded next week must still
    /// land: Bazarr never revisits our `manual` ASR uploads on its own).
    /// Never raises.
    async fn jimaku_candidate(
        &self,
        series_title: &str,
        season: Option<i64>,
        episode: i64,
        stem: &str,
        media_path: &str,
    ) -> Option<LadderHit> {
        {
            let mut tried = self.jimaku_tried.lock().unwrap_or_else(|e| e.into_inner());
            let now = std::time::Instant::now();
            // Stamped at attempt start (also dedups concurrent workers on
            // the same stem); a miss therefore costs one cooldown, not a
            // hot loop, and a restart retries everything.
            if !jimaku_retry_due(tried.get(stem).copied(), now) {
                return None;
            }
            tried.insert(stem.to_string(), now);
        }
        let tag = match season {
            Some(s) => format!("S{s:02}E{episode:02}"),
            None => format!("E{episode}"),
        };
        let anilist_id = self.jimaku.resolve_anilist_id(series_title).await;
        let Some(anilist_id) = anilist_id else {
            tracing::info!("ladder: jimaku direct: no AniList id for '{series_title}' {tag}");
            return None;
        };
        let entries = match self.jimaku.search_by_anilist(anilist_id).await {
            Ok(e) => e,
            Err(e) => {
                tracing::warn!("ladder: jimaku direct: entry search failed ({tag}): {e:#}");
                return None;
            }
        };
        let entry = crate::jimaku::pick_entry(entries, anilist_id);
        let entry_id: i64 = entry?.get("id")?.as_i64()?;
        let files = match self.jimaku.list_files(entry_id, Some(episode)).await {
            Ok(f) => f,
            Err(e) => {
                tracing::warn!("ladder: jimaku direct: file list failed ({tag}): {e:#}");
                return None;
            }
        };
        let ranked = crate::jimaku::rank_files(files);
        let best = ranked.into_iter().next()?;
        let url = best.get("url")?.as_str()?.to_string();
        let name = best
            .get("name")
            .and_then(|n| n.as_str())
            .unwrap_or("sub.srt");
        let tmp = self.cfg.tmp_dir.join(format!(
            "jimaku-{anilist_id}-e{episode}-{}",
            std::path::Path::new(name)
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or("sub.srt")
        ));
        if self.jimaku.download(&url, &tmp).await.is_err() {
            tracing::warn!("ladder: jimaku direct: download failed ({tag})");
            return None;
        }
        // .ass/.ssa go through ffmpeg conversion first.
        let srt_path = if matches!(
            tmp.extension()
                .and_then(|e| e.to_str())
                .unwrap_or("")
                .to_lowercase()
                .as_str(),
            "ass" | "ssa"
        ) {
            let out = tmp.with_extension("srt");
            let st = tokio::process::Command::new("ffmpeg")
                .args([
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    &tmp.to_string_lossy(),
                    &out.to_string_lossy(),
                ])
                .status()
                .await;
            let _ = tokio::fs::remove_file(&tmp).await;
            match st {
                Ok(s) if s.success() => out,
                _ => {
                    tracing::warn!("ladder: jimaku direct: ass convert failed ({tag})");
                    return None;
                }
            }
        } else {
            tmp
        };
        let text = tokio::fs::read_to_string(&srt_path).await.ok()?;
        let _ = tokio::fs::remove_file(&srt_path).await;
        let cues = srt::parse_srt(&text);
        if !self.adequate(&cues, "ja", media_path).await {
            tracing::info!("ladder: jimaku direct: gate rejected ({tag})");
            return None;
        }
        let cues: Vec<Cue> = cues
            .into_iter()
            .filter(|c| !srt::has_ai_marker_text(&c.text))
            .collect();
        if cues.is_empty() {
            return None;
        }
        // Persist the raw source for idempotent reuse (no-clobber).
        let dest = format!("{stem}.ja.hi.srt");
        if !Path::new(&dest).exists() {
            let tmp_dest = format!("{dest}.tmp");
            if tokio::fs::write(&tmp_dest, srt::write_srt(&cues, false, 0))
                .await
                .is_ok()
            {
                let _ = tokio::fs::rename(&tmp_dest, &dest).await;
            }
        }
        tracing::info!("ladder: jimaku direct: hit ({tag})");
        Some(LadderHit {
            cues,
            src_lang: "ja".to_string(),
            source: "jpn".to_string(),
            source_kind: Some("external".to_string()),
        })
    }

    /// Ladder adequacy gate: cue/char minimums, CJK fraction for Japanese,
    /// span within tolerance of container duration when known.
    async fn adequate(&self, cues: &[Cue], src_lang: &str, media_path: &str) -> bool {
        if cues.len() < self.cfg.ladder_min_cues {
            return false;
        }
        let chars: usize = cues.iter().map(|c| c.text.chars().count()).sum();
        if chars < self.cfg.ladder_min_chars {
            return false;
        }
        if normalize_lang(src_lang) == "ja" {
            let cjk: usize = cues
                .iter()
                .flat_map(|c| c.text.chars())
                .filter(|c| {
                    matches!(c,
                    '\u{3040}'..='\u{30ff}' | '\u{3400}'..='\u{4dbf}' | '\u{4e00}'..='\u{9fff}')
                })
                .count();
            let total: usize = cues
                .iter()
                .flat_map(|c| c.text.chars())
                .filter(|c| !c.is_whitespace())
                .count();
            if total > 0 && cjk as f64 / (total as f64) < self.cfg.ladder_min_cjk {
                return false;
            }
        }
        if let (Some(first), Some(last)) = (cues.first(), cues.last()) {
            if let Some(dur) = asr::media_duration_s(media_path).await {
                if dur > 0.0 {
                    let span = (last.end_ms.saturating_sub(first.start_ms)) as f64 / 1000.0;
                    let tol = self.cfg.ladder_span_tol;
                    if span < dur * (1.0 - tol) || span > dur * (1.0 + tol) {
                        return false;
                    }
                }
            }
        }
        true
    }
}

/// Cooldown before a Jimaku-missed stem is probed again. The retired Python
/// hunt ramped 30min→24h with per-pass budgets, tombstones, and a state
/// file; this port keeps the one property that matters (misses are
/// retried, because late uploads must still land) and drops the rest:
/// - budgets: unnecessary — the shared client paces all calls 500ms apart
///   against a 25 req/min limit, so probes cannot burst by construction;
/// - 429 aborts: a limited stem simply misses into the next cooldown;
/// - tombstones: no upgrade pass means no Sonarr-404 orphans; a hopeless
///   stem costs ~2 calls/day, cheaper than tombstone bookkeeping;
/// - backoff ramp: a flat daily probe lands a new upload within ~24h of
///   appearance, same as the capped end of the old ramp.
const JIMAKU_RETRY_COOLDOWN: std::time::Duration = std::time::Duration::from_secs(24 * 3600);

/// True when no attempt is recorded, or the last one is older than the
/// cooldown. Pure (takes `now`) for testability; `Instant` is monotonic so
/// wall-clock jumps cannot re-arm or stall the schedule.
fn jimaku_retry_due(last: Option<std::time::Instant>, now: std::time::Instant) -> bool {
    match last {
        None => true,
        Some(t) => now.duration_since(t) >= JIMAKU_RETRY_COOLDOWN,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn retry_cooldown_fresh_due_stale_due() {
        let now = std::time::Instant::now();
        let day = std::time::Duration::from_secs(24 * 3600);
        // Never attempted: due immediately (bootstrap must not wait a day).
        assert!(jimaku_retry_due(None, now));
        // Attempted just now / an hour ago: not due.
        assert!(!jimaku_retry_due(Some(now), now));
        assert!(!jimaku_retry_due(
            Some(now - std::time::Duration::from_secs(3600)),
            now
        ));
        // Older than the cooldown: due again (late uploads still land).
        assert!(jimaku_retry_due(Some(now - day), now));
        assert!(jimaku_retry_due(Some(now - 2 * day), now));
    }
}
