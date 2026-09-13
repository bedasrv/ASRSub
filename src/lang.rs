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
//! and [`is_usable_code`] decides whether that code may be pinned as the
//! transcription language. [`is_usable_code`] answers with
//! [`PINNABLE_LANGS`], the set the live endpoint was measured to accept, so
//! `und`/`unknown`/`""` and every code the provider rejects (`fil`, `tgl`,
//! `tam`, …) take the detection path instead of failing the episode with
//! HTTP 400. [`normalize_reported_lang`] is the looser reading applied to a
//! code a provider *reports*: a detected code is metadata, never a wire
//! value, so it must not be gated as one.

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
    // `fil`/`tgl`/`filipino` are deliberately ABSENT: the provider answers
    // `language=fil` and `language=tgl` with HTTP 400 (measured; see
    // [`PINNABLE_LANGS`]), and an alias may only point at a pinnable code
    // (`every_alias_maps_to_a_pinnable_code`). A Tagalog/Filipino track
    // therefore takes the detection path, which stores what the provider
    // reports (`tl`, via [`REPORTED_LANG_SPELLINGS`]) instead of pinning a
    // code the endpoint rejects.
];

/// Canonical codes the pipeline may pin as Whisper's `language` field.
///
/// Measured 2026-09-13 against the live Whisper endpoint the pipeline uses
/// (`openai/whisper-large-v3-turbo` at
/// `https://openrouter.ai/api/v1/audio/transcriptions`, the `whisper_stt`
/// entry of the provider file), one multipart request per code with a 1 s
/// silent MP3: every code below answered HTTP 200. `fil` and `tgl` answered
/// HTTP 400 (hence no alias may normalize into them), and so did `xx`,
/// `tam`, `tel`, `slo`, `cat` and `und`. 200 for codes the pipeline does not
/// carry (e.g. `cy`, `yue`, `haw`) is NOT a licence to pin them: this list is
/// exactly the canonical codes of [`LANG_ALIASES`], the languages the
/// pipeline can route. Anything else takes the detection path — no
/// `language` field on the wire — rather than risk a permanent 400.
const PINNABLE_LANGS: &[&str] = &[
    "ar", "cs", "da", "de", "el", "en", "es", "fa", "fi", "fr", "he", "hi", "hu", "id", "it", "ja",
    "ko", "ms", "nl", "no", "pl", "pt", "ro", "ru", "sv", "th", "tr", "uk", "vi", "zh",
];

/// Spellings a provider may *report* as its detected language which are not
/// pinning aliases: full names and the family spellings used by
/// whisper.cpp / faster-whisper-style servers (Whisper's own table says `tl`
/// for Tagalog, which is also why the pin table has no Tagalog entry).
///
/// Detection-only by construction: [`normalize_lang`] never consults this
/// table, so no container tag can select a track or reach the wire through
/// it. Their targets need not be pinnable — a detected code is metadata
/// (stored as `source_lang`, compared for `needs_translate`) and an
/// unpinnable one is dropped from follow-up chunks by `asr`'s filter.
const REPORTED_LANG_SPELLINGS: &[(&str, &str)] = &[
    ("tamil", "ta"),
    ("malayalam", "ml"),
    ("cantonese", "yue"),
    ("mandarin", "zh"),
    ("castilian", "es"),
    ("farsi", "fa"),
    ("flemish", "nl"),
    ("tagalog", "tl"),
    ("filipino", "tl"),
];

/// Drop a trailing bracket qualifier: `"English (US)"` → `"English"`,
/// `"Chinese (Traditional)"` → `"Chinese"`, `"French [FR]"` → `"French"`.
/// The qualifier refines a language the tag before it already names, so
/// without this `"English (US)"` collapsed to the nonsense code
/// `englishus`. Applied repeatedly (stacked qualifiers) and only when the
/// group closes the string; a group that does not close it is left to the
/// alphanumeric collapse. The strip is unconditional, so an *unmapped* tag
/// ending in a closed group loses it too: `"Klingon (KLI)"` → `"klingon"`,
/// `"(US)"` → `""` (a group cannot name a language on its own). Both are
/// pinned by tests rather than assumed.
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

