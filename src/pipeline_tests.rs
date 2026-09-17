use super::*;

#[test]
fn movie_original_language_degrades_gracefully() {
    assert_eq!(
        movie_original_lang(&serde_json::json!({"originalLanguage": {"id": 2, "name": "French"}}))
            .as_deref(),
        Some("French")
    );
    assert_eq!(
        movie_original_lang(&serde_json::json!({"originalLanguage": "German"})).as_deref(),
        Some("German")
    );
    // Missing / blank / wrong shape: None, never an error.
    assert_eq!(movie_original_lang(&serde_json::json!({})), None);
    assert_eq!(
        movie_original_lang(&serde_json::json!({"originalLanguage": {"name": "  "}})),
        None
    );
}

#[test]
fn verified_targets_requires_paired_matching_digests_and_file_bytes() {
    use sha2::Digest;

    let dir = tempfile::tempdir().unwrap();
    let live = dir.path().join("ep.id.hi.srt");
    let bytes = b"live artifact";
    std::fs::write(&live, bytes).unwrap();
    let mut hasher = sha2::Sha256::new();
    hasher.update(bytes);
    let digest: [u8; 32] = hasher.finalize().into();
    let digest_hex = digest
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    let row = |kind: Option<&str>, id: i64, lang: &str, target: &str, hash: Option<&str>| {
        let mut extra = std::collections::HashMap::new();
        if let Some(kind) = kind {
            extra.insert("kind".to_string(), serde_json::json!(kind));
        }
        if let Some(hash) = hash {
            extra.insert("artifact_sha256".to_string(), serde_json::json!(hash));
        }
        state::RegistryRow {
            stem: None,
            lang: Some(lang.to_string()),
            episode_id: Some(id),
            source: None,
            source_kind: None,
            source_path: None,
            target_path: Some(target.to_string()),
            ts: None,
            extra,
        }
    };
    let state_row = |kind: Option<&str>, id: i64, lang: &str, hash: Option<&str>| {
        let mut extra = std::collections::HashMap::new();
        if let Some(hash) = hash {
            extra.insert("artifact_sha256".to_string(), serde_json::json!(hash));
        }
        state::StateEntry {
            episode_id: Some(id),
            language: Some(lang.to_string()),
            status: Some("done".to_string()),
            kind: kind.map(str::to_string),
            ts: None,
            extra,
        }
    };
    let key = ("series".to_string(), 7, "id".to_string());
    let verified = verified_targets(
        &[row(
            None,
            7,
            "ind",
            live.to_str().unwrap(),
            Some(&digest_hex),
        )],
        &[state_row(None, 7, "id", Some(&digest_hex))],
    );
    assert_eq!(
        verified.get(&key).map(|target| target.artifact_sha256),
        Some(digest)
    );

    // A missing registry digest, a state mismatch, or bytes changed after
    // publication must all remain unverified.
    assert!(verified_targets(
        &[row(None, 7, "id", live.to_str().unwrap(), None)],
        &[state_row(None, 7, "id", Some(&digest_hex))],
    )
    .is_empty());
    assert!(verified_targets(
        &[row(
            None,
            7,
            "id",
            live.to_str().unwrap(),
            Some(&digest_hex)
        )],
        &[state_row(None, 7, "id", None)],
    )
    .is_empty());
    assert!(verified_targets(
        &[row(
            None,
            7,
            "id",
            live.to_str().unwrap(),
            Some(&digest_hex)
        )],
        &[state_row(None, 7, "id", Some(&"00".repeat(32)))],
    )
    .is_empty());
    std::fs::write(&live, b"foreign replacement").unwrap();
    assert!(verified_targets(
        &[row(
            None,
            7,
            "id",
            live.to_str().unwrap(),
            Some(&digest_hex)
        )],
        &[state_row(None, 7, "id", Some(&digest_hex))],
    )
    .is_empty());
}

#[test]
fn reduces_one_success_and_one_failure_to_partial_report() {
    use crate::feature_modules::discord_text::SafeDisplayText;
    use crate::feature_modules::discord_types::*;

    let report = reduce_target_outcomes(
        EpisodeKind::Series,
        7,
        SafeDisplayText::sanitize("title").unwrap(),
        Some(1),
        Some(2),
        vec![
            TargetRunResult::try_new(
                TargetLanguage::parse("id").unwrap(),
                TargetStatus::Completed { warning: None },
                Some([1; 32]),
            )
            .unwrap(),
            TargetRunResult::try_new(
                TargetLanguage::parse("en").unwrap(),
                TargetStatus::Failed {
                    class: FailureClass::Translation,
                },
                None,
            )
            .unwrap(),
        ],
        None,
    )
    .unwrap();

    assert_eq!(report.aggregate(), AggregateDisposition::Partial);
    assert_eq!(report.targets().as_slice().len(), 2);
    assert!(matches!(
        report.targets().as_slice()[0].status(),
        TargetStatus::Failed {
            class: FailureClass::Translation
        }
    ));
}

fn report(id: i64) -> crate::feature_modules::discord_types::EpisodeRunReport {
    use crate::feature_modules::discord_text::SafeDisplayText;
    use crate::feature_modules::discord_types::*;
    EpisodeRunReport::try_new(
        EpisodeKind::Series,
        id,
        SafeDisplayText::sanitize("title").unwrap(),
        Some(1),
        Some(1),
        BoundedTargets::try_from([TargetRunResult::try_new(
            TargetLanguage::parse("id").unwrap(),
            TargetStatus::Completed { warning: None },
            Some([id as u8; 32]),
        )
        .unwrap()])
        .unwrap(),
        None,
        AggregateDisposition::Complete,
    )
    .unwrap()
}

#[test]
fn retains_bounded_report_omission_count() {
    let reports = (0..=crate::feature_modules::discord_types::MAX_PASS_REPORTS as i64)
        .map(report)
        .collect::<Vec<_>>();
    let (bounded, omitted) = pipeline_reports::bound_pass_reports(reports).unwrap();
    let outcome = PassOutcome::new(PassStats::default(), bounded, omitted);

    assert_eq!(outcome.reports().len(), 128);
    assert_eq!(outcome.omitted_reports(), 1);
}

#[test]
fn run_pass_wrapper_discards_reports() {
    let (reports, omitted) =
        crate::feature_modules::discord_types::BoundedReports::from_reports(std::iter::empty::<
            crate::feature_modules::discord_types::EpisodeRunReport,
        >())
        .unwrap();
    let outcome = PassOutcome::new(PassStats::default(), reports, omitted);
    assert_eq!(outcome.reports().len(), 0);
    assert_eq!(outcome.omitted_reports(), 0);
}

#[test]
fn reports_sort_deterministically() {
    let (reports, _) =
        crate::feature_modules::discord_types::BoundedReports::from_reports([report(9), report(2)])
            .unwrap();
    assert_eq!(
        reports.iter().map(|r| r.episode_id()).collect::<Vec<_>>(),
        vec![2, 9]
    );
}

#[test]
fn commit_id_is_stable() {
    let a = report(7);
    let b = report(7);
    assert_eq!(
        a.pipeline_commit_id().unwrap().as_str(),
        b.pipeline_commit_id().unwrap().as_str()
    );
}
