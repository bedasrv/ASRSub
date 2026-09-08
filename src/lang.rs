//! Language normalization + sidecar path helpers.
//!
//! Mirrors the Python `normalize_language` contract: `jpn`/`jp` -> `ja`,
//! `ind` -> `id`, `eng`/`enm` -> `en`. Unknown codes pass through lowercased
//! alphanumeric form so future languages keep working.

/// Normalize a language tag to the canonical application code.
pub fn normalize_lang(value: &str) -> String {
    let norm: String = value
        .trim()
        .to_lowercase()
        .chars()
        .filter(|c| c.is_ascii_alphanumeric())
        .collect();
    match norm.as_str() {
        "ja" | "jp" | "jpn" => "ja".to_string(),
        "id" | "ind" => "id".to_string(),
        "en" | "eng" | "enm" => "en".to_string(),
        other => other.to_string(),
    }
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
    // NB: the `_` fallback above returns a 'static slice but borrows `lang`;
    // handle unknown languages without borrowing issues.
    let norm = normalize_lang(lang);
    let aliases: &[&str] = match norm.as_str() {
        "ja" => &["ja", "jpn", "jp"],
        "id" => &["id", "ind"],
        "en" => &["en", "eng", "enm"],
        _ => &[],
    };
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
    fn canonical_hi_path() {
        assert_eq!(
            canonical_target_sidecar("/m/ep.mkv-stem", "id"),
            "/m/ep.mkv-stem.id.hi.srt"
        );
    }

    #[test]
    fn replaceable_lists_canonical_first() {
        let v = replaceable_target_sidecar_paths("/m/ep", "ja");
        assert_eq!(v[0], "/m/ep.ja.hi.srt");
        assert!(!v.iter().any(|p| p.contains("forced")));
    }
}
