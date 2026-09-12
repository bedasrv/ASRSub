//! Remote LLM translation over OpenAI-compatible `/chat/completions`.
//!
//! Every chunk races the ordered [`ProviderPool`] (fastest-first, per-endpoint
//! semaphores, circuit-breakers) so one slow/dead free-tier model never stalls
//! the sweep. The wire contract mirrors the Python cloud branch: system prompt
//! with KNOWLEDGE, user JSON `{source_language,
//! target_language, lines}`, assistant JSON array of the same length. Guards:
//! wrong-script rejection, CJK-echo probe, count-mismatch retry, per-line
//! fallback, merge-aware tail completion.

use anyhow::Result;
use serde::{Deserialize, Serialize};

use crate::lang::normalize_lang;
use crate::providers::ProviderPool;
use crate::srt::{extract_json_array, guard_foreign_lines, sanitize_lines, wrong_script};

pub const LANG_NAMES: &[(&str, &str)] =
    &[("id", "Indonesian"), ("en", "English"), ("ja", "Japanese")];

pub fn lang_name(lang: &str) -> &str {
    let n = normalize_lang(lang);
    LANG_NAMES
        .iter()
        .find(|(k, _)| *k == n)
        .map(|(_, v)| *v)
        .unwrap_or("Indonesian")
}

#[derive(Debug, Clone, Serialize)]
struct ChatMsg {
    role: String,
    content: String,
}

#[derive(Debug, Deserialize)]
struct ChatResp {
    choices: Vec<ChatChoice>,
}

#[derive(Debug, Deserialize)]
struct ChatChoice {
    message: ChatMsgIn,
}

#[derive(Debug, Deserialize)]
struct ChatMsgIn {
    #[serde(default)]
    content: Option<String>,
}

/// Display name for a normalized source code, for prompts and payloads.
/// The ladder/ASR only ever produce `ja`/`en` sources, so this mapping is
/// exact for the reachable domain (anything non-English is Japanese);
/// `attempt_chunk` additionally accepts a raw `"ja"`.
pub(crate) fn display_source_lang(src_lang: &str) -> &'static str {
    if crate::lang::normalize_lang(src_lang) == "en" {
        "English"
    } else {
        "Japanese"
    }
}

fn system_prompt(target: &str, source: &str, knowledge: &str) -> String {
    let mut s = format!(
        "You are a professional anime subtitle translator. Translate the provided {source} \
         subtitle lines into {target}. Rules: (1) output ONLY a JSON array of strings, \
         same count and order as input; (2) natural dialogue, keep honorifics and name suffixes \
         (san/chan/kun); (3) each line <=42 characters; (4) do not add anything not in the \
         source; (5) no timestamps, no numbering."
    );
    if !knowledge.is_empty() {
        s.push_str("\n\n");
        s.push_str(knowledge);
    }
    s
}

/// One chat call against a single provider. Returns None on 404 (model gone —
/// caller falls through to the next provider) and errors otherwise.
async fn post_chat_once(
    pool: &ProviderPool,
    idx: usize,
    model: &str,
    endpoint: &str,
    key: &str,
    thinking: bool,
    messages: &[ChatMsg],
) -> Result<Option<String>> {
    let _permit = pool.acquire(idx).await;
    let mut body = serde_json::json!({
        "model": model,
        "messages": messages,
        "temperature": 0.3,
    });
    if thinking {
        body["thinking"] = serde_json::json!({"type": "enabled", "effort": "max"});
    }
    let fut = pool
        .http()
        .post(endpoint)
        .header("Authorization", format!("Bearer {key}"))
        .json(&body)
        .timeout(ProviderPool::llm_timeout())
        .send();
    let resp = match tokio::time::timeout(ProviderPool::llm_timeout(), fut).await {
        Err(_) => {
            // Outer timeout (hung endpoint): trip the breaker like any other
            // failure so it leaves the rotation instead of stalling chunks.
            pool.record_failure(idx);
            anyhow::bail!("llm timeout");
        }
        Ok(Err(e)) => {
            pool.record_failure(idx);
            return Err(e.into());
        }
        Ok(Ok(r)) => r,
    };
    let code = resp.status().as_u16();
    if code == 404 {
        pool.record_failure(idx);
        return Ok(None);
    }
    if code == 429 || code >= 500 {
        let _ = resp.text().await;
        pool.record_failure(idx);
        anyhow::bail!("llm HTTP {code} (retryable, defer)");
    }
    if code != 200 {
        let body = resp.text().await.unwrap_or_default();
        pool.record_failure(idx);
        anyhow::bail!("llm HTTP {code}: {}", crate::srt::snippet(&body));
    }
    let parsed: ChatResp = resp.json().await?;
    pool.record_success(idx);
    Ok(parsed
        .choices
        .into_iter()
        .next()
        .and_then(|c| c.message.content))
}

