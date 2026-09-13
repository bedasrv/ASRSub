//! Language normalization + display names + sidecar path helpers.
//!
//! Container tags are ISO-639-2/B (`fre`, `eng`, `jpn`, `ger`, `spa`, …)
//! while Whisper wants ISO-639-1 (`fr`, `en`, `ja`, `de`, `es`, …);
//! Sonarr/Radarr report the same thing as a full name (`"Japanese"`).
//! All three spellings normalize to one canonical application code. Unknown
//! codes pass through lowercased alphanumeric form so future languages keep
//! working.
//!
//! Two boundaries live here because both decide whether a code is a
//! language at all: [`normalize_lang`] turns a tag into its canonical code,
//! and [`is_usable_code`] decides whether that code names a language the
//! pipeline may act on — `und`/`unknown`/`""` are uncertainty markers, not
//! languages, and must never reach Whisper's `language` field or a
//! provenance row.

/// ISO-639 aliases (2/B, 2/T, full names) → canonical application code.
/// One table: `normalize_lang` is the single place a tag becomes a code, so
/// track choice, Whisper's `language` field, and sidecar paths cannot drift.
const LANG_ALIASES: &[(&str, &str)] = &[
    ("ja", "ja"),
    ("jp", "ja"),
    ("jpn", "ja"),
    ("japanese", "ja"),
    ("en", "en"),
    ("eng", "en"),
    ("enm", "en"),
    ("english", "en"),
    ("id", "id"),
    ("ind", "id"),
    ("indonesian", "id"),
    ("fr", "fr"),
    ("fre", "fr"),
    ("fra", "fr"),
    ("french", "fr"),
    ("de", "de"),
    ("ger", "de"),
    ("deu", "de"),
    ("german", "de"),
    ("es", "es"),
    ("spa", "es"),
    ("spanish", "es"),
    ("it", "it"),
    ("ita", "it"),
    ("italian", "it"),
    ("pt", "pt"),
    ("por", "pt"),
    ("portuguese", "pt"),
    ("nl", "nl"),
    ("dut", "nl"),
    ("nld", "nl"),
    ("dutch", "nl"),
    ("ru", "ru"),
    ("rus", "ru"),
    ("russian", "ru"),
    ("ko", "ko"),
    ("kor", "ko"),
    ("korean", "ko"),
    ("zh", "zh"),
    ("zho", "zh"),
    ("chi", "zh"),
    ("chinese", "zh"),
    ("pl", "pl"),
    ("pol", "pl"),
    ("polish", "pl"),
    ("tr", "tr"),
    ("tur", "tr"),
    ("turkish", "tr"),
    ("ar", "ar"),
    ("ara", "ar"),
    ("arabic", "ar"),
    ("hi", "hi"),
    ("hin", "hi"),
    ("hindi", "hi"),
    ("th", "th"),
    ("tha", "th"),
    ("thai", "th"),
    ("vi", "vi"),
    ("vie", "vi"),
    ("vietnamese", "vi"),
    ("sv", "sv"),
    ("swe", "sv"),
    ("swedish", "sv"),
    ("no", "no"),
    ("nor", "no"),
    ("norwegian", "no"),
    ("da", "da"),
    ("dan", "da"),
    ("danish", "da"),
    ("fi", "fi"),
    ("fin", "fi"),
    ("finnish", "fi"),
    ("cs", "cs"),
    ("ces", "cs"),
    ("cze", "cs"),
    ("czech", "cs"),
    ("el", "el"),
    ("ell", "el"),
    ("gre", "el"),
    ("greek", "el"),
    ("he", "he"),
    ("heb", "he"),
    ("hebrew", "he"),
    ("hu", "hu"),
    ("hun", "hu"),
    ("hungarian", "hu"),
    ("ro", "ro"),
    ("ron", "ro"),
    ("rum", "ro"),
    ("romanian", "ro"),
    ("uk", "uk"),
    ("ukr", "uk"),
    ("ukrainian", "uk"),
    ("fa", "fa"),
    ("fas", "fa"),
    ("per", "fa"),
    ("persian", "fa"),
    ("ms", "ms"),
    ("msa", "ms"),
    ("may", "ms"),
    ("malay", "ms"),
    ("fil", "fil"),
    ("tgl", "fil"),
    ("filipino", "fil"),
];

