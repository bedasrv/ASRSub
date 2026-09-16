#![allow(dead_code)]
use unicode_normalization::UnicodeNormalization;

use super::discord_unicode;

pub(crate) const MAX_SAFE_TITLE_SCALARS: usize = 512;

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub(crate) enum TextError {
    TooLong,
    InvalidScalar,
    Normalization,
}

#[derive(Clone, Eq, PartialEq, Ord, PartialOrd)]
pub(crate) struct SafeDisplayText(String);

impl SafeDisplayText {
    pub(crate) fn sanitize(raw: &str) -> Result<Self, TextError> {
        let normalized: String = raw.nfc().collect();
        if normalized.chars().count() > MAX_SAFE_TITLE_SCALARS {
            return Err(TextError::TooLong);
        }
        let mut plain = String::with_capacity(normalized.len());
        let mut pending_space = false;
        for c in normalized.chars() {
            let n = c as u32;
            if n > 0x10ffff {
                return Err(TextError::InvalidScalar);
            }
            if c.is_whitespace() {
                pending_space = true;
                continue;
            }
            if n <= 0x1f
                || (0x7f..=0x9f).contains(&n)
                || discord_unicode::is_bidi_control(c)
                || discord_unicode::is_line_separator(c)
                || discord_unicode::is_default_ignorable(c)
            {
                continue;
            }
            if pending_space && !plain.is_empty() {
                plain.push(' ');
            }
            pending_space = false;
            if c.is_alphabetic()
                || discord_unicode::is_mark(c)
                || c.is_numeric()
                || c.is_ascii_punctuation()
                    && matches!(
                        c,
                        '!' | '&'
                            | '\''
                            | '('
                            | ')'
                            | '+'
                            | ','
                            | '-'
                            | '.'
                            | ':'
                            | '?'
                            | '['
                            | ']'
                            | '_'
                    )
            {
                plain.push(c);
            } else {
                pending_space = true;
            }
        }
        while plain.ends_with(' ') {
            plain.pop();
        }
        let escaped: String = plain
            .chars()
            .flat_map(|c| match c {
                '\\' | '`' | '*' | '_' | '~' | '|' | '>' | '[' | ']' | '(' | ')' | '#' => {
                    vec!['\\', c]
                }
                _ => vec![c],
            })
            .collect();
        Ok(Self(escaped))
    }

    pub(crate) fn as_str(&self) -> &str {
        &self.0
    }

    pub(crate) fn scalar_len(&self) -> usize {
        self.0.chars().count()
    }
}

impl std::fmt::Debug for SafeDisplayText {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_tuple("SafeDisplayText").field(&self.0).finish()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sanitizes_controls_unicode_and_markdown() {
        let text = SafeDisplayText::sanitize(" A\u{202e}B # C\n").unwrap();
        assert_eq!(text.as_str(), "AB  C");
    }

    #[test]
    fn rejects_long_titles() {
        assert_eq!(
            SafeDisplayText::sanitize(&"x".repeat(MAX_SAFE_TITLE_SCALARS + 1)),
            Err(TextError::TooLong)
        );
    }
}
