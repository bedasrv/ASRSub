//! Language normalization + display names + sidecar path helpers.
//!
//! Container tags are ISO-639-2/B (`fre`, `eng`, `jpn`, `ger`, `spa`, …)
//! while Whisper wants ISO-639-1 (`fr`, `en`, `ja`, `de`, `es`, …);
//! Sonarr/Radarr report the same thing as a full name (`"Japanese"`).
//! All three spellings normalize to one canonical application code. Unknown
//! codes pass through lowercased alphanumeric form so future languages keep
//! working.

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

/// Normalize a language tag to the canonical application code.
pub fn normalize_lang(value: &str) -> String {
    let norm: String = value
        .trim()
        .to_lowercase()
        .chars()
        .filter(|c| c.is_ascii_alphanumeric())
        .collect();
    match LANG_ALIASES.iter().find(|(alias, _)| *alias == norm) {
        Some((_, canonical)) => (*canonical).to_string(),
        None => norm,
    }
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
