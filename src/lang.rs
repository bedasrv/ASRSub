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

use std::sync::OnceLock;

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
    // Same class as Tagalog above: containers and callers spell Javanese `jv`
    // (ISO 639-1) while the endpoint answers `jv` with HTTP 400 and `jw` with
    // 200 (2026-09-13 probe, see `WIRE_ACCEPTED_LANGS`). Remapping keeps the
    // pin, the reported value, the display name and the sidecar suffix on one
    // code instead of dropping the tag into detection.
    ("jv", "jw"),
];

/// The `language` values the endpoint is measured to ACCEPT, each paired with
/// Whisper's own name for that code: `(code, name)`.
///
/// The **code** is the wire gate, and nothing else (track identity uses
/// [`identity_accepts`]): a code outside this table must never be sent, because
/// the provider answers an off-list code with HTTP 400 and the episode fails on
/// every pass (`und`, `fil`, `tgl`, `xx`, `tam`, `ceb`, `jv`, …) — such a
/// request takes the detection path instead.
///
/// The **name** is the single place these 100 names are written down, and the
/// reported-value path reads them from here: a provider answering a language's
/// OWN name names the language we pinned, so it must not abort the episode.
/// 22 of these names (`tibetan` for `bo`, `haitian creole` for `ht`, …) were in
/// neither [`LANG_ALIASES`] nor [`REPORTED_LANG_SPELLINGS`], so a provider that
/// answered a language's own name failed a pinned episode permanently with
/// `whisper language mismatch: pinned <code>, reported <name>`; deriving the
/// accepted names from this one table makes that class impossible rather than
/// enumerating it. [`REPORTED_LANG_SPELLINGS`] keeps only the curated extras
/// Whisper's table does not use, and [`display_name`] names ~36 codes, so
/// neither can be the source for all 100. Names are matched on the collapsed
/// token like every other lookup, so the table may spell them the way Whisper
/// does (`haitian creole`).
///
/// Measured 2026-09-13 against the live Whisper endpoint the pipeline uses
/// (`openai/whisper-large-v3-turbo` at
/// `https://openrouter.ai/api/v1/audio/transcriptions`, the `whisper_stt`
/// entry of the provider file): 118 multipart requests, one 1 s silent MP3
/// per candidate, run by the committed probe `tools/probe_wire_langs.py`; the
/// responses are kept beside it as `tools/wire_langs-20260913.csv`, and
/// `--recheck` compares that CSV with this table offline (zero requests).
///
/// The candidate universe is Whisper's own language table (`openai/whisper`
/// `whisper/tokenizer.py` `LANGUAGES`, commit
/// `86098128c0b4f24f0e2aa2994de830614b474227`, 100 entries): every code the
/// pipeline can actually send, because `normalize_lang` maps any fuller
/// spelling to its canonical 2–3 letter code before a pin is decided. All 100
/// answered HTTP 200 and echoed the code they were handed, so the table below
/// is exactly those 100 — this round added `jw` and `ln`, the two the
/// previous 98-code table was missing. Both are real losses, not cosmetics:
/// a `jw`/`ln`-tagged track threw its pin away, transcribed the first piece
/// unforced and left every later piece unguarded, so a mixed-language file
/// was committed labelled with its first piece's code.
///
/// The same 118 requests covered, and this table deliberately excludes:
/// * the four spellings unreachable by construction, each rewritten to its
///   canonical code before any wire decision — `en-US` → `en`, `pt-BR` →
///   `pt`, `zh-Hant` → `zh`, `tagalog` → `tl`. The endpoint answers all four
///   200; none of them is ever sent;
/// * `ceb`, `eo`, `jv`, `zu`, `fil`, `tgl`, `filipino`, `tam`, `tel`, `slo`,
///   `cat`, `und`, `na`, `zz`, measured HTTP 400 and therefore never sent
///   (`jv` and the Tagalog spellings are remapped by [`LANG_ALIASES`]; `na`
///   is also an uncertainty marker — `identity_accepts` refuses it — so it
///   can be neither pinned nor sent).
pub(crate) const WIRE_ACCEPTED_LANGS: &[(&str, &str)] = &[
    ("af", "afrikaans"),
    ("am", "amharic"),
    ("ar", "arabic"),
    ("as", "assamese"),
    ("az", "azerbaijani"),
    ("ba", "bashkir"),
    ("be", "belarusian"),
    ("bg", "bulgarian"),
    ("bn", "bengali"),
    ("bo", "tibetan"),
    ("br", "breton"),
    ("bs", "bosnian"),
    ("ca", "catalan"),
    ("cs", "czech"),
    ("cy", "welsh"),
    ("da", "danish"),
    ("de", "german"),
    ("el", "greek"),
    ("en", "english"),
    ("es", "spanish"),
    ("et", "estonian"),
    ("eu", "basque"),
    ("fa", "persian"),
    ("fi", "finnish"),
    ("fo", "faroese"),
    ("fr", "french"),
    ("gl", "galician"),
    ("gu", "gujarati"),
    ("ha", "hausa"),
    ("haw", "hawaiian"),
    ("he", "hebrew"),
    ("hi", "hindi"),
    ("hr", "croatian"),
    ("ht", "haitian creole"),
    ("hu", "hungarian"),
    ("hy", "armenian"),
    ("id", "indonesian"),
    ("is", "icelandic"),
    ("it", "italian"),
    ("ja", "japanese"),
    ("jw", "javanese"),
    ("ka", "georgian"),
    ("kk", "kazakh"),
    ("km", "khmer"),
    ("kn", "kannada"),
    ("ko", "korean"),
    ("la", "latin"),
    ("lb", "luxembourgish"),
    ("ln", "lingala"),
    ("lo", "lao"),
    ("lt", "lithuanian"),
    ("lv", "latvian"),
    ("mg", "malagasy"),
    ("mi", "maori"),
    ("mk", "macedonian"),
    ("ml", "malayalam"),
    ("mn", "mongolian"),
    ("mr", "marathi"),
    ("ms", "malay"),
    ("mt", "maltese"),
    ("my", "myanmar"),
    ("ne", "nepali"),
    ("nl", "dutch"),
    ("nn", "nynorsk"),
    ("no", "norwegian"),
    ("oc", "occitan"),
    ("pa", "punjabi"),
    ("pl", "polish"),
    ("ps", "pashto"),
    ("pt", "portuguese"),
    ("ro", "romanian"),
    ("ru", "russian"),
    ("sa", "sanskrit"),
    ("sd", "sindhi"),
    ("si", "sinhala"),
    ("sk", "slovak"),
    ("sl", "slovenian"),
    ("sn", "shona"),
    ("so", "somali"),
    ("sq", "albanian"),
    ("sr", "serbian"),
    ("su", "sundanese"),
    ("sv", "swedish"),
    ("sw", "swahili"),
    ("ta", "tamil"),
    ("te", "telugu"),
    ("tg", "tajik"),
    ("th", "thai"),
    ("tk", "turkmen"),
    ("tl", "tagalog"),
    ("tr", "turkish"),
    ("tt", "tatar"),
    ("uk", "ukrainian"),
    ("ur", "urdu"),
    ("uz", "uzbek"),
    ("vi", "vietnamese"),
    ("yi", "yiddish"),
    ("yo", "yoruba"),
    ("yue", "cantonese"),
    ("zh", "chinese"),
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
/// `asr::detected_lang` without this table.
///
/// This is the curated EXTRAS list, not the whole reported vocabulary: since
/// round 6 every code's own name in [`WIRE_ACCEPTED_LANGS`] is readable too,
/// from that one definition (`tibetan`, `javanese`, `nynorsk`, … — the 22 names
/// that used to abort a pinned episode). The entries here stay because Whisper's
/// table does not spell them this way: family spellings a different server may
/// answer with (`farsi`, `mandarin`, `castilian`, `flemish`, `burmese`). A
/// responder whose spelling is in neither list collapses to a long token and
/// `detected_lang` rejects it, naming the value; extend a list when such a
/// spelling shows up in a log, and never claim broader coverage than the two
/// lists.
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

/// Canonical code for a spelling every language table knows: the pin aliases,
/// the curated reported names, and every Whisper name in
/// [`WIRE_ACCEPTED_LANGS`] — the same single definition that decides the wire
/// accept set, so a code can never be pinnable while its own name is
/// unreadable (the round-6 class).
fn known_code(token: &str) -> Option<&'static str> {
    alias_code(token)
        .or_else(|| {
            REPORTED_LANG_SPELLINGS
                .iter()
                .find(|(alias, _)| *alias == token)
                .map(|(_, code)| *code)
        })
        .or_else(|| wire_name_code(token))
}

