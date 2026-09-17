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
    // ladder passes English through for `en` sources, the ASR passes the
    // track's real tag for everything else.
    let p = system_prompt("Indonesian", display_source_lang("en"), "");
    assert!(p.contains("English"), "{p}");
    assert!(p.contains("Indonesian"), "{p}");
    assert_eq!(display_source_lang("ja"), "Japanese");
    assert_eq!(display_source_lang("jpn"), "Japanese");
    assert_eq!(display_source_lang("en"), "English");
    // A French source must not be called Japanese (the defect).
    assert_eq!(display_source_lang("fr"), "French");
    assert_eq!(display_source_lang("fre"), "French");
}

#[test]
fn french_source_lines_must_skip_the_foreign_guard() {
    // The foreign-script guard turns "mostly-latin" lines into SDH
    // placeholders (it exists for Japanese ASR echoing OP/ED lyrics in
    // English/Chinese). A faithful French transcript is mostly latin, so
    // running it there would empty the episode — the guard must only run
    // for CJK sources, and `fr` is not one.
    let fr = vec![
        "Bonjour, comment allez-vous ?".to_string(),
        "Le president est arrive.".to_string(),
    ];
    let wiped = guard_foreign_lines(fr.clone(), &placeholders());
    assert_eq!(wiped, vec!["（歌詞）".to_string(), "（歌詞）".to_string()]);
    assert!(!crate::lang::needs_foreign_guard("fr"));
    assert!(crate::lang::needs_foreign_guard("ja"));
}

#[tokio::test]
async fn persistent_failure_returns_error_instead_of_blank_success() {
    // A missing/exhausted provider must fail the target. Returning a
    // same-length Vec of empty strings would admit a blank subtitle as a
    // successful translation.
    let pool = ProviderPool::new(
        crate::providers::ProvidersFile {
            llm_translation_models: vec![],
            whisper_stt: None,
            whisper_stt_fallbacks: vec![],
        },
        reqwest::Client::new(),
    );
    let error = translate_lines(
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
    .expect_err("exhausted providers must fail translation");
    assert!(
        error.to_string().contains("translation failed"),
        "{error:#}"
    );
}

#[tokio::test]
async fn stalled_response_is_cancelled_and_fails_translation() {
    use tokio::io::AsyncReadExt;

    let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
        .await
        .unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        let mut request = [0u8; 4096];
        let _ = stream.read(&mut request).await;
        tokio::time::sleep(std::time::Duration::from_secs(60)).await;
    });

    let pool = ProviderPool::new_with_timeouts(
        crate::providers::ProvidersFile {
            llm_translation_models: vec![crate::providers::LlmProvider {
                endpoint: format!("http://{address}/chat/completions"),
                model: "stalled-model".to_string(),
                key_env: String::new(),
                api_key: "test-key".to_string(),
                probe_latency_s: 1.0,
                thinking_param_accepted: false,
            }],
            whisper_stt: None,
            whisper_stt_fallbacks: vec![],
        },
        reqwest::Client::new(),
        crate::providers::LlmTimeouts {
            connect: std::time::Duration::from_millis(20),
            read: std::time::Duration::from_millis(20),
            request: std::time::Duration::from_millis(50),
            translation: std::time::Duration::from_millis(200),
        },
    );
    let started = tokio::time::Instant::now();
    let result = tokio::time::timeout(
        std::time::Duration::from_millis(100),
        translate_lines(
            &pool,
            TranslateJob {
                lines: vec!["first line".to_string()],
                target_lang: "id",
                source_lang: "English",
                knowledge: "",
                chunk_size: 1,
                fanout: 1,
                skip_guard: true,
                placeholders: &[],
            },
        ),
    )
    .await
    .expect("stalled response must be cancelled by translation deadline")
    .expect_err("stalled response must fail translation");
    assert!(started.elapsed() < std::time::Duration::from_millis(100));
    assert!(
        result.to_string().contains("connect/headers timeout"),
        "unexpected timeout error: {result:#}"
    );
    server.abort();
}

