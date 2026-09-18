#![allow(dead_code)]
//! Best-effort, in-memory daemon notification handoff.

use std::sync::Arc;

use tokio::sync::mpsc;

use super::discord_renderer;
use super::discord_state_schema::{DeliveryView, OverflowSummaryV1};
use super::discord_transport::{DeliveryResult, DeliveryTransport};
use super::discord_types::BoundedReports;

pub(crate) const NOTIFIER_QUEUE_CAPACITY: usize = 8;

#[derive(Debug)]
pub(crate) enum NotifierWork {
    Pass {
        reports: BoundedReports,
        omitted_reports: u64,
    },
}

pub(crate) struct CoordinatorHandle {
    sender: mpsc::Sender<NotifierWork>,
}

impl CoordinatorHandle {
    pub(crate) fn try_send(&self, work: NotifierWork) -> Result<(), NotifierWork> {
        self.sender
            .try_send(work)
            .map_err(|error| error.into_inner())
    }
}

pub(crate) fn start_with_transport(
    transport: Arc<dyn DeliveryTransport>,
) -> (CoordinatorHandle, tokio::task::JoinHandle<()>) {
    let (sender, mut receiver) = mpsc::channel(NOTIFIER_QUEUE_CAPACITY);
    let join = tokio::spawn(async move {
        while let Some(NotifierWork::Pass {
            reports,
            omitted_reports,
        }) = receiver.recv().await
        {
            if reports.len() == 0 && omitted_reports == 0 {
                continue;
            }
            let view = DeliveryView::new(
                0,
                reports,
                OverflowSummaryV1::new(0, 0, 0, 0, omitted_reports, 0),
            );
            let payload = match discord_renderer::render(&view) {
                Ok(payload) => payload,
                Err(error) => {
                    tracing::warn!(
                        classification = ?error,
                        "discord notification render failed"
                    );
                    continue;
                }
            };
            if let DeliveryResult::Failed(class) = transport.send(&payload).await {
                tracing::warn!(
                    classification = ?class,
                    "discord notification delivery failed"
                );
            }
        }
    });
    (CoordinatorHandle { sender }, join)
}

#[cfg(test)]
mod tests {
    use std::future::Future;
    use std::pin::Pin;
    use std::sync::{Arc, Mutex};

    use super::super::discord_state_schema::{
        DeliveryView, OverflowSummaryV1, PayloadBytes, SafeDeliveryError, MAX_PAYLOAD_BYTES,
    };
    use super::super::discord_text::SafeDisplayText;
    use super::super::discord_transport::{DeliveryResult, DeliveryTransport};
    use super::super::discord_types::{
        AggregateDisposition, BoundedReports, BoundedTargets, EpisodeKind, EpisodeRunReport,
        FailureClass, TargetLanguage, TargetRunResult, TargetStatus,
    };
    use super::{start_with_transport, NotifierWork};

    struct RecordingTransport {
        outcomes: Mutex<Vec<DeliveryResult>>,
        payloads: Mutex<Vec<Vec<u8>>>,
    }

    impl RecordingTransport {
        fn scripted(outcomes: Vec<DeliveryResult>) -> Self {
            Self {
                outcomes: Mutex::new(outcomes),
                payloads: Mutex::new(Vec::new()),
            }
        }

        fn payload_count(&self) -> usize {
            self.payloads.lock().unwrap().len()
        }

        fn first_payload(&self) -> Vec<u8> {
            self.payloads.lock().unwrap()[0].clone()
        }
    }