/// Canonical code for one of Whisper's own names: the `(code, name)` half of
/// [`WIRE_ACCEPTED_LANGS`]. `token` is already collapsed by the caller, so the
/// comparison collapses the table's name too — the table spells names the way
/// Whisper does (`haitian creole` → `haitiancreole`). A name that is in no
/// table answers `None`, so an unknown spelling still fails closed and names
/// the value.
fn wire_name_code(token: &str) -> Option<&'static str> {
    WIRE_ACCEPTED_LANGS
        .iter()
        .find(|(_, name)| collapse(name) == token)
        .map(|(code, _)| *code)
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
/// (`"French"`, `"JA"`), a name either reported table knows — a curated
/// [`REPORTED_LANG_SPELLINGS`] extra (`"tamil"` → `"ta"`, `"farsi"` → `"fa"`)
/// or a code's own name out of [`WIRE_ACCEPTED_LANGS`] (`"tibetan"` → `"bo"`,
/// `"haitian creole"` → `"ht"`, `"javanese"` → `"jw"`) — and a `-XX`/`_XX`
/// region or script subtag on a head that names a language (`"en-US"` →
/// `"en"`, `"zh-Hant"` → `"zh"`, `"pt-BR"` → `"pt"`). Anything else is
/// returned as the plain alphanumeric collapse, for the caller to accept (a
/// 2–3 letter token is metadata) or reject.
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
    let code = code.trim();
    WIRE_ACCEPTED_LANGS.iter().any(|(c, _)| *c == code)
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
        // Accepted by the endpoint (see `WIRE_ACCEPTED_LANGS`), so a detected
        // `jw`/`ln` source names itself instead of reading `Unknown`.
        "jw" => "Javanese",
        "ln" => "Lingala",
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
/// Aliases carrying the same content for one canonical language: every alias
/// table key that normalizes to the canonical code, plus the canonical itself,
/// canonical first. Unknown languages yield an empty slice (callers fall back
/// to the normalized code itself).
///
/// Derived from [`LANG_ALIASES`] instead of a literal list, so renaming a
/// canonical can no longer orphan what an earlier revision of this pipeline
/// wrote to disk. With the old three hardcoded entries (`ja`/`id`/`en`) the
/// `fil` → `tl` remap made `replaceable_target_sidecar_paths(stem, "fil")`
/// stop seeing `{stem}.fil.hi.srt`: HEAD re-transcribed and wrote a duplicate
/// `{stem}.tl.hi.srt` beside it — no adoption, no clobber, and the stale file
/// stayed servable. The `pt`/`zh` spellings (`por`, `chi`, `pt-BR`,
/// `zh-Hant`) had the same hole and are covered by the same derivation.
pub fn sidecar_aliases(lang: &str) -> &'static [&'static str] {
    static TABLE: OnceLock<Vec<(&'static str, Vec<&'static str>)>> = OnceLock::new();
    let table = TABLE.get_or_init(|| {
        let mut canonicals: Vec<&'static str> = Vec::new();
        for (_, code) in LANG_ALIASES {
            let canonical = canonical_code(code);
            if !canonicals.contains(&canonical) {
                canonicals.push(canonical);
            }
        }
        canonicals
            .into_iter()
            .map(|canonical| {
                let mut aliases = vec![canonical];
                for (alias, _) in LANG_ALIASES {
                    if canonical_code(alias) == canonical && !aliases.contains(alias) {
                        aliases.push(alias);
                    }
                }
                (canonical, aliases)
            })
            .collect()
    });
    let want = normalize_lang(lang);
    table
        .iter()
        .find(|(canonical, _)| *canonical == want)
        .map(|(_, aliases)| aliases.as_slice())
        .unwrap_or(&[])
}