/// Drop a trailing region/script qualifier: `"English (US)"` → `"English"`,
/// `"Chinese (Traditional)"` → `"Chinese"`, `"French [FR]"` → `"French"`.
/// The qualifier refines a language the tag before it already names, so
/// without this `"English (US)"` collapsed to the nonsense code
/// `englishus`. Applied repeatedly (stacked qualifiers) and only when the
/// group closes the string; a group that does not close it, or garbage with
/// no bracket at all, is left to the alnum collapse exactly as before.
fn strip_trailing_qualifier(value: &str) -> &str {
    let mut s = value.trim();
    loop {
        let t = s.trim_end();
        let open = if let Some(inner) = t.strip_suffix(')') {
            inner.rfind('(')
        } else if let Some(inner) = t.strip_suffix(']') {
            inner.rfind('[')
        } else {
            None
        };
        match open {
            Some(i) => s = &t[..i],
            None => return t,
        }
    }
}

/// Normalize a language tag to the canonical application code.
pub fn normalize_lang(value: &str) -> String {
    let norm: String = strip_trailing_qualifier(value)
        .to_lowercase()
        .chars()
        .filter(|c| c.is_ascii_alphanumeric())
        .collect();
    match LANG_ALIASES.iter().find(|(alias, _)| *alias == norm) {
        Some((_, canonical)) => (*canonical).to_string(),
        None => norm,
    }
}

/// A code the pipeline may pin as a transcription language: a plausible
/// ISO-639-style token, never an uncertainty marker. `und` (what
/// ffmpeg/mp4 muxers write for an unset language), `unknown`, `""` and a
/// whitespace-only tag, `englishus` (from the full name `"English (US)"`),
/// and `jajp` (from a `ja-JP`-style tag) all fail it. A failing code means
/// *unknown*: the caller must fall back to detection, because sending one of
/// these verbatim as Whisper's `language` form field is what made the
/// provider answer HTTP 400 (and a response code of `unknown`/`none` must
/// not become an episode's source language either).
pub fn is_usable_code(code: &str) -> bool {
    let c = code.trim();
    let ascii_alpha = c.len() >= 2 && c.len() <= 3 && c.chars().all(|ch| ch.is_ascii_lowercase());
    ascii_alpha
        && !matches!(
            c,
            "und" | "mul" | "zxx" | "mis" | "unknown" | "none" | "na" | "undefined" | "auto"
        )
}

/// The language name out of a Sonarr/Radarr `originalLanguage` value: the
/// usual `{id, name}` object or a bare string (Bazarr passes the Radarr
/// payload through, and the two services do not spell it the same way).
/// Absent, null, or a blank name degrades to `None` — it never fails a
/// pass. One parser for both callers so the shapes cannot drift apart.
pub fn original_language(value: &serde_json::Value) -> Option<String> {
    let s = match value {
        serde_json::Value::String(s) => s.clone(),
        _ => value.get("name").and_then(|n| n.as_str())?.to_string(),
    };
    let s = s.trim().to_string();
    (!s.is_empty()).then_some(s)
}

/// English display name for a language code, for translation prompts and
/// payloads. Every reachable source names itself (`fr` → `French`) instead
/// of every non-English source reading `Japanese`; `en`/`ja` keep their
/// long-standing exact strings. Unknown codes say so rather than guessing.
pub fn display_name(code: &str) -> &'static str {
    match normalize_lang(code).as_str() {
        "id" => "Indonesian",
        "en" => "English",
        "ja" => "Japanese",
        "fr" => "French",
        "de" => "German",
        "es" => "Spanish",
        "it" => "Italian",
        "pt" => "Portuguese",
        "nl" => "Dutch",
        "ru" => "Russian",
        "ko" => "Korean",
        "zh" => "Chinese",
        "pl" => "Polish",
        "tr" => "Turkish",
        "ar" => "Arabic",
        "hi" => "Hindi",
        "th" => "Thai",
        "vi" => "Vietnamese",
        "sv" => "Swedish",
        "no" => "Norwegian",
        "da" => "Danish",
        "fi" => "Finnish",
        "cs" => "Czech",
        "el" => "Greek",
        "he" => "Hebrew",
        "hu" => "Hungarian",
        "ro" => "Romanian",
        "uk" => "Ukrainian",
        "fa" => "Persian",
        "ms" => "Malay",
        "fil" => "Filipino",
        _ => "Unknown",
    }
}

/// True for the only source the foreign-script guard is meant for: Japanese
/// ASR echoing OP/ED lyrics in English or Chinese. That guard rewrites
/// "mostly-latin" and "hanzi without kana" lines to SDH placeholders, so
/// running it on any other source would empty the episode — a latin source
/// (French, German, English) is mostly-latin by nature, and a Chinese one is
/// hanzi without kana. Only `ja` needs it; everything else skips it.
pub fn needs_foreign_guard(code: &str) -> bool {
    normalize_lang(code) == "ja"
}

/// Stem of a media/sidecar path: everything before the last `.`
/// (`/m/ep.mkv` → `/m/ep`). No extension → the whole path.
pub fn stem_of(path: &str) -> &str {
    path.rsplit_once('.').map(|(s, _)| s).unwrap_or(path)
}