/// Lower-case alphanumeric collapse: the shape every lookup key is stored in.
fn collapse(value: &str) -> String {
    value
        .to_lowercase()
        .chars()
        .filter(|c| c.is_ascii_alphanumeric())
        .collect()
}

/// Canonical code for a *pinning* spelling, if either language table knows it.
fn known_code(token: &str) -> Option<String> {
    LANG_ALIASES
        .iter()
        .chain(REPORTED_LANG_SPELLINGS.iter())
        .find(|(alias, _)| *alias == token)
        .map(|(_, code)| (*code).to_string())
}

/// Normalize a language tag to the canonical application code.
pub fn normalize_lang(value: &str) -> String {
    let norm = collapse(strip_trailing_qualifier(value));
    match LANG_ALIASES.iter().find(|(alias, _)| *alias == norm) {
        Some((_, canonical)) => (*canonical).to_string(),
        None => norm,
    }
}

/// Normalize a language a provider *reported* in a `verbose_json` response.
///
/// Response values are not wire values: a detected code is stored and
/// compared, never sent as a pin, so this reading is deliberately looser
/// than [`normalize_lang`]. It accepts, in order: a pin-table spelling
/// (`"French"`, `"JA"`), a [`REPORTED_LANG_SPELLINGS`] name (`"tamil"` →
/// `"ta"`), and a `-XX`/`_XX` region or script subtag on a head that names a
/// language (`"en-US"` → `"en"`, `"zh-Hant"` → `"zh"`, `"pt-BR"` → `"pt"`).
/// Anything else is returned as the plain alphanumeric collapse, for the
/// caller to accept (a 2–3 letter token is metadata) or reject.
pub fn normalize_reported_lang(value: &str) -> String {
    let base = strip_trailing_qualifier(value);
    let collapsed = collapse(base);
    if let Some(code) = known_code(&collapsed) {
        return code;
    }
    if let Some(head) = subtag_head(base) {
        return known_code(&head).unwrap_or(head);
    }
    collapsed
}

/// Head of a `language-REGION` / `language_SCRIPT` value (`"en-US"` →
/// `"en"`), only when that head itself names a language: a spelling one of
/// the tables knows, or a plain 2–3 letter token (the same shape the caller
/// accepts as metadata). `"klingon-KLI"` has no such head, so it keeps the
/// head-less collapse instead of being read as Klingon.
fn subtag_head(value: &str) -> Option<String> {
    let sep = value.find(['-', '_'])?;
    let head = collapse(&value[..sep]);
    let names_a_language = known_code(&head).is_some()
        || ((2..=3).contains(&head.len()) && head.chars().all(|c| c.is_ascii_lowercase()));
    names_a_language.then_some(head)
}

/// A token that means "no language" rather than naming one: what muxers write
/// for an unset tag (`und`), what a provider answers when detection is
/// inconclusive (`unknown`, `none`, `N/A`), and the collective/undefined
/// markers (`mul`, `zxx`, `mis`). Such a token may never be pinned (the
/// whitelist excludes it) and may never become an episode's source language.
/// Recognized case-insensitively, so a raw tag can be tested before it goes
/// through [`normalize_lang`].
///
/// `na` is listed too, and the ambiguity is inherent: it is ISO 639-1 for
/// Nauru, but normalization makes it indistinguishable from `N/A`, and a
/// responder that says `N/A` means "unknown". The honest reading is the
/// uncertainty marker. With the pin whitelist in place the ambiguity is
/// inert for container tags — a `na` tag is simply not pinnable, so it takes
/// the detection path like any other code we do not pin.
pub fn is_uncertainty_marker(code: &str) -> bool {
    let c = code.trim().to_ascii_lowercase();
    matches!(
        c.as_str(),
        "und" | "mul" | "zxx" | "mis" | "unknown" | "none" | "na" | "undefined" | "auto"
    )
}