#[tokio::test]
async fn episode_language_deadline_cancels_inflight_provider_work() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
        .await
        .unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        let mut request = [0u8; 4096];
        let _ = stream.read(&mut request).await;
        let _ = stream
                .write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 1024\r\n\r\n{\"choices\":[",
                )
                .await;
        tokio::time::sleep(std::time::Duration::from_secs(60)).await;
    });

    let pool = ProviderPool::new_with_timeouts(
        crate::providers::ProvidersFile {
            llm_translation_models: vec![crate::providers::LlmProvider {
                endpoint: format!("http://{address}/chat/completions"),
                model: "deadline-model".to_string(),
                key_env: String::new(),
                api_key: "test-key".to_string(),
                probe_latency_s: 1.0,
                thinking_param_accepted: false,
            }],
            whisper_stt: None,
            whisper_stt_fallbacks: vec![],
        },
        reqwest::Client::new(),
        crate::providers::LlmTimeouts {
            connect: std::time::Duration::from_secs(1),
            read: std::time::Duration::from_secs(1),
            request: std::time::Duration::from_secs(1),
            translation: std::time::Duration::from_millis(30),
        },
    );
    let started = tokio::time::Instant::now();
    let error = tokio::time::timeout(
        std::time::Duration::from_millis(100),
        translate_lines(
            &pool,
            TranslateJob {
                lines: vec!["first line".to_string()],
                target_lang: "id",
                source_lang: "English",
                knowledge: "",
                chunk_size: 1,
                fanout: 1,
                skip_guard: true,
                placeholders: &[],
            },
        ),
    )
    .await
    .expect("episode-language deadline must cancel the stalled request")
    .expect_err("episode-language deadline must return a translation error");
    assert!(started.elapsed() < std::time::Duration::from_millis(100));
    assert!(
        error
            .to_string()
            .contains("episode-language deadline exceeded"),
        "unexpected deadline error: {error:#}"
    );
    server.abort();
}

#[tokio::test]
async fn timed_out_provider_fails_over_to_next_model() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
        .await
        .unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        loop {
            let Ok((mut stream, _)) = listener.accept().await else {
                break;
            };
            tokio::spawn(async move {
                let mut request = [0u8; 4096];
                let Ok(size) = stream.read(&mut request).await else {
                    return;
                };
                let request = String::from_utf8_lossy(&request[..size]);
                if request.starts_with("POST /first ") {
                    let _ = stream
                            .write_all(
                                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 1024\r\n\r\n{\"choices\":[",
                            )
                            .await;
                    tokio::time::sleep(std::time::Duration::from_secs(60)).await;
                    return;
                }
                let body = br#"{"choices":[{"message":{"content":"[\"Halo dunia\"]"}}]}"#;
                let header = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n",
                        body.len()
                    );
                let _ = stream.write_all(header.as_bytes()).await;
                let _ = stream.write_all(body).await;
            });
        }
    });

    let pool = ProviderPool::new_with_timeouts(
        crate::providers::ProvidersFile {
            llm_translation_models: vec![
                crate::providers::LlmProvider {
                    endpoint: format!("http://{address}/first"),
                    model: "timed-out-model".to_string(),
                    key_env: String::new(),
                    api_key: "test-key-1".to_string(),
                    probe_latency_s: 1.0,
                    thinking_param_accepted: false,
                },
                crate::providers::LlmProvider {
                    endpoint: format!("http://{address}/second"),
                    model: "backup-model".to_string(),
                    key_env: String::new(),
                    api_key: "test-key-2".to_string(),
                    probe_latency_s: 2.0,
                    thinking_param_accepted: false,
                },
            ],
            whisper_stt: None,
            whisper_stt_fallbacks: vec![],
        },
        reqwest::Client::new(),
        crate::providers::LlmTimeouts {
            connect: std::time::Duration::from_millis(20),
            read: std::time::Duration::from_millis(20),
            request: std::time::Duration::from_millis(50),
            translation: std::time::Duration::from_millis(300),
        },
    );
    let result = translate_lines(
        &pool,
        TranslateJob {
            lines: vec!["first line".to_string()],
            target_lang: "id",
            source_lang: "English",
            knowledge: "",
            chunk_size: 1,
            fanout: 1,
            skip_guard: true,
            placeholders: &[],
        },
    )
    .await
    .unwrap();
    assert_eq!(result, vec!["Halo dunia"]);
    server.abort();
}

