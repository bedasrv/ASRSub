//! Per-series name glossary (schema v2) injected as KNOWLEDGE background.
//!
//! `glossary.json`: `{ "<Series>": {"entries": [{ja, en, aliases[], kind}]}}`.
//! A legacy flat map `{ "<Series>": {"カナ": "En"} }` is migrated in memory.
//! Episode-cast filtering scans the full cue text once per episode so every
//! chunk of the episode shares one stable KNOWLEDGE prefix (prefix-cache
//! friendly for the remote LLM).

use std::collections::HashMap;
use std::path::Path;

use serde::{Deserialize, Serialize};

pub const MAX_REFS: usize = 15;

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct GlossaryEntry {
    pub ja: String,
    pub en: String,
    #[serde(default)]
    pub aliases: Vec<String>,
    #[serde(default = "default_kind")]
    pub kind: String,
}

fn default_kind() -> String {
    "character".to_string()
}

#[derive(Debug, Clone, Default)]
pub struct Glossary {
    raw: HashMap<String, serde_json::Value>,
}

impl Glossary {
    pub fn load(path: &Path) -> Self {
        let Ok(text) = std::fs::read_to_string(path) else {
            return Self::default();
        };
        let Ok(serde_json::Value::Object(obj)) = serde_json::from_str::<serde_json::Value>(&text)
        else {
            return Self::default();
        };
        Self {
            raw: obj.into_iter().collect(),
        }
    }

    #[cfg(test)]
    pub fn from_map(raw: HashMap<String, serde_json::Value>) -> Self {
        Self { raw }
    }

    fn find_key(&self, series_title: &str) -> Option<String> {
        let norm = norm_key(series_title);
        if norm.is_empty() {
            return None;
        }
        for k in self.raw.keys() {
            if norm_key(k) == norm {
                return Some(k.clone());
            }
        }
        for k in self.raw.keys() {
            if norm.contains(&norm_key(k)) && !norm_key(k).is_empty() {
                return Some(k.clone());
            }
        }
        None
    }

    pub fn entries(&self, series_title: &str, max_refs: usize) -> Vec<GlossaryEntry> {
        let Some(key) = self.find_key(series_title) else {
            return Vec::new();
        };
        let Some(raw) = self.raw.get(&key) else {
            return Vec::new();
        };
        entries_from_raw(raw).into_iter().take(max_refs).collect()
    }

    /// Episode-cast resolution: substring scan over ja/en/aliases; falls back
    /// to the full list when < 3 match (ASR-garbled names).
    pub fn matched_entries(
        &self,
        series_title: &str,
        cue_texts: &[String],
        max_refs: usize,
    ) -> Vec<GlossaryEntry> {
        let entries = self.entries(series_title, max_refs);
        if entries.is_empty() || cue_texts.is_empty() {
            return entries;
        }
        let haystack = cue_texts.join("\n").to_lowercase();
        if haystack.trim().is_empty() {
            return entries;
        }
        let mut matched = Vec::new();
        for e in &entries {
            let mut surfaces = vec![e.ja.clone(), e.en.clone()];
            surfaces.extend(e.aliases.iter().cloned());
            if surfaces
                .iter()
                .any(|s| !s.is_empty() && haystack.contains(&s.to_lowercase()))
            {
                matched.push(e.clone());
            }
        }
        if matched.len() < 3 {
            entries
        } else {
            matched
        }
    }

    pub fn knowledge_block_for_cues(
        &self,
        series_title: &str,
        cue_texts: &[String],
        max_refs: usize,
    ) -> String {
        render_block(&self.matched_entries(series_title, cue_texts, max_refs))
    }
}