/// Hiragana/katakana block (`3040-30FF`), per the pipeline's long-standing
/// convention shared by the srt guard, echo probe, and ladder gate.
pub fn is_kana(c: char) -> bool {
    ('\u{3040}'..='\u{30ff}').contains(&c)
}

/// Any CJK character (kana + `3400-4DBF` + `4E00-9FFF`): the single
/// definition shared by the foreign-script guard (`srt`), the echo probe
/// (`translate`), and the ladder adequacy gate.
pub fn is_cjk(c: char) -> bool {
    is_kana(c) || ('\u{3400}'..='\u{4dbf}').contains(&c) || ('\u{4e00}'..='\u{9fff}').contains(&c)
}
/// Aliases carrying the same content for one canonical language.
/// Unknown languages yield an empty slice (callers fall back to the
/// normalized code itself).
pub fn sidecar_aliases(lang: &str) -> &'static [&'static str] {
    match normalize_lang(lang).as_str() {
        "ja" => &["ja", "jpn", "jp"],
        "id" => &["id", "ind"],
        "en" => &["en", "eng", "enm"],
        _ => &[],
    }
}

/// All on-disk sidecar candidates for `{stem}.{lang}[.hi|.forced...].srt`.
pub fn sidecar_paths(stem: &str, lang: &str) -> Vec<String> {
    let norm = normalize_lang(lang);
    let aliases = sidecar_aliases(&norm);
    if aliases.is_empty() {
        return ["", ".hi", ".forced", ".hi.forced", ".forced.hi"]
            .iter()
            .map(|flag| format!("{stem}.{norm}{flag}.srt"))
            .collect();
    }
    aliases
        .iter()
        .flat_map(|alias| {
            ["", ".hi", ".forced", ".hi.forced", ".forced.hi"]
                .iter()
                .map(move |flag| format!("{stem}.{alias}{flag}.srt"))
        })
        .collect()
}

/// The single canonical HI path ASRSub owns for a target language.
pub fn canonical_target_sidecar(stem: &str, lang: &str) -> String {
    format!("{stem}.{}.hi.srt", normalize_lang(lang))
}