    impl DeliveryTransport for RecordingTransport {
        fn send<'a>(
            &'a self,
            payload: &'a PayloadBytes,
        ) -> Pin<Box<dyn Future<Output = DeliveryResult> + Send + 'a>> {
            self.payloads
                .lock()
                .unwrap()
                .push(payload.as_bytes().to_vec());
            let outcome = self
                .outcomes
                .lock()
                .unwrap()
                .pop()
                .unwrap_or(DeliveryResult::Accepted);
            Box::pin(async move { outcome })
        }
    }

    fn report() -> EpisodeRunReport {
        EpisodeRunReport::try_new(
            EpisodeKind::Movie,
            1,
            SafeDisplayText::sanitize("movie").unwrap(),
            None,
            None,
            BoundedTargets::try_from([TargetRunResult::try_new(
                TargetLanguage::parse("id").unwrap(),
                TargetStatus::Completed { warning: None },
                Some([1; 32]),
            )
            .unwrap()])
            .unwrap(),
            None,
            AggregateDisposition::Complete,
        )
        .unwrap()
    }

    fn render_failure_report() -> EpisodeRunReport {
        let targets = (0..32)
            .map(|index| {
                TargetRunResult::try_new(
                    {
                        let language = format!("{}{:02}", "x".repeat(30), index);
                        TargetLanguage::parse(&language).unwrap()
                    },
                    TargetStatus::Failed {
                        class: FailureClass::Unknown,
                    },
                    None,
                )
                .unwrap()
            })
            .collect::<Vec<_>>();
        EpisodeRunReport::try_new(
            EpisodeKind::Movie,
            2,
            SafeDisplayText::sanitize("render failure").unwrap(),
            None,
            None,
            BoundedTargets::try_from(targets).unwrap(),
            None,
            AggregateDisposition::Failed,
        )
        .unwrap()
    }

    fn pass(reports: BoundedReports, omitted_reports: u64) -> NotifierWork {
        NotifierWork::Pass {
            reports,
            omitted_reports,
        }
    }

    async fn finish(handle: super::CoordinatorHandle, join: tokio::task::JoinHandle<()>) {
        drop(handle);
        join.await.unwrap();
    }

    #[tokio::test]
    async fn direct_constructor_sends_one_post_for_meaningful_pass() {
        let transport = Arc::new(RecordingTransport::scripted(vec![DeliveryResult::Accepted]));
        let (handle, join) = start_with_transport(transport.clone());
        let (reports, omitted_reports) = BoundedReports::from_reports([report()]).unwrap();

        handle.try_send(pass(reports, omitted_reports)).unwrap();
        finish(handle, join).await;

        assert_eq!(transport.payload_count(), 1);
    }

    #[tokio::test]
    async fn idle_pass_sends_nothing() {
        let transport = Arc::new(RecordingTransport::scripted(Vec::new()));
        let (handle, join) = start_with_transport(transport.clone());
        let (reports, omitted_reports) = BoundedReports::from_reports(std::iter::empty()).unwrap();

        handle.try_send(pass(reports, omitted_reports)).unwrap();
        finish(handle, join).await;

        assert_eq!(transport.payload_count(), 0);
    }

    #[tokio::test]
    async fn omitted_reports_use_only_a_bounded_generic_summary() {
        let transport = Arc::new(RecordingTransport::scripted(vec![DeliveryResult::Accepted]));
        let (handle, join) = start_with_transport(transport.clone());
        let (reports, _) = BoundedReports::from_reports(std::iter::empty()).unwrap();

        handle.try_send(pass(reports, 3)).unwrap();
        finish(handle, join).await;

        let payload = String::from_utf8(transport.first_payload()).unwrap();
        assert!(payload.len() <= MAX_PAYLOAD_BYTES);
        assert!(payload.contains("state-capacity: at least 3 reports rejected by state capacity"));
        assert!(!payload.contains("episode 3"));
    }

    #[tokio::test]
    async fn render_failure_does_not_stop_later_delivery() {
        let transport = Arc::new(RecordingTransport::scripted(vec![DeliveryResult::Accepted]));
        let (handle, join) = start_with_transport(transport.clone());
        let (bad_reports, bad_omitted) =
            BoundedReports::from_reports([render_failure_report()]).unwrap();
        let (good_reports, good_omitted) = BoundedReports::from_reports([report()]).unwrap();

        handle.try_send(pass(bad_reports, bad_omitted)).unwrap();
        handle.try_send(pass(good_reports, good_omitted)).unwrap();
        finish(handle, join).await;

        assert_eq!(transport.payload_count(), 1);
    }

    #[tokio::test]
    async fn transport_failure_does_not_stop_later_delivery() {
        let transport = Arc::new(RecordingTransport::scripted(vec![
            DeliveryResult::Accepted,
            DeliveryResult::Failed(SafeDeliveryError::Transport),
        ]));
        let (handle, join) = start_with_transport(transport.clone());
        let (first_reports, first_omitted) = BoundedReports::from_reports([report()]).unwrap();
        let (second_reports, second_omitted) = BoundedReports::from_reports([report()]).unwrap();

        handle.try_send(pass(first_reports, first_omitted)).unwrap();
        handle
            .try_send(pass(second_reports, second_omitted))
            .unwrap();
        finish(handle, join).await;

        assert_eq!(transport.payload_count(), 2);
    }

    #[test]
    fn delivery_view_keeps_notifier_inputs_state_free() {
        let (reports, _) = BoundedReports::from_reports([report()]).unwrap();
        let view = DeliveryView::new(0, reports, OverflowSummaryV1::new(0, 0, 0, 0, 2, 0));
        assert_eq!(view.state_generation(), 0);
        assert_eq!(view.overflow_summary().pre_admission_drops(), 2);
    }
}
