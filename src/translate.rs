//! Remote LLM translation over OpenAI-compatible `/chat/completions`.
//!
//! Every chunk races the ordered [`ProviderPool`] (fastest-first, per-endpoint
//! semaphores, circuit-breakers) so one slow/dead free-tier model never stalls
//! the sweep. The wire contract mirrors the Python cloud branch: system prompt
//! with KNOWLEDGE, user JSON `{source_language,
//! target_language, lines}`, assistant JSON array of the same length. Guards:
//! wrong-script rejection, CJK-echo probe, count-mismatch retry, per-line
//! fallback, merge-aware tail completion.

use anyhow::{Context, Result};
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

/// Display name for a source code, for prompts and payloads. Every
/// reachable source names itself (`fr` → `French`) rather than every
/// non-English source reading `Japanese`; unknown codes say `Unknown`
/// instead of mislabelling the source. `attempt_chunk` additionally
/// accepts a raw `"ja"`.
pub(crate) fn display_source_lang(src_lang: &str) -> &'static str {
    crate::lang::display_name(src_lang)
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
    let timeouts = pool.llm_timeouts();
    let request = async {
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
            .timeout(timeouts.request)
            .send();
        let resp = match tokio::time::timeout(timeouts.connect.min(timeouts.request), fut).await {
            Err(_) => {
                pool.record_failure(idx);
                anyhow::bail!(
                    "llm connect/headers timeout after {} ms",
                    timeouts.connect.as_millis()
                );
            }
            Ok(Err(e)) => {
                pool.record_failure(idx);
                anyhow::bail!("llm {}", safe_reqwest_reason(&e));
            }
            Ok(Ok(r)) => r,
        };
        let code = resp.status().as_u16();
        if code == 404 {
            pool.record_failure(idx);
            return Ok(None);
        }
        if code == 429 || code >= 500 {
            pool.record_failure(idx);
            anyhow::bail!("llm HTTP {code} (retryable, defer)");
        }
        if code != 200 {
            pool.record_failure(idx);
            anyhow::bail!("llm HTTP {code}");
        }
        let parsed: ChatResp =
            match tokio::time::timeout(timeouts.read.min(timeouts.request), resp.json()).await {
                Err(_) => {
                    pool.record_failure(idx);
                    anyhow::bail!(
                        "llm response-body timeout after {} ms",
                        timeouts.read.as_millis()
                    );
                }
                Ok(Err(e)) => {
                    pool.record_failure(idx);
                    anyhow::bail!("llm {}", safe_reqwest_reason(&e));
                }
                Ok(Ok(parsed)) => parsed,
            };
        let content = parsed
            .choices
            .into_iter()
            .next()
            .and_then(|choice| choice.message.content);
        let Some(content) = content else {
            pool.record_failure(idx);
            anyhow::bail!("llm response missing content");
        };
        pool.record_success(idx);
        Ok(Some(content))
    };
    match tokio::time::timeout(timeouts.request, request).await {
        Err(_) => {
            pool.record_failure(idx);
            anyhow::bail!(
                "llm overall request timeout after {} ms",
                timeouts.request.as_millis()
            );
        }
        Ok(result) => result,
    }
}

fn safe_reqwest_reason(error: &reqwest::Error) -> &'static str {
    if error.is_timeout() {
        "request timeout"
    } else if error.is_connect() {
        "connect failure"
    } else if error.is_body() {
        "request/response body failure"
    } else if error.is_decode() {
        "response decode failure"
    } else {
        "transport failure"
    }
}

/// Try providers in order until one yields content.
async fn chat_across_providers(
    pool: &ProviderPool,
    messages: &[ChatMsg],
) -> Result<Option<String>> {
    let mut failures = Vec::new();
    for (idx, p) in pool.ordered() {
        let key = p.api_key();
        if key.is_empty() {
            failures.push(format!("model {}: no API key", p.model));
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
            Ok(None) => {
                failures.push(format!("model {}: HTTP 404", p.model));
                continue;
            }
            Err(e) => {
                let reason = crate::config::mask_for_log(&e.to_string()).into_owned();
                if reason.contains("timeout") {
                    tracing::warn!(
                        model = %p.model,
                        reason = %reason,
                        "llm attempt timed out, trying next provider"
                    );
                } else {
                    tracing::debug!(
                        model = %p.model,
                        reason = %reason,
                        "llm attempt failed, trying next provider"
                    );
                }
                failures.push(format!("model {}: {reason}", p.model));
                continue;
            }
        }
    }
    if failures.is_empty() {
        anyhow::bail!("translation failed: no providers configured");
    }
    anyhow::bail!(
        "translation failed: all providers exhausted ({})",
        failures.join("; ")
    )
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
) -> Result<Option<Vec<String>>> {
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
        let raw = chat_across_providers(pool, &messages)
            .await?
            .context("translation response missing")?;
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
                return Ok(Some(parsed));
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
    Ok(None)
}

async fn translate_single_line(
    pool: &ProviderPool,
    line: &str,
    target_lang: &str,
    source_lang: &str,
    knowledge: &str,
    placeholders: &[String],
) -> Result<String> {
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
        Ok(Some(mut v)) if !v.is_empty() => {
            Ok(v.remove(0).trim_start_matches('>').trim_start().to_string())
        }
        _ => anyhow::bail!("translation failed: provider returned no valid line"),
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
    let language = normalize_lang(job.target_lang);
    let deadline = pool.llm_timeouts().translation;
    let models = pool.llm_models();
    match tokio::time::timeout(deadline, translate_lines_inner(pool, job)).await {
        Ok(result) => result,
        Err(_) => {
            tracing::warn!(
                language = %language,
                models = ?models,
                timeout_ms = deadline.as_millis(),
                "translation episode-language deadline exceeded"
            );
            anyhow::bail!(
                "translation failed for {language}: episode-language deadline exceeded after {} ms",
                deadline.as_millis()
            );
        }
    }
}

async fn translate_lines_inner(pool: &ProviderPool, job: TranslateJob<'_>) -> Result<Vec<String>> {
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
            let out = attempt_chunk(&pool, &chunk, &tgt, &src, &know, &ph).await?;
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
                let text = translate_single_line(&pool, &line, &tgt, &src, &know, &ph).await?;
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
#[path = "translate_tests.rs"]
mod tests;