fn norm_key(s: &str) -> String {
    s.trim()
        .to_lowercase()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

fn entries_from_raw(raw: &serde_json::Value) -> Vec<GlossaryEntry> {
    if let Some(entries) = raw.get("entries").and_then(|v| v.as_array()) {
        return entries
            .iter()
            .filter_map(|e| {
                let ja = e.get("ja")?.as_str()?;
                let en = e.get("en")?.as_str()?;
                if ja.is_empty() || en.is_empty() {
                    return None;
                }
                let aliases = e
                    .get("aliases")
                    .and_then(|a| a.as_array())
                    .map(|arr| {
                        arr.iter()
                            .filter_map(|v| v.as_str().map(str::to_string))
                            .collect()
                    })
                    .unwrap_or_default();
                let kind = e
                    .get("kind")
                    .and_then(|k| k.as_str())
                    .filter(|k| matches!(*k, "character" | "place" | "term"))
                    .unwrap_or("character")
                    .to_string();
                Some(GlossaryEntry {
                    ja: ja.to_string(),
                    en: en.to_string(),
                    aliases,
                    kind,
                })
            })
            .collect();
    }
    // Legacy flat map.
    let Some(obj) = raw.as_object() else {
        return Vec::new();
    };
    obj.iter()
        .filter_map(|(ja, en)| {
            let en = en.as_str()?;
            if ja.is_empty() || en.is_empty() || ja == "entries" {
                return None;
            }
            Some(GlossaryEntry {
                ja: ja.clone(),
                en: en.to_string(),
                aliases: Vec::new(),
                kind: "character".to_string(),
            })
        })
        .collect()
}

fn render_block(entries: &[GlossaryEntry]) -> String {
    if entries.is_empty() {
        return String::new();
    }
    let mut lines = vec![
        "KNOWLEDGE — official names for this series. When a Japanese form below appears (including aliases), translate it as its official English name:".to_string(),
    ];
    for e in entries {
        if e.aliases.is_empty() {
            lines.push(format!("- {} => {} ({})", e.ja, e.en, e.kind));
        } else {
            lines.push(format!(
                "- {} => {} (aliases: {}; {})",
                e.ja,
                e.en,
                e.aliases.join("; "),
                e.kind
            ));
        }
    }
    lines.join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn migrates_legacy_flat_map() {
        let g = Glossary::from_map(
            [("DanDaDan".to_string(), json!({"オカルン": "Okarun"}))]
                .into_iter()
                .collect(),
        );
        let e = g.entries("dandadan", 15);
        assert_eq!(e.len(), 1);
        assert_eq!(e[0].en, "Okarun");
    }

    #[test]
    fn falls_back_to_full_list_when_few_match() {
        let g = Glossary::from_map(
            [(
                "S".to_string(),
                json!({"entries": [
                    {"ja": "a1", "en": "A1"},
                    {"ja": "a2", "en": "A2"},
                    {"ja": "a3", "en": "A3"},
                    {"ja": "a4", "en": "A4"},
                ]}),
            )]
            .into_iter()
            .collect(),
        );
        let m = g.matched_entries("s", &["nothing here".to_string()], 15);
        assert_eq!(m.len(), 4);
    }

    #[test]
    fn knowledge_block_renders_matched_cast() {
        // Legacy parity (knowledge block rendering): matched cast renders
        // as ja => en lines with aliases/kinds; unknown series → empty
        // block (no prompt bloat).
        let g = Glossary::from_map(
            [(
                "Show".to_string(),
                json!({"entries": [
                    {"ja": "オカルン", "en": "Okarun", "aliases": ["okk-arun"], "kind": "character"},
                    {"ja": "東京", "en": "Tokyo", "kind": "place"},
                    {"ja": "呪い", "en": "curse", "kind": "term"},
                    {"ja": "モモ", "en": "Momo"},
                ]}),
            )]
            .into_iter()
            .collect(),
        );
        let cues = vec![
            "オカルンと東京へ行く".to_string(),
            "呪いだ".to_string(),
            "モモ！".to_string(),
        ];
        let block = g.knowledge_block_for_cues("show", &cues, 15);
        assert!(block.starts_with("KNOWLEDGE"), "{block}");
        assert!(block.contains("オカルン => Okarun"), "{block}");
        assert!(block.contains("aliases: okk-arun"), "{block}");
        assert!(block.contains("東京 => Tokyo (place)"), "{block}");
        assert!(g.knowledge_block_for_cues("other", &cues, 15).is_empty());
    }
}
