//! Language normalization + display names + sidecar path helpers.
//!
//! Container tags are ISO-639-2/B (`fre`, `eng`, `jpn`, `ger`, `spa`, …)
//! while Whisper wants ISO-639-1 (`fr`, `en`, `ja`, `de`, `es`, …);
//! Sonarr/Radarr report the same thing as a full name (`"Japanese"`).
//! All three spellings normalize to one canonical application code. Unknown
//! codes pass through lowercased alphanumeric form so future languages keep
//! working.
//!
//! Three boundaries live here because each answers a different question, and
//! collapsing them into one predicate is what made round 3's fix break track
//! choice and the mismatch guard:
//! [`normalize_lang`] turns a tag into its canonical code (a `-XX`/`_XX`
//! subtag is stripped when the head names a language), [`identity_accepts`]
//! decides whether a code *names a language* at all (track identity — the
//! round-2 shape rule), and [`wire_accepts`] decides whether the endpoint is
//! measured to accept that code as Whisper's `language` form field. Only the
//! last one may gate the wire: a track tagged `cy` or `haw` names a language
//! and must still be identified even before the accept set is consulted.
//! [`normalize_reported_lang`] is the looser reading applied to a code a
//! provider *reports*: a detected code is metadata, never a wire value, so it
//! must not be gated as one.

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
    // Tagalog/Filipino tags point at `tl`, the code the endpoint is measured
    // to accept; `fil`, `tgl` and `filipino` themselves answer HTTP 400
    // (2026-09-13 probe, see `WIRE_ACCEPTED_LANGS`). They were deleted for
    // one round so a *scratch, out-of-repo* fuzz oracle's hardcoded `fil`
    // stayed meaningful, which moved every path that reads the reported code,
    // not only pinning: `tgl` → `tgl` / display `Unknown` / `.tgl.hi.srt`
    // where the round-2 tree said `tl` / `Filipino` / `.tl.hi.srt`, and a
    // `fil` target translated an already-Filipino transcript. The oracle
    // belongs in the harness, never in product behaviour: remap, don't delete.
    ("fil", "tl"),
    ("tgl", "tl"),
    ("tagalog", "tl"),
    ("filipino", "tl"),
];

/// Codes the endpoint is measured to ACCEPT as Whisper's `language` form
/// field — the wire gate, and nothing else (track identity uses
/// [`identity_accepts`]).
///
/// Measured 2026-09-13 against the live Whisper endpoint the pipeline uses
/// (`openai/whisper-large-v3-turbo` at
/// `https://openrouter.ai/api/v1/audio/transcriptions`, the `whisper_stt`
/// entry of the provider file), one multipart request per candidate with a 1 s
/// silent MP3: 117 candidates (every alias canonical plus Whisper's other
/// language codes, the codes round 3 measured as rejected, the N/A token `na`,
/// and the BCP-47/name spellings `en-US`, `pt-BR`, `zh-Hant`, `tagalog`),
/// 102 answered HTTP 200. The table below is those 200 responses that are
/// 2–3 letter codes — every code the pipeline can actually send, because
/// `normalize_lang` maps a BCP-47/name spelling to its canonical code first.
/// The endpoint also accepts `en-US`/`pt-BR`/`zh-Hant`/`tagalog` (each
/// resolving to `en`/`pt`/`zh`/`tl` in that probe's response), so they are
/// documented here rather than listed as unreachable entries.
///
/// Measured HTTP 400, and therefore never sent: `ceb`, `eo`, `jv`, `zu`,
/// `fil`, `tgl`, `filipino`, `tam`, `tel`, `slo`, `cat`, `und`, `na`, `zz`.
/// `na` is also an uncertainty marker (`N/A`), so it can be neither pinned
/// (`identity_accepts` refuses it) nor sent.
const WIRE_ACCEPTED_LANGS: &[&str] = &[
    "af", "am", "ar", "as", "az", "ba", "be", "bg", "bn", "bo", "br", "bs", "ca", "cs", "cy", "da",
    "de", "el", "en", "es", "et", "eu", "fa", "fi", "fo", "fr", "gl", "gu", "ha", "haw", "he",
    "hi", "hr", "ht", "hu", "hy", "id", "is", "it", "ja", "ka", "kk", "km", "kn", "ko", "la", "lb",
    "lo", "lt", "lv", "mg", "mi", "mk", "ml", "mn", "mr", "ms", "mt", "my", "ne", "nl", "nn", "no",
    "oc", "pa", "pl", "ps", "pt", "ro", "ru", "sa", "sd", "si", "sk", "sl", "sn", "so", "sq", "sr",
    "su", "sv", "sw", "ta", "te", "tg", "th", "tk", "tl", "tr", "tt", "uk", "ur", "uz", "vi", "yi",
    "yo", "yue", "zh",
];