/// Try providers in order until one yields content.
async fn chat_across_providers(
    pool: &ProviderPool,
    messages: &[ChatMsg],
) -> Result<Option<String>> {
    for (idx, p) in pool.ordered() {
        let key = p.api_key();
        if key.is_empty() {
            continue;
        }
        match post_chat_once(
            pool,
            idx,
            &p.model,
            &p.endpoint,
            &key,
            p.thinking_param_accepted,
            messages,
        )
        .await
        {
            Ok(Some(content)) => return Ok(Some(content)),
            Ok(None) => continue, // 404 -> next model
            Err(e) => {
                tracing::debug!(
                    provider = %p.model,
                    error = %crate::config::mask_for_log(&e.to_string()),
                    "llm attempt failed, next provider"
                );
                continue;
            }
        }
    }
    Ok(None)
}

fn echo_hit(text: &str) -> bool {
    text.chars().any(crate::lang::is_cjk)
}

/// True when a translated line is one of the configured SDH placeholders
/// (e.g. `（歌詞）` for foreign lyric lines). Placeholders are CJK by design
/// and must be exempt from the wrong-script/echo guards below — otherwise a
/// correctly-echoed placeholder fails the whole chunk and the single-line
/// fallback empties the line.
fn is_placeholder(line: &str, placeholders: &[String]) -> bool {
    let t = line.trim();
    !t.is_empty() && placeholders.iter().any(|p| p.trim() == t)
}

/// Translate one chunk (<= TRANSLATE_CHUNK lines): one initial attempt plus
/// one corrective retry on parse/count/script failure. Returns parsed lines
/// or None. Matches the `for _ in range(2)` cloud branch in
/// `chat_translate_batch`.
async fn attempt_chunk(
    pool: &ProviderPool,
    chunk: &[String],
    target_lang: &str,
    source_lang: &str,
    knowledge: &str,
    placeholders: &[String],
) -> Option<Vec<String>> {
    let target_name = lang_name(target_lang).to_string();
    let source_name = if source_lang == "ja" {
        "Japanese"
    } else {
        source_lang
    }
    .to_string();
    let user = serde_json::to_string(&serde_json::json!({
        "source_language": source_name,
        "target_language": target_name,
        "lines": chunk,
    }))
    .unwrap();
    let system = system_prompt(&target_name, &source_name, knowledge);
    let mut messages = vec![
        ChatMsg {
            role: "system".to_string(),
            content: system,
        },
        ChatMsg {
            role: "user".to_string(),
            content: user,
        },
    ];
    for _ in 0..2 {
        let raw = chat_across_providers(pool, &messages).await.ok()??;
        if let Some(parsed) = extract_json_array(&raw) {
            // Placeholder lines are exempt from both guards (see
            // is_placeholder): they are CJK by design.
            let script_ok = !parsed
                .iter()
                .any(|t| !is_placeholder(t, placeholders) && wrong_script(t, &target_name));
            let echo_ok = target_name == "Japanese"
                || !parsed
                    .iter()
                    .take(11.min(parsed.len()))
                    .any(|t| !is_placeholder(t, placeholders) && echo_hit(t));
            if parsed.len() == chunk.len() && script_ok && echo_ok {
                return Some(parsed);
            }
        }
        messages.push(ChatMsg {
            role: "assistant".to_string(),
            content: raw,
        });
        messages.push(ChatMsg {
            role: "user".to_string(),
            content: format!(
                "Your previous output was not a JSON array with exactly {} strings. \
                 Output ONLY the JSON array, e.g. [\"...\", \"...\"], same count and order as the input.",
                chunk.len()
            ),
        });
    }
    None
}