/// The code the tables call canonical for a token, following a chained alias
/// to a fixpoint (`xx` → `jv` → `jw`). Bounded by the table length, so a
/// cyclic table entry answers with the cycle's first code instead of hanging.
fn canonical_code(token: &'static str) -> &'static str {
    let mut code = token;
    for _ in 0..LANG_ALIASES.len() {
        match alias_code(code) {
            Some(next) if next != code => code = next,
            _ => return code,
        }
    }
    code
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
        // The Javanese spelling the endpoint rejects (`jv`, HTTP 400)
        // remaps to the one it accepts (`jw`, HTTP 200) — the same class of
        // remap as `fil` -> `tl`, so the pin, the reported value, the display
        // name and the sidecar suffix stay on one code.
        assert_eq!(normalize_lang("jv"), "jw");
        assert_eq!(normalize_lang("jw"), "jw");
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

    /// The codes of the accept set, in table order.
    fn wire_codes() -> Vec<&'static str> {
        WIRE_ACCEPTED_LANGS.iter().map(|(c, _)| *c).collect()
    }

    #[test]
    fn wire_accepts_implies_identity_accepts() {
        // Fix 1's relationship, half two: every code the endpoint accepts is
        // a code that names a language. Asserted, not assumed — a marker or a
        // name-shaped token in the wire table would let a request assert
        // something that is not a language. Also pins the shape of the
        // measured table itself — 2–3 lower-case letters, no duplicates,
        // sorted, and one distinct name per code (round 6 made the name the
        // other half of the same entry) — so a careless edit is visible.
        for (code, name) in WIRE_ACCEPTED_LANGS {
            assert!(wire_accepts(code), "{code}");
            assert!(identity_accepts(code), "{code} must name a language");
            assert!(
                (2..=3).contains(&code.len()) && code.chars().all(|c| c.is_ascii_lowercase()),
                "{code} is not a 2-3 letter code"
            );
            assert!(!is_uncertainty_marker(code), "{code} is a marker");
            assert!(!name.is_empty(), "{code} has no Whisper name");
            assert!(
                name.chars().all(|c| c.is_ascii_lowercase() || c == ' '),
                "{code}: {name} is not a lower-case name"
            );
        }
        let unique: std::collections::BTreeSet<&str> =
            WIRE_ACCEPTED_LANGS.iter().map(|(c, _)| *c).collect();
        assert_eq!(unique.len(), WIRE_ACCEPTED_LANGS.len(), "duplicate code");
        let names: std::collections::BTreeSet<String> = WIRE_ACCEPTED_LANGS
            .iter()
            .map(|(_, n)| collapse(n))
            .collect();
        assert_eq!(
            names.len(),
            WIRE_ACCEPTED_LANGS.len(),
            "two codes share one name"
        );
        let mut sorted = wire_codes();
        sorted.sort_unstable();
        assert_eq!(sorted, wire_codes(), "keep the table sorted");
    }

    #[test]
    fn wire_accept_set_contents_are_pinned() {
        // The set is a live measurement of one endpoint, so the drift that
        // matters is silent: a code dropped (the round-4 table was missing
        // `jw`/`ln`, and a `jw`-tagged track lost its pin because of it) or a
        // code added that the endpoint answers with HTTP 400. The measured
        // list is therefore restated here in full — the deliberate duplicate
        // is the point — and `tools/probe_wire_langs.py` re-derives it from
        // the live endpoint (candidate universe: openai/whisper's own
        // 100-code language table, commit 86098128c0b4).
        let measured: Vec<&str> = vec![
            "af", "am", "ar", "as", "az", "ba", "be", "bg", "bn", "bo", "br", "bs", "ca", "cs",
            "cy", "da", "de", "el", "en", "es", "et", "eu", "fa", "fi", "fo", "fr", "gl", "gu",
            "ha", "haw", "he", "hi", "hr", "ht", "hu", "hy", "id", "is", "it", "ja", "jw", "ka",
            "kk", "km", "kn", "ko", "la", "lb", "ln", "lo", "lt", "lv", "mg", "mi", "mk", "ml",
            "mn", "mr", "ms", "mt", "my", "ne", "nl", "nn", "no", "oc", "pa", "pl", "ps", "pt",
            "ro", "ru", "sa", "sd", "si", "sk", "sl", "sn", "so", "sq", "sr", "su", "sv", "sw",
            "ta", "te", "tg", "th", "tk", "tl", "tr", "tt", "uk", "ur", "uz", "vi", "yi", "yo",
            "yue", "zh",
        ];
        assert_eq!(
            measured.len(),
            100,
            "Whisper's own language table has 100 codes"
        );
        assert_eq!(
            wire_codes(),
            measured,
            "the accept set drifted from the measured 100"
        );
    }

    #[test]
    fn every_accepted_code_is_readable_by_its_own_name() {
        // Round 6: 22 of the 100 codes had a Whisper name that neither table
        // knew (`tibetan`, `javanese`, `nynorsk`, …), so a provider answering
        // a language's OWN name failed a pinned episode permanently with
        // `whisper language mismatch: pinned <code>, reported <name>`. The
        // names are now the second half of this table, so this test is the
        // drift alarm for both halves at once: a name that does not resolve
        // back to its own code fails here, whether it was mis-typed or
        // shadowed by another code's alias/curated spelling.
        for (code, name) in WIRE_ACCEPTED_LANGS {
            assert_eq!(
                normalize_reported_lang(name),
                *code,
                "{code}'s own name {name} must resolve to {code}"
            );
            // The lookup is on the collapsed token, so a multi-word name
            // resolves with its space, without it, and in any case.
            assert_eq!(normalize_reported_lang(&collapse(name)), *code, "{code}");
            assert_eq!(
                normalize_reported_lang(&name.to_uppercase()),
                *code,
                "{code}"
            );
            // A reported code is metadata and must read as itself.
            assert_eq!(normalize_reported_lang(code), *code, "{code}");
        }
    }

    #[test]
    fn the_names_that_used_to_abort_a_pinned_episode_are_readable() {
        // The measured defect list (independent review of b0b91e68: the real
        // binary, one run per code, a stub answering each code's own Whisper
        // name — 22 non-zero exits, raw evidence r5rev/head_names.json). Each
        // pair here answered `whisper language mismatch: pinned <code>,
        // reported <name>`; the names are now read out of the accept table.
        for (name, code) in [
            ("afrikaans", "af"),
            ("assamese", "as"),
            ("bashkir", "ba"),
            ("belarusian", "be"),
            ("tibetan", "bo"),
            ("breton", "br"),
            ("basque", "eu"),
            ("faroese", "fo"),
            ("hawaiian", "haw"),
            ("haitian creole", "ht"),
            ("javanese", "jw"),
            ("latin", "la"),
            ("luxembourgish", "lb"),
            ("lingala", "ln"),
            ("maltese", "mt"),
            ("myanmar", "my"),
            ("nynorsk", "nn"),
            ("occitan", "oc"),
            ("sanskrit", "sa"),
            ("shona", "sn"),
            ("albanian", "sq"),
            ("yiddish", "yi"),
        ] {
            assert_eq!(normalize_reported_lang(name), code, "{name}");
        }
    }

    #[test]
    fn display_names_and_reported_names_agree() {
        // `display_name` names ~36 of the 100 codes, which is why it cannot be
        // the reported-name source — but where it DOES name a code, that name
        // must read back as the same code. The round-5 gap this pins: `jw` and
        // `ln` gained display names while the reported path did not know
        // `javanese`/`lingala`, so a detected Javanese source was unusable.
        for (code, _) in WIRE_ACCEPTED_LANGS {
            let name = display_name(code);
            if name != "Unknown" {
                assert_eq!(
                    normalize_reported_lang(name),
                    normalize_lang(code),
                    "{code} is named {name}, which must read back as {code}"
                );
            }
        }
        assert_eq!(normalize_reported_lang("Javanese"), "jw");
        assert_eq!(normalize_reported_lang("Lingala"), "ln");
    }

    #[test]
    fn the_names_table_does_not_turn_a_marker_into_a_language() {
        // The 100 names ride the same lookup the pin aliases use, so this pins
        // the boundary the fold must not move: a marker names no language, is
        // never a subtag head, and reads back as itself for the caller to
        // reject (a responder saying `N/A` must not become a pin).
        for marker in [
            "und",
            "unknown",
            "none",
            "na",
            "zz",
            "mul",
            "zxx",
            "mis",
            "auto",
            "undefined",
        ] {
            assert!(!names_a_known_language(marker), "{marker} names nothing");
            assert_eq!(normalize_reported_lang(marker), marker, "{marker}");
            assert!(
                subtag_head(&format!("{marker}-US")).is_none(),
                "{marker}-US"
            );
        }
        assert_eq!(normalize_reported_lang("und-US"), "undus");
        assert_eq!(normalize_lang("und-US"), "undus");
        // A name-shaped token that is in no table is still rejected with the
        // value named, rather than being accepted because names exist now.
        assert_eq!(normalize_reported_lang("klingon"), "klingon");
        assert_eq!(normalize_reported_lang("klingon-KLI"), "klingonkli");
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
        // values below were probed one request per code on 2026-09-13 (round 5
        // re-probed Whisper's whole 100-code table with
        // `tools/probe_wire_langs.py`; `jw` and `ln` were the two the old
        // 98-code set was missing and are accepted).
        for ok in [
            "en", "ja", "id", "fr", " zh ", "cy", "yue", "haw", "ta", "ml", "tl", "sr", "hr", "ne",
            "si", "km", "lo", "my", "bn", "ur", "sw", "jw", "ln",
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
        assert_eq!(sidecar_paths("/m/ep", "tgl"), sidecar_paths("/m/ep", "fil"));
        // The canonical paths come first, and the legacy `fil` spellings an
        // earlier revision wrote stay in the list (round 5 derivation).
        let tl = sidecar_paths("/m/ep", "tgl");
        assert_eq!(
            &tl[..5],
            &[
                "/m/ep.tl.srt",
                "/m/ep.tl.hi.srt",
                "/m/ep.tl.forced.srt",
                "/m/ep.tl.hi.forced.srt",
                "/m/ep.tl.forced.hi.srt",
            ]
        );
        assert!(tl.contains(&"/m/ep.fil.hi.srt".to_string()), "{tl:?}");
    }

    #[test]
    fn sidecar_aliases_cover_every_spelling_of_a_canonical() {
        // The list is derived from the alias table, so renaming a canonical
        // cannot orphan an older spelling on disk (`fil` -> `tl` did exactly
        // that while the list was three hardcoded entries).
        for (alias, canonical) in LANG_ALIASES {
            let list = sidecar_aliases(alias);
            assert!(
                list.contains(canonical),
                "{alias}: no {canonical} in {list:?}"
            );
            assert!(
                list.contains(alias),
                "{alias}: itself missing from {list:?}"
            );
            assert_eq!(list[0], *canonical, "{alias}: canonical must come first");
            // Every spelling in the list really resolves to the canonical.
            for a in list {
                assert_eq!(canonical_code(a), *canonical, "{alias}: {a}");
            }
        }
        // A chained alias (a key whose own value is a key) lands on the
        // canonical too, not on its intermediate.
        assert_eq!(canonical_code("jv"), "jw");
        assert!(sidecar_aliases("jw").contains(&"jv"));
        // ja/id/en are STRICT SUPERSETS of what they listed before, canonical
        // first (ja 6 -> 8, id 4 -> 6, en 6 -> 8 replaceable paths — the
        // derivation adds `.japanese[.hi].srt`, `.indonesian[.hi].srt` and
        // `.english[.hi].srt`), and the order inside the list is the alias
        // table's (`ja`, `jp`, `jpn`, `japanese`; round 4's literal was
        // `ja`, `jpn`, `jp`). No caller is order-sensitive: each iterates the
        // list or uses `.any()`.
        for (code, legacy) in [
            ("ja", vec!["ja", "jpn", "jp"]),
            ("id", vec!["id", "ind"]),
            ("en", vec!["en", "eng", "enm"]),
        ] {
            let list = sidecar_aliases(code);
            assert_eq!(list[0], code);
            for a in legacy {
                assert!(list.contains(&a), "{code}: {a} lost from {list:?}");
            }
        }
        // A language no alias names keeps the empty list (callers fall back to
        // the normalized code itself).
        assert!(sidecar_aliases("cy").is_empty());
        assert!(sidecar_aliases("yue").is_empty());
    }

    #[test]
    fn legacy_target_spellings_stay_discoverable() {
        // What an earlier revision of this pipeline wrote for a Filipino
        // target was `{stem}.fil.hi.srt` (and `.fil.srt`). After the
        // `fil` -> `tl` remap the canonical file is `.tl.hi.srt`, so those
        // legacy names must still be discovered — otherwise HEAD writes a
        // duplicate beside the stale one and never adopts it.
        for tag in ["fil", "tgl", "tagalog", "filipino"] {
            let paths = replaceable_target_sidecar_paths("/m/ep", tag);
            assert_eq!(paths[0], "/m/ep.tl.hi.srt", "{tag}");
            for legacy in ["/m/ep.fil.hi.srt", "/m/ep.fil.srt"] {
                assert!(paths.contains(&legacy.to_string()), "{tag}: {paths:?}");
            }
        }
        // Region/script spellings inherit the whole list of their canonical:
        // `pt-BR` and `zh-Hant` normalize to `pt`/`zh`, so the ISO-639-2
        // spellings (`por`, `chi`) are discoverable through them too.
        for (tag, canonical) in [
            ("pt-BR", "pt"),
            ("pt_BR", "pt"),
            ("zh-Hant", "zh"),
            ("fil-PH", "tl"),
        ] {
            assert_eq!(
                replaceable_target_sidecar_paths("/m/ep", tag),
                replaceable_target_sidecar_paths("/m/ep", canonical),
                "{tag}"
            );
        }
        assert!(replaceable_target_sidecar_paths("/m/ep", "pt")
            .contains(&"/m/ep.por.hi.srt".to_string()));
        assert!(replaceable_target_sidecar_paths("/m/ep", "zh")
            .contains(&"/m/ep.chi.hi.srt".to_string()));
        // ja/id/en behaviour is unchanged in the only sense that matters: every
        // spelling they listed before is still listed (strict supersets), and
        // the canonical HI path still comes first. The list grew by the full
        // name of each code and is in alias-table order, neither of which any
        // caller depends on.
        for (lang, legacy) in [
            ("ja", vec!["ja", "jpn", "jp"]),
            ("id", vec!["id", "ind"]),
            ("en", vec!["en", "eng", "enm"]),
        ] {
            let paths = replaceable_target_sidecar_paths("/m/ep", lang);
            assert_eq!(paths[0], format!("/m/ep.{lang}.hi.srt"));
            for a in legacy {
                for suffix in [".srt", ".hi.srt"] {
                    assert!(
                        paths.contains(&format!("/m/ep.{a}{suffix}")),
                        "{lang}: {a}{suffix} lost from {paths:?}"
                    );
                }
            }
        }
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
        // A `jv` response is the Javanese spelling the endpoint rejects; the
        // alias table remaps it to the accepted `jw` (metadata, never sent).
        assert_eq!(normalize_reported_lang("jv"), "jw");
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
        // The codes the accept set gained (`jw`, `ln`) name themselves too, so
        // a detected Javanese/Lingala source is not reported as `Unknown`;
        // `jv` reaches `jw` through the alias table.
        assert_eq!(display_name("jw"), "Javanese");
        assert_eq!(display_name("jv"), "Javanese");
        assert_eq!(display_name("ln"), "Lingala");
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