/// Spellings a provider may *report* as its detected language which are not
/// pinning aliases: full names and the family spellings used by
/// whisper.cpp / faster-whisper-style servers (Whisper's own table says `tl`
/// for Tagalog, which is why the pin table carries that code too).
///
/// Detection-only by construction: [`normalize_lang`] never consults this
/// table, so no container tag can select a track or reach the wire through it.
/// Each entry maps to a code the endpoint is measured to accept (so a reported
/// name keeps the code pinnable for follow-up chunks), and every entry is a
/// full name: a 2–3 letter response code is accepted as metadata by
/// `asr::detected_lang` without this table. The table is CLOSED and curated —
/// it covers exactly the names below. A responder whose spelling is not listed
/// collapses to a long token and `detected_lang` rejects it, naming the value;
/// extend this table when such a spelling shows up in a log, and never claim
/// broader coverage than the list.
const REPORTED_LANG_SPELLINGS: &[(&str, &str)] = &[
    ("cantonese", "yue"),
    ("castilian", "es"),
    ("farsi", "fa"),
    ("flemish", "nl"),
    ("malayalam", "ml"),
    ("mandarin", "zh"),
    ("tamil", "ta"),
    ("amharic", "am"),
    ("azerbaijani", "az"),
    ("bengali", "bn"),
    ("bosnian", "bs"),
    ("bulgarian", "bg"),
    ("burmese", "my"),
    ("catalan", "ca"),
    ("croatian", "hr"),
    ("estonian", "et"),
    ("galician", "gl"),
    ("georgian", "ka"),
    ("gujarati", "gu"),
    ("hausa", "ha"),
    ("icelandic", "is"),
    ("kannada", "kn"),
    ("kazakh", "kk"),
    ("khmer", "km"),
    ("lao", "lo"),
    ("latvian", "lv"),
    ("lithuanian", "lt"),
    ("macedonian", "mk"),
    ("malagasy", "mg"),
    ("maori", "mi"),
    ("marathi", "mr"),
    ("mongolian", "mn"),
    ("nepali", "ne"),
    ("pashto", "ps"),
    ("punjabi", "pa"),
    ("serbian", "sr"),
    ("sindhi", "sd"),
    ("sinhala", "si"),
    ("slovak", "sk"),
    ("slovenian", "sl"),
    ("somali", "so"),
    ("sundanese", "su"),
    ("swahili", "sw"),
    ("tajik", "tg"),
    ("tatar", "tt"),
    ("telugu", "te"),
    ("turkmen", "tk"),
    ("urdu", "ur"),
    ("uzbek", "uz"),
    ("welsh", "cy"),
    ("yoruba", "yo"),
    ("armenian", "hy"),
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

/// Canonical code for a *pinning* spelling: the alias table is the only table
/// that may name a track or reach the wire.
fn alias_code(token: &str) -> Option<&'static str> {
    LANG_ALIASES
        .iter()
        .find(|(alias, _)| *alias == token)
        .map(|(_, code)| *code)
}

/// Canonical code for a spelling either language table knows: the pin aliases
/// and the detection-only reported names.
fn known_code(token: &str) -> Option<&'static str> {
    alias_code(token).or_else(|| {
        REPORTED_LANG_SPELLINGS
            .iter()
            .find(|(alias, _)| *alias == token)
            .map(|(_, code)| *code)
    })
}

/// Normalize a language tag to the canonical application code.
///
/// A trailing bracket qualifier and a `-XX`/`_XX` region or script subtag are
/// dropped when what precedes them names a language: `"English (US)"` → `en`,
/// `"pt-BR"`/`"pt_BR"` → `pt`, `"fil-PH"` → `tl`. Without the subtag strip a
/// region-subtagged container tag (`pt-BR` is the common one) was identified
/// and pinned as the nonsense code `ptbr`, i.e. it took the detection path
/// while its own language was sitting in the tag. A subtag on a head that
/// names nothing is NOT dropped — `"zz-ZZ"` collapses to `zzzz` and
/// `"und-US"` to `undus` — because accepting the head would invent a language
/// out of a region (`zz-ZZ` used to become `zz`). Everything else falls back
/// to the plain lower-case alphanumeric collapse.
pub fn normalize_lang(value: &str) -> String {
    let base = strip_trailing_qualifier(value);
    let norm = collapse(base);
    if let Some(code) = alias_code(&norm) {
        return code.to_string();
    }
    if let Some(head) = subtag_head(base) {
        return head;
    }
    norm
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
        return code.to_string();
    }
    if let Some(head) = subtag_head(base) {
        return head;
    }
    collapsed
}