async fn translate_single_line(
    pool: &ProviderPool,
    line: &str,
    target_lang: &str,
    source_lang: &str,
    knowledge: &str,
    placeholders: &[String],
) -> String {
    match attempt_chunk(
        pool,
        &[line.to_string()],
        target_lang,
        source_lang,
        knowledge,
        placeholders,
    )
    .await
    {
        Some(mut v) if !v.is_empty() => {
            v.remove(0).trim_start_matches('>').trim_start().to_string()
        }
        _ => String::new(),
    }
}

/// Episode translation job: one struct instead of positional args.
/// `fanout` bounds concurrent chunk translations (from
/// `TRANSLATE_CONCURRENCY`); the per-line fallback fanout is fixed at 4.
pub struct TranslateJob<'a> {
    pub lines: Vec<String>,
    pub target_lang: &'a str,
    pub source_lang: &'a str,
    pub knowledge: &'a str,
    pub chunk_size: usize,
    pub fanout: usize,
    pub skip_guard: bool,
    pub placeholders: &'a [String],
}

/// Merge-aware episode translation. Chunks translate concurrently (bounded);
/// results are re-ordered to input order. Short-line merges by the model are
/// tolerated via tail completion + bounded per-line fallback so output length
/// always equals input length.
pub async fn translate_lines(pool: &ProviderPool, job: TranslateJob<'_>) -> Result<Vec<String>> {
    let target_lang = normalize_lang(job.target_lang);
    let mut lines = sanitize_lines(job.lines, 10);
    if !job.skip_guard {
        lines = guard_foreign_lines(lines, job.placeholders);
    }
    if lines.is_empty() {
        return Ok(Vec::new());
    }
    let chunk_size = job.chunk_size.max(1);
    // Single mechanism: `TRANSLATE_CONCURRENCY` (file or env, via the global
    // env-wins rule). No separate env-only override.
    let sem = std::sync::Arc::new(tokio::sync::Semaphore::new(job.fanout.max(1)));
    let mut jobs = Vec::new();
    for (ci, chunk) in lines.chunks(chunk_size).enumerate() {
        let pool = pool.clone();
        let sem = sem.clone();
        let chunk = chunk.to_vec();
        let (tgt, src, know, ph) = (
            target_lang.clone(),
            job.source_lang.to_string(),
            job.knowledge.to_string(),
            job.placeholders.to_vec(),
        );
        jobs.push(async move {
            let _p = sem.acquire_owned().await.expect("semaphore closed");
            let out = attempt_chunk(&pool, &chunk, &tgt, &src, &know, &ph).await;
            Ok::<_, anyhow::Error>((ci, chunk, out))
        });
    }
    let mut results = futures::future::try_join_all(jobs).await?;
    results.sort_by_key(|(ci, _, _)| *ci);
    let (mut out, pending) = stitch_chunks(lines.len(), &results);
    // Fallback lines translate concurrently under a small semaphore — never
    // serially, so a dead provider chunk cannot stall the episode
    // line-by-line. Each slot keeps its absolute position.
    if !pending.is_empty() {
        let fb_sem = std::sync::Arc::new(tokio::sync::Semaphore::new(4));
        let mut fb_jobs = Vec::with_capacity(pending.len());
        for (idx, line) in pending {
            let pool = pool.clone();
            let fb_sem = fb_sem.clone();
            let (tgt, src, know, ph) = (
                target_lang.clone(),
                job.source_lang.to_string(),
                job.knowledge.to_string(),
                job.placeholders.to_vec(),
            );
            fb_jobs.push(async move {
                let _p = fb_sem.acquire_owned().await.expect("semaphore closed");
                let text = translate_single_line(&pool, &line, &tgt, &src, &know, &ph).await;
                Ok::<_, anyhow::Error>((idx, text))
            });
        }
        for (idx, text) in futures::future::try_join_all(fb_jobs).await? {
            out[idx] = text;
        }
    }
    Ok(out)
}