/// A code the pipeline may pin as the transcription language: a canonical
/// code from [`PINNABLE_LANGS`], the set the provider was measured to
/// accept. A failing code means *unknown* or *unpinnable*: the caller must
/// take the detection path, because sending an unlisted code as Whisper's
/// `language` form field is what made the provider answer HTTP 400 and left
/// the episode failing on every pass (`und`, `fil`, `tgl`, `xx`, `tam`, …).
pub fn is_usable_code(code: &str) -> bool {
    PINNABLE_LANGS.contains(&code.trim())
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
        // The codes a provider may report for these languages (the pin table
        // has no Tagalog entry: the endpoint rejects `fil`/`tgl`), so a
        // detected source still names itself in the translation prompt.
        "tl" => "Filipino",
        "ta" => "Tamil",
        "ml" => "Malayalam",
        "yue" => "Cantonese",
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
        // Tagalog/Filipino spellings are deliberately NOT aliased: their
        // canonical would be `fil`, which the provider answers with HTTP 400.
        // They collapse to themselves and take the detection path.
        assert_eq!(normalize_lang("tgl"), "tgl");
        assert_eq!(normalize_lang("fil"), "fil");
        assert_eq!(normalize_lang("filipino"), "filipino");
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
        // The strip is unconditional, so an UNMAPPED tag ending in a closed
        // bracket group loses that group (the old comment here claimed such
        // garbage "collapses exactly as before", which was false):
        assert_eq!(normalize_lang("Klingon (KLI)"), "klingon");
        assert_eq!(normalize_lang("(US)"), "");
        // A bracket that does not close the string is not a qualifier, and
        // neither is a bracket in the middle.
        assert_eq!(normalize_lang("English (US) extra"), "englishusextra");
        assert_eq!(normalize_lang("Klingon [KLI"), "klingonkli");
        assert_eq!(normalize_lang(""), "");
    }

    #[test]
    fn every_alias_maps_to_a_pinnable_code() {
        // The fix for the permanent-400 class: an alias may only normalize a
        // tag INTO a code the provider is measured to accept. `tgl`/`fil`/
        // `filipino` used to point at `fil` (HTTP 400) and turned a legal tag
        // into a permanently failing episode.
        for (alias, canonical) in LANG_ALIASES {
            assert!(
                is_usable_code(canonical),
                "alias {alias:?} maps to unpinnable {canonical:?}"
            );
            assert_eq!(
                normalize_lang(alias),
                *canonical,
                "alias {alias:?} must normalize to {canonical:?}"
            );
        }
        for bad in ["fil", "tgl", "filipino"] {
            assert_eq!(normalize_lang(bad), bad, "{bad} must not be aliased");
            assert!(!is_usable_code(bad), "{bad} must not be pinnable");
        }
    }

    #[test]
    fn pinnable_langs_are_canonical_measured_codes() {
        // The whitelist is exactly the alias table's canonical set: no code
        // can be pinned that the pipeline cannot route (or vice versa).
        let canonicals: std::collections::BTreeSet<&str> =
            LANG_ALIASES.iter().map(|(_, c)| *c).collect();
        let pinned: std::collections::BTreeSet<&str> = PINNABLE_LANGS.iter().copied().collect();
        assert_eq!(pinned, canonicals, "whitelist must equal the canonical set");
        // Every entry is a plain 2-letter lower-case code: nothing shaped
        // like a name or an uncertainty marker can be pinned.
        for code in PINNABLE_LANGS {
            assert_eq!(code.len(), 2, "{code} must be a 2-letter code");
            assert!(code.chars().all(|c| c.is_ascii_lowercase()), "{code}");
            assert!(!is_uncertainty_marker(code), "{code} is a marker");
        }
    }

    #[test]
    fn usable_codes_are_the_measured_whitelist_only() {
        for ok in ["en", "ja", "id", "fr", " zh "] {
            assert!(is_usable_code(ok), "{ok} must be usable");
        }
        for bad in [
            // Not a language at all.
            "",
            " ",
            "englishus",
            "jajp",
            "zhhant",
            "klingon",
            "e",
            "EN",
            // Uncertainty markers: what muxers/responders write when there is
            // no language (`na` is Nauru in ISO 639-1, indistinguishable from
            // `N/A` after normalization — see `is_uncertainty_marker`).
            "und",
            "mul",
            "zxx",
            "mis",
            "unknown",
            "none",
            "na",
            "undefined",
            "auto",
            // Measured HTTP 400 on the live endpoint.
            "fil",
            "tgl",
            "xx",
            "tam",
            "tel",
            "slo",
            "cat",
            // Accepted by the provider but not languages this pipeline
            // carries an alias for, so they are never pinned.
            "cy",
            "yue",
            "haw",
            "tl",
        ] {
            assert!(!is_usable_code(bad), "{bad} must not be usable");
        }
    }

    #[test]
    fn reported_langs_normalize_names_and_regions() {
        // The shapes that used to error with "no detected language" — false,
        // and a permanent per-episode failure: the code is metadata, never a
        // wire value, so it must be normalized rather than gated.
        assert_eq!(normalize_reported_lang("tamil"), "ta");
        assert_eq!(normalize_reported_lang("malayalam"), "ml");
        assert_eq!(normalize_reported_lang("cantonese"), "yue");
        assert_eq!(normalize_reported_lang("Tagalog"), "tl");
        assert_eq!(normalize_reported_lang("filipino"), "tl");
        // Region/script subtags on a head that names a language.
        assert_eq!(normalize_reported_lang("en-US"), "en");
        assert_eq!(normalize_reported_lang("zh-Hant"), "zh");
        assert_eq!(normalize_reported_lang("pt-BR"), "pt");
        assert_eq!(normalize_reported_lang("ja_JP"), "ja");
        // The pin table first: names and codes it already carries.
        assert_eq!(normalize_reported_lang("French"), "fr");
        assert_eq!(normalize_reported_lang("Japanese (JP)"), "ja");
        assert_eq!(normalize_reported_lang("EN"), "en");
        // Unknown values collapse as before, for the caller to judge.
        assert_eq!(normalize_reported_lang("ta"), "ta");
        assert_eq!(normalize_reported_lang("englishus"), "englishus");
        assert_eq!(normalize_reported_lang("(US)"), "");
        assert_eq!(normalize_reported_lang(""), "");
    }

    #[test]
    fn region_subtag_is_stripped_only_for_a_known_head() {
        // A subtag refines a language the head already names; a head that
        // names nothing keeps the head-less collapse.
        assert_eq!(normalize_reported_lang("klingon-KLI"), "klingonkli");
        assert_eq!(normalize_reported_lang("-US"), "us");
        // A plain 2–3 letter head counts as naming a language (the shape the
        // caller accepts as metadata), so its subtag is dropped — including
        // a head this pipeline does not pin.
        assert_eq!(normalize_reported_lang("ta-IN"), "ta");
        assert_eq!(normalize_reported_lang("tl-PH"), "tl");
        assert_eq!(normalize_reported_lang("und-US"), "und");
    }

    #[test]
    fn reported_spellings_are_detection_only() {
        // The reported-names table must never become a pinning alias: a
        // container tag cannot select a track or pin a wire language
        // through a spelling the provider family happens to answer with.
        let pin_aliases: std::collections::BTreeSet<&str> =
            LANG_ALIASES.iter().map(|(a, _)| *a).collect();
        for (name, code) in REPORTED_LANG_SPELLINGS {
            assert!(!pin_aliases.contains(name), "{name} must not pin");
            assert_eq!(normalize_reported_lang(name), *code, "{name}");
            assert_eq!(normalize_lang(name), *name, "{name} must stay unpinned");
            assert!((2..=3).contains(&code.len()));
            assert!(!is_uncertainty_marker(code));
            // Idempotent: a reported code re-normalizes to itself.
            assert_eq!(normalize_reported_lang(code), *code);
        }
    }

    #[test]
    fn uncertainty_markers_are_the_no_language_tokens() {
        for m in [
            "und",
            "mul",
            "zxx",
            "mis",
            "unknown",
            "none",
            "na",
            "undefined",
            "auto",
            " NA ",
        ] {
            assert!(is_uncertainty_marker(m), "{m}");
        }
        for ok in ["en", "ja", "id", "tl", "ta"] {
            assert!(!is_uncertainty_marker(ok), "{ok}");
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