/// Replaceable (non-forced) candidates, canonical HI first.
pub fn replaceable_target_sidecar_paths(stem: &str, lang: &str) -> Vec<String> {
    let norm = normalize_lang(lang);
    let mut paths = vec![canonical_target_sidecar(stem, &norm)];
    let aliases = sidecar_aliases(&norm);
    if aliases.is_empty() {
        let p = format!("{stem}.{norm}.srt");
        if !paths.contains(&p) {
            paths.push(p);
        }
        return paths;
    }
    for alias in aliases {
        for flag in ["", ".hi"] {
            let p = format!("{stem}.{alias}{flag}.srt");
            if !paths.contains(&p) {
                paths.push(p);
            }
        }
    }
    paths
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalizes_aliases() {
        assert_eq!(normalize_lang("JPN"), "ja");
        assert_eq!(normalize_lang("jp"), "ja");
        assert_eq!(normalize_lang("IND"), "id");
        assert_eq!(normalize_lang("ENG"), "en");
        assert_eq!(normalize_lang(" id "), "id");
    }

    #[test]
    fn normalizes_iso639_2b_2t_and_names() {
        // Container tags are 2/B while Whisper wants 2-letter codes.
        assert_eq!(normalize_lang("fre"), "fr");
        assert_eq!(normalize_lang("fra"), "fr");
        assert_eq!(normalize_lang("ger"), "de");
        assert_eq!(normalize_lang("deu"), "de");
        assert_eq!(normalize_lang("spa"), "es");
        assert_eq!(normalize_lang("por"), "pt");
        assert_eq!(normalize_lang("dut"), "nl");
        assert_eq!(normalize_lang("chi"), "zh");
        assert_eq!(normalize_lang("msa"), "ms");
        assert_eq!(normalize_lang("tgl"), "fil");
        // Sonarr/Radarr report the language as a full name.
        assert_eq!(normalize_lang("Japanese"), "ja");
        assert_eq!(normalize_lang("French"), "fr");
        // Existing results are unchanged.
        assert_eq!(normalize_lang("ja"), "ja");
        assert_eq!(normalize_lang("en"), "en");
        // Unknown codes still pass through lowercased/alphanumeric.
        assert_eq!(normalize_lang("xx"), "xx");
        assert_eq!(normalize_lang(" Klingon "), "klingon");
        assert_eq!(normalize_lang(""), "");
    }

    #[test]
    fn regional_qualifiers_collapse_to_the_language() {
        // A trailing qualifier refines the language the tag already names:
        // without stripping it, "English (US)" collapsed to `englishus` and
        // was pinned as Whisper's `language`.
        assert_eq!(normalize_lang("English (US)"), "en");
        assert_eq!(normalize_lang("Japanese (JP)"), "ja");
        assert_eq!(normalize_lang("Chinese (Traditional)"), "zh");
        assert_eq!(normalize_lang("French [FR]"), "fr");
        // Stacked qualifiers and loose whitespace still collapse.
        assert_eq!(normalize_lang("German (DE) [Stereo]"), "de");
        assert_eq!(normalize_lang(" Korean "), "ko");
        // Unmapped garbage keeps collapsing exactly as before: a bracket
        // that does not close the string is not a qualifier.
        assert_eq!(normalize_lang(" Klingon "), "klingon");
        assert_eq!(normalize_lang("English (US) extra"), "englishusextra");
        assert_eq!(normalize_lang(""), "");
    }

    #[test]
    fn usable_codes_exclude_uncertainty_markers() {
        for ok in ["en", "ja", "id", "fr", "fil", " zh "] {
            assert!(is_usable_code(ok), "{ok} must be usable");
        }
        for bad in [
            "",
            " ",
            "und",
            "mul",
            "zxx",
            "mis",
            "unknown",
            "none",
            "na",
            "undefined",
            "auto",
            "englishus",
            "jajp",
            "zhhant",
            "e",
            "EN",
        ] {
            assert!(!is_usable_code(bad), "{bad} must not be usable");
        }
    }

    #[test]
    fn alias_table_is_unique_and_round_trips() {
        // Every alias resolves, no alias is declared twice, and every
        // canonical code is its own alias: the table cannot drift into
        // ambiguity (two spellings of one language must not disagree).
        let mut seen = std::collections::HashSet::new();
        for (alias, canonical) in LANG_ALIASES {
            assert!(seen.insert(*alias), "duplicate alias {alias}");
            assert_eq!(normalize_lang(alias), *canonical, "alias {alias}");
            assert_eq!(
                normalize_lang(canonical),
                *canonical,
                "canonical {canonical}"
            );
        }
        assert_eq!(seen.len(), LANG_ALIASES.len());
    }

    #[test]
    fn original_language_accepts_both_arr_spellings() {
        // Sonarr/Radarr `originalLanguage`: object or bare string, and a
        // blank/null/absent value degrades to None rather than erroring.
        assert_eq!(
            original_language(&serde_json::json!({"id": 3, "name": "Japanese"})).as_deref(),
            Some("Japanese")
        );
        assert_eq!(
            original_language(&serde_json::json!("German")).as_deref(),
            Some("German")
        );
        assert_eq!(original_language(&serde_json::json!({"name": "  "})), None);
        assert_eq!(original_language(&serde_json::json!(null)), None);
        assert_eq!(original_language(&serde_json::json!(7)), None);
    }

    #[test]
    fn display_names_are_honest_per_language() {
        assert_eq!(display_name("fr"), "French");
        assert_eq!(display_name("fre"), "French");
        assert_eq!(display_name("de"), "German");
        assert_eq!(display_name("ind"), "Indonesian");
        // The two long-standing names stay byte-identical.
        assert_eq!(display_name("en"), "English");
        assert_eq!(display_name("jpn"), "Japanese");
        assert_eq!(display_name("xx"), "Unknown");
    }

    #[test]
    fn only_japanese_sources_need_the_foreign_guard() {
        assert!(needs_foreign_guard("ja"));
        assert!(needs_foreign_guard("Japanese"));
        // A latin source is mostly-latin (and a Chinese one is hanzi without
        // kana): the guard would turn every line into a placeholder.
        assert!(!needs_foreign_guard("fr"));
        assert!(!needs_foreign_guard("en"));
        assert!(!needs_foreign_guard("id"));
        assert!(!needs_foreign_guard("zh"));
    }

    #[test]
    fn canonical_hi_path() {
        assert_eq!(
            canonical_target_sidecar("/m/ep.mkv-stem", "id"),
            "/m/ep.mkv-stem.id.hi.srt"
        );
    }

    #[test]
    fn stem_and_cjk_helpers() {
        assert_eq!(stem_of("/m/ep.mkv"), "/m/ep");
        assert_eq!(stem_of("noext"), "noext");
        assert!(is_kana('あ') && is_cjk('あ'));
        assert!(is_cjk('漢') && !is_kana('漢'));
        assert!(!is_cjk('a'));
    }

    #[test]
    fn replaceable_lists_canonical_first() {
        let v = replaceable_target_sidecar_paths("/m/ep", "ja");
        assert_eq!(v[0], "/m/ep.ja.hi.srt");
        assert!(!v.iter().any(|p| p.contains("forced")));
    }
}