/// Merge chunk outputs into absolute positions. Pure and unit-testable:
/// only exact-count chunks land (like Python's `len(parsed) == len(lines)`
/// gate); anything else — short, oversized, or missing — becomes
/// `(pos, line)` fallback work. Panic-proof by construction: indices derive
/// solely from chunk positions, so an oversized model array can never write
/// past its slots.
fn stitch_chunks(total: usize, results: &[ChunkResult]) -> (Vec<String>, Vec<(usize, String)>) {
    let mut ordered: Vec<&ChunkResult> = results.iter().collect();
    ordered.sort_by_key(|(ci, _, _)| *ci);
    let mut out: Vec<String> = vec![String::new(); total];
    let mut pending: Vec<(usize, String)> = Vec::new();
    let mut pos = 0;
    for (_, chunk, parsed) in ordered {
        match parsed {
            Some(v) if v.len() == chunk.len() => {
                for (i, t) in v.iter().enumerate() {
                    out[pos + i] = t.clone();
                }
            }
            _ => {
                for (j, line) in chunk.iter().enumerate() {
                    pending.push((pos + j, line.clone()));
                }
            }
        }
        pos += chunk.len();
    }
    (out, pending)
}

/// One translated chunk with its input index and parsed output.
/// Factored out for `type_complexity`: chunk results flow through this alias.
type ChunkResult = (usize, Vec<String>, Option<Vec<String>>);