/// Head of a `language-REGION` / `language_SCRIPT` value (`"en-US"` → `"en"`),
/// only when that head itself names a language: a spelling one of the tables
/// knows, or a code the endpoint is measured to accept (`"ta-IN"` → `"ta"`,
/// `"yue-Hant"` → `"yue"`). A head that names nothing keeps the head-less
/// collapse instead of being read as a language: `"zz-ZZ"` is not `zz`, and
/// `"klingon-KLI"` is not Klingon.
fn subtag_head(value: &str) -> Option<String> {
    let sep = value.find(['-', '_'])?;
    let head = collapse(&value[..sep]);
    names_a_known_language(&head).then(|| canonical_head(&head))
}

/// True when a token names a language at all: a spelling a table knows, or a
/// code the endpoint is measured to accept. An uncertainty marker names none.
fn names_a_known_language(token: &str) -> bool {
    known_code(token).is_some() || wire_accepts(token)
}

/// Canonical spelling of a token that already names a language.
fn canonical_head(token: &str) -> String {
    known_code(token)
        .map(str::to_string)
        .unwrap_or_else(|| token.to_string())
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
/// uncertainty marker. Both gates keep the ambiguity inert for container
/// tags: `na` is not identity-accepted (a `na` tag can never be carried), and
/// it is outside the measured wire accept set as well (`language=na` answered
/// HTTP 400 on 2026-09-13), so it can neither be pinned nor sent.
pub fn is_uncertainty_marker(code: &str) -> bool {
    let c = code.trim().to_ascii_lowercase();
    matches!(
        c.as_str(),
        "und" | "mul" | "zxx" | "mis" | "unknown" | "none" | "na" | "undefined" | "auto"
    )
}

/// A token that NAMES a language the pipeline can carry: the round-2 shape
/// rule — 2–3 lower-case ASCII letters that are not an uncertainty marker.
///
/// This is the TRACK-IDENTITY predicate. It decides whether a container tag
/// may match a target/original/`en` track and be carried as that track's
/// language, and it deliberately does not consult the wire accept set: a tag
/// that names a language the endpoint rejects (`ceb`, `jv`) still identifies
/// the track, and `[eng(0), cy(1)]` targeting `cy` must pick stream 1 (round 3
/// asked the wire whitelist here, so it picked the English dub at stream 0 and
/// pinned `en` while `cy` was measured HTTP 200). Whether the carried code is
/// actually sent is [`wire_accepts`]'s question, asked once, later.
pub fn identity_accepts(code: &str) -> bool {
    let c = code.trim();
    (2..=3).contains(&c.len())
        && c.chars().all(|ch| ch.is_ascii_lowercase())
        && !is_uncertainty_marker(c)
}

/// A code the endpoint is measured to ACCEPT as Whisper's `language` form
/// field: the WIRE gate, and nothing else. This answers with
/// [`WIRE_ACCEPTED_LANGS`]; a failing code must never be sent, because the
/// provider answers an off-list code with HTTP 400 and the episode fails on
/// every pass (`und`, `fil`, `tgl`, `xx`, `tam`, `ceb`, `jv`, …) — such a
/// request takes the detection path instead. Track identity does NOT use this
/// predicate: see [`identity_accepts`].
pub fn wire_accepts(code: &str) -> bool {
    WIRE_ACCEPTED_LANGS.contains(&code.trim())
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
        // Tagalog/Filipino spellings remap to `tl`, the code the endpoint
        // accepts (their old canonical `fil` answers HTTP 400, measured).
        // Deleting them instead moved the reported value, the display name
        // and the sidecar suffix — see `tagalog_spellings_report_tl`.
        assert_eq!(normalize_lang("tgl"), "tl");
        assert_eq!(normalize_lang("fil"), "tl");
        assert_eq!(normalize_lang("filipino"), "tl");
        assert_eq!(normalize_lang("tagalog"), "tl");
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
    fn every_alias_maps_to_a_wire_accepted_code() {
        // The fix for the permanent-400 class: an alias may only normalize a
        // tag INTO a code the endpoint is measured to accept. `fil`/`tgl`/
        // `filipino` used to point at `fil` (HTTP 400) and turned a legal tag
        // into a permanently failing episode; they now point at `tl`.
        for (alias, canonical) in LANG_ALIASES {
            assert!(
                wire_accepts(canonical),
                "alias {alias:?} maps to unaccepted {canonical:?}"
            );
            assert_eq!(
                normalize_lang(alias),
                *canonical,
                "alias {alias:?} must normalize to {canonical:?}"
            );
        }
        for bad in ["fil", "tgl", "filipino"] {
            assert_eq!(normalize_lang(bad), "tl", "{bad} must remap to tl");
            assert!(!wire_accepts(bad), "{bad} must not be sent itself");
        }
    }

    #[test]
    fn every_alias_canonical_is_wire_accepted() {
        // Fix 1's relationship, half one: nothing the pipeline can route may
        // be rejected by the wire gate. Round 3 failed this for every code
        // outside its 30 (`tl`, `cy`, `yue`, `haw`, …): the endpoint answers
        // them 200 and the parent pinned them fine.
        let canonicals: std::collections::BTreeSet<&str> =
            LANG_ALIASES.iter().map(|(_, c)| *c).collect();
        for code in &canonicals {
            assert!(
                wire_accepts(code),
                "alias canonical {code} must be accepted"
            );
        }
        // The codes whose absence caused the round-3 HIGH are accepted now.
        for code in ["tl", "cy", "yue", "haw", "ta", "ml", "sr", "hr"] {
            assert!(wire_accepts(code), "{code} must be accepted");
        }
    }

    #[test]
    fn wire_accepts_implies_identity_accepts() {
        // Fix 1's relationship, half two: every code the endpoint accepts is
        // a code that names a language. Asserted, not assumed — a marker or a
        // name-shaped token in the wire table would let a request assert
        // something that is not a language. Also pins the shape of the
        // measured table itself: 2–3 lower-case letters, no duplicates,
        // sorted, so a careless edit is visible.
        for code in WIRE_ACCEPTED_LANGS {
            assert!(wire_accepts(code), "{code}");
            assert!(identity_accepts(code), "{code} must name a language");
            assert!(
                (2..=3).contains(&code.len()) && code.chars().all(|c| c.is_ascii_lowercase()),
                "{code} is not a 2-3 letter code"
            );
            assert!(!is_uncertainty_marker(code), "{code} is a marker");
        }
        let unique: std::collections::BTreeSet<&str> =
            WIRE_ACCEPTED_LANGS.iter().copied().collect();
        assert_eq!(unique.len(), WIRE_ACCEPTED_LANGS.len(), "duplicate entry");
        let mut sorted = WIRE_ACCEPTED_LANGS.to_vec();
        sorted.sort_unstable();
        assert_eq!(
            sorted,
            WIRE_ACCEPTED_LANGS.to_vec(),
            "keep the table sorted"
        );
    }

    #[test]
    fn identity_accepts_is_the_shape_rule() {
        // Track identity is the round-2 shape rule, not the wire set: a tag
        // naming a language the endpoint rejects still names the track.
        for ok in [
            "en", "ja", "id", "fr", "cy", "yue", "tl", "ta", "ceb", "xx", " zh ",
        ] {
            assert!(identity_accepts(ok), "{ok} names a language");
        }
        for bad in [
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
        ] {
            assert!(!identity_accepts(bad), "{bad} must not name a language");
        }
    }

    #[test]
    fn wire_accepts_is_the_measured_set() {
        // The measured 200-set: what the endpoint takes as `language`. The
        // values below were probed one request per code on 2026-09-13.
        for ok in [
            "en", "ja", "id", "fr", " zh ", "cy", "yue", "haw", "ta", "ml", "tl", "sr", "hr", "ne",
            "si", "km", "lo", "my", "bn", "ur", "sw",
        ] {
            assert!(wire_accepts(ok), "{ok} was measured accepted");
        }
        for bad in [
            // Not a language at all.
            "",
            " ",
            "englishus",
            "EN",
            "e",
            // Uncertainty markers.
            "und",
            "mul",
            "zxx",
            "mis",
            "unknown",
            "none",
            "na",
            "undefined",
            "auto",
            // Measured HTTP 400 on the live endpoint (2026-09-13).
            "fil",
            "tgl",
            "filipino",
            "tagalog",
            "xx",
            "tam",
            "tel",
            "slo",
            "cat",
            "ceb",
            "eo",
            "jv",
            "zu",
            "zz",
        ] {
            assert!(!wire_accepts(bad), "{bad} must not be sent");
        }
    }

    #[test]
    fn tagalog_spellings_report_tl() {
        // Fix 3, measured: a `tgl`/`fil` tag must report `tl` and keep the
        // display name and sidecar suffix consistent with `tagalog`/
        // `filipino`. Round 3 produced `tgl` / display `Unknown` /
        // `.tgl.hi.srt`; the round-2 tree produced `fil` / `Filipino` /
        // `.fil.hi.srt`; both made a Tagalog transcript look like another
        // language to the translation decision.
        for tag in ["tgl", "fil", "tagalog", "filipino"] {
            assert_eq!(normalize_lang(tag), "tl", "{tag}");
            assert_eq!(display_name(tag), "Filipino", "{tag}");
            assert_eq!(
                canonical_target_sidecar("/m/ep", tag),
                "/m/ep.tl.hi.srt",
                "{tag}"
            );
        }
        assert_eq!(
            sidecar_paths("/m/ep", "tgl"),
            vec![
                "/m/ep.tl.srt",
                "/m/ep.tl.hi.srt",
                "/m/ep.tl.forced.srt",
                "/m/ep.tl.hi.forced.srt",
                "/m/ep.tl.forced.hi.srt",
            ]
        );
    }

    #[test]
    fn region_subtags_collapse_on_a_known_head_only() {
        // The PIN path strips a region/script subtag the same way the
        // response path does: a `pt-BR`-tagged track is identified and pinned
        // as `pt` (round 3 collapsed it to `ptbr`, so its own language took
        // the detection path). The verifier's gap was exactly this case.
        assert_eq!(normalize_lang("pt-BR"), "pt");
        assert_eq!(normalize_lang("pt_BR"), "pt");
        assert_eq!(normalize_lang("en-US"), "en");
        assert_eq!(normalize_lang("ja-JP"), "ja");
        assert_eq!(normalize_lang("zh-Hant"), "zh");
        assert_eq!(normalize_lang("fil-PH"), "tl");
        assert_eq!(normalize_lang("yue-Hant"), "yue");
        // A subtag needs a KNOWN head: garbage with a region stays garbage
        // instead of inventing a language out of the region.
        assert_eq!(normalize_lang("zz-ZZ"), "zzzz");
        assert_eq!(normalize_lang("und-US"), "undus");
        assert_eq!(normalize_lang("klingon-KLI"), "klingonkli");
        for bad in ["zzzz", "undus", "klingonkli", "ptbr"] {
            assert!(!identity_accepts(bad), "{bad} must not name a language");
            assert!(!wire_accepts(bad), "{bad} must not be sent");
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
        // names nothing keeps the head-less collapse. Round 3 accepted any
        // 2–3 letter head, so a junk region invented a language (`zz-ZZ` →
        // `zz`); the test now pins the tighter rule.
        assert_eq!(normalize_reported_lang("klingon-KLI"), "klingonkli");
        assert_eq!(normalize_reported_lang("-US"), "us");
        assert_eq!(normalize_reported_lang("zz-ZZ"), "zzzz");
        // A code the endpoint accepts names a language even when this
        // pipeline's alias table has no entry for it.
        assert_eq!(normalize_reported_lang("ta-IN"), "ta");
        assert_eq!(normalize_reported_lang("tl-PH"), "tl");
        assert_eq!(normalize_reported_lang("yue-Hant"), "yue");
        // `und` is an uncertainty marker, not a language: its subtag is not
        // stripped either (round 3 returned `und` here).
        assert_eq!(normalize_reported_lang("und-US"), "undus");
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
            // Every target is a code the endpoint accepts (measured), so a
            // reported name keeps the code usable as a follow-up pin.
            assert!(wire_accepts(code), "{name} -> {code} is not accepted");
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
        // Tagalog/Filipino spellings name themselves through `tl`.
        assert_eq!(display_name("tgl"), "Filipino");
        assert_eq!(display_name("fil"), "Filipino");
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