#[tokio::test]
async fn review_lines_returns_without_waiting_on_a_stalled_body() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
        .await
        .unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        let mut request = [0u8; 4096];
        let _ = stream.read(&mut request).await;
        let _ = stream
                .write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 1024\r\n\r\n{\"choices\":[",
                )
                .await;
        tokio::time::sleep(std::time::Duration::from_secs(60)).await;
    });
    let pool = ProviderPool::new_with_timeouts(
        crate::providers::ProvidersFile {
            llm_translation_models: vec![crate::providers::LlmProvider {
                endpoint: format!("http://{address}/chat/completions"),
                model: "review-model".to_string(),
                key_env: String::new(),
                api_key: "test-key".to_string(),
                probe_latency_s: 1.0,
                thinking_param_accepted: false,
            }],
            whisper_stt: None,
            whisper_stt_fallbacks: vec![],
        },
        reqwest::Client::new(),
        crate::providers::LlmTimeouts {
            connect: std::time::Duration::from_millis(20),
            read: std::time::Duration::from_millis(20),
            request: std::time::Duration::from_millis(50),
            translation: std::time::Duration::from_millis(200),
        },
    );
    let started = tokio::time::Instant::now();
    let result = review_lines(
        &pool,
        &["こんにちは".to_string()],
        &["hello".to_string()],
        1,
    )
    .await;
    assert!(started.elapsed() < std::time::Duration::from_millis(100));
    assert_eq!(result, vec!["hello"]);
    server.abort();
}

#[tokio::test]
async fn exhausted_providers_return_a_safe_translation_error() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
        .await
        .unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        loop {
            let Ok((mut stream, _)) = listener.accept().await else {
                break;
            };
            tokio::spawn(async move {
                let mut request = [0u8; 4096];
                let _ = stream.read(&mut request).await;
                let _ = stream
                    .write_all(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                    .await;
            });
        }
    });

    let pool = ProviderPool::new_with_timeouts(
        crate::providers::ProvidersFile {
            llm_translation_models: vec![
                crate::providers::LlmProvider {
                    endpoint: format!("http://{address}/chat"),
                    model: "gone-a".to_string(),
                    key_env: String::new(),
                    api_key: "test-key-a".to_string(),
                    probe_latency_s: 1.0,
                    thinking_param_accepted: false,
                },
                crate::providers::LlmProvider {
                    endpoint: format!("http://{address}/chat"),
                    model: "gone-b".to_string(),
                    key_env: String::new(),
                    api_key: "test-key-b".to_string(),
                    probe_latency_s: 2.0,
                    thinking_param_accepted: false,
                },
            ],
            whisper_stt: None,
            whisper_stt_fallbacks: vec![],
        },
        reqwest::Client::new(),
        crate::providers::LlmTimeouts {
            connect: std::time::Duration::from_millis(20),
            read: std::time::Duration::from_millis(20),
            request: std::time::Duration::from_millis(50),
            translation: std::time::Duration::from_millis(200),
        },
    );
    let error = translate_lines(
        &pool,
        TranslateJob {
            lines: vec!["first line".to_string()],
            target_lang: "id",
            source_lang: "English",
            knowledge: "",
            chunk_size: 1,
            fanout: 1,
            skip_guard: true,
            placeholders: &[],
        },
    )
    .await
    .expect_err("all providers returning 404 must fail translation");
    let text = error.to_string();
    assert!(text.contains("translation failed"), "{text}");
    assert!(text.contains("gone-a"), "{text}");
    assert!(text.contains("gone-b"), "{text}");
    assert!(text.contains("HTTP 404"), "{text}");
    assert!(!text.contains("test-key"), "credentials leaked: {text}");
    server.abort();
}