/// Review pass: fix mistranslations line-by-line against the JA source.
/// Serial by design (API safe concurrency = 1 for the reviewer model).
pub async fn review_lines(
    pool: &ProviderPool,
    ja: &[String],
    tr: &[String],
    chunk_size: usize,
) -> Vec<String> {
    let mut out = tr.to_vec();
    let system = "You are a subtitle translation reviewer for an anime episode.\n\
        The Japanese source line is ground truth. The current translation may contain mistakes. \
        Fix ONLY: mistranslations, wrong character/term names, grammatical errors, awkward phrasing, \
        or lines that do not match the Japanese meaning.\n\
        Do NOT restyle lines that are already correct. Do NOT add or remove lines.\nDo NOT add commentary.\n\
        Reply with corrected lines ONLY, one per line, format: \"i. corrected text\"\n\
        where i is the original line number. If NO line needs correction, reply with\nexactly: NONE";
    for (start, (jc, tc)) in ja.chunks(chunk_size).zip(tr.chunks(chunk_size)).enumerate() {
        let start = start * chunk_size;
        let mut user = String::from(
            "Review these lines. Japanese source (JA) vs current translation (TR):\n\n",
        );
        for (i, (j, t)) in jc.iter().zip(tc.iter()).enumerate() {
            user.push_str(&format!("{}. JA: {j} | TR: {t}\n", i + 1));
        }
        let messages = vec![
            ChatMsg {
                role: "system".to_string(),
                content: system.to_string(),
            },
            ChatMsg {
                role: "user".to_string(),
                content: user,
            },
        ];
        let Ok(Some(raw)) = chat_across_providers(pool, &messages).await else {
            continue;
        };
        if raw.trim() == "NONE" {
            continue;
        }
        for line in raw.lines() {
            if let Some((n, text)) = line.trim().split_once(['.', '．']) {
                if let Ok(n) = n.trim().parse::<usize>() {
                    if n >= 1 && n <= tc.len() {
                        out[start + n - 1] = text.trim().to_string();
                    }
                }
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn placeholders() -> Vec<String> {
        vec!["（歌詞）".to_string()]
    }

    #[test]
    fn placeholder_lines_pass_script_and_echo_guards() {
        // A correctly-echoed SDH placeholder must not fail chunk acceptance:
        // it is CJK by design (wrong_script + echo_hit both fire on it).
        assert!(is_placeholder("（歌詞）", &placeholders()));
        assert!(!is_placeholder("Halo dunia", &placeholders()));
        assert!(!is_placeholder("", &placeholders()));
        assert!(wrong_script("（歌詞）", "Indonesian"));
        assert!(echo_hit("（歌詞）"));
    }

    #[test]
    fn guards_still_catch_real_foreign_output() {
        // Non-placeholder CJK in a latin target is still rejected.
        assert!(!is_placeholder("こんにちは世界", &placeholders()));
        assert!(wrong_script("こんにちは世界", "Indonesian"));
        assert!(!wrong_script("Halo, apa kabar?", "Indonesian"));
    }

    #[test]
    fn stitch_rejects_oversized_and_orders_chunks() {
        // Oversized model arrays never write past their slots (no panic,
        // no cross-chunk bleed): the whole chunk becomes fallback work.
        let results = vec![
            (
                1usize,
                vec!["c".to_string(), "d".to_string()],
                Some(vec!["C".to_string()]),
            ),
            (
                0usize,
                vec!["a".to_string(), "b".to_string()],
                Some(vec!["A".to_string(), "B".to_string(), "EXTRA".to_string()]),
            ),
        ];
        let (out, pending) = stitch_chunks(4, &results);
        // Neither the oversized array (chunk 0) nor the short one (chunk 1)
        // lands: every line becomes fallback work at its own position.
        assert_eq!(out, vec!["", "", "", ""]);
        assert_eq!(
            pending,
            vec![
                (0, "a".to_string()),
                (1, "b".to_string()),
                (2, "c".to_string()),
                (3, "d".to_string())
            ]
        );
    }

    #[test]
    fn stitch_places_exact_chunks_in_order() {
        let results = vec![
            (1usize, vec!["c".to_string()], Some(vec!["C".to_string()])),
            (
                0usize,
                vec!["a".to_string(), "b".to_string()],
                Some(vec!["A".to_string(), "B".to_string()]),
            ),
        ];
        let (out, pending) = stitch_chunks(3, &results);
        assert_eq!(out, vec!["A", "B", "C"]);
        assert!(pending.is_empty());
    }

    #[test]
    fn prompt_carries_actual_source_and_target() {
        // Legacy parity (test_translation_source_language.py): the prompt
        // must name the real source language, not a hardcoded one — the
        // ladder passes English through for `en` sources.
        let p = system_prompt("Indonesian", display_source_lang("en"), "");
        assert!(p.contains("English"), "{p}");
        assert!(p.contains("Indonesian"), "{p}");
        assert_eq!(display_source_lang("ja"), "Japanese");
        assert_eq!(display_source_lang("jpn"), "Japanese");
        assert_eq!(display_source_lang("en"), "English");
    }

    #[tokio::test]
    async fn persistent_failure_emits_empty_lines() {
        // Legacy parity (dry_tests per_line_fallback): with no usable
        // provider, every line — chunk and per-line fallback — resolves to
        // empty text, and output length still equals input length.
        let pool = ProviderPool::new(
            crate::providers::ProvidersFile {
                llm_translation_models: vec![],
                whisper_stt: None,
                whisper_stt_fallbacks: vec![],
            },
            reqwest::Client::new(),
        );
        let out = translate_lines(
            &pool,
            TranslateJob {
                lines: vec!["first line".to_string(), "second line".to_string()],
                target_lang: "id",
                source_lang: "English",
                knowledge: "",
                chunk_size: 10,
                fanout: 2,
                skip_guard: false,
                placeholders: &[],
            },
        )
        .await
        .unwrap();
        assert_eq!(out, vec!["", ""]);
    }
}
