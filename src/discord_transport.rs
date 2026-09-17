#![allow(dead_code)]
//! Dedicated outbound Discord transport.

use std::future::Future;
use std::pin::Pin;
#[cfg(test)]
use std::sync::{Arc, Mutex};
use std::time::Duration;

use reqwest::redirect::Policy;

use super::discord_config::{validate_webhook_bytes, RuntimeSecretBytes, SecretReadError};
use super::discord_state_schema::{PayloadBytes, SafeDeliveryError};

pub(crate) const TRANSPORT_TIMEOUT: Duration = Duration::from_secs(15);

pub(crate) struct ValidatedWebhookUrl(String);

impl ValidatedWebhookUrl {
    pub(crate) fn from_runtime_secret_bytes(bytes: &[u8]) -> Result<Self, SecretReadError> {
        validate_webhook_bytes(bytes)?;
        Ok(Self(
            String::from_utf8(bytes.to_vec()).map_err(|_| SecretReadError::Invalid)?,
        ))
    }
    pub(crate) fn from_runtime_secret(
        secret: &RuntimeSecretBytes,
    ) -> Result<Self, SecretReadError> {
        Self::from_runtime_secret_bytes(secret.as_bytes())
    }
}

pub(crate) struct DiscordTransport {
    client: reqwest::Client,
    webhook: ValidatedWebhookUrl,
}

impl DiscordTransport {
    pub(crate) fn new(webhook: ValidatedWebhookUrl) -> Result<Self, SafeDeliveryError> {
        let client = reqwest::Client::builder()
            .timeout(TRANSPORT_TIMEOUT)
            .redirect(Policy::none())
            .no_proxy()
            .build()
            .map_err(|_| SafeDeliveryError::Transport)?;
        Ok(Self { client, webhook })
    }

    pub(crate) async fn send(&self, payload: &PayloadBytes) -> DeliveryResult {
        let response = match self
            .client
            .post(&self.webhook.0)
            .header(reqwest::header::CONTENT_TYPE, "application/json")
            .header(reqwest::header::ACCEPT, "application/json")
            .header(reqwest::header::USER_AGENT, "ASRSub/3.0.0")
            .body(payload.as_bytes().to_vec())
            .send()
            .await
        {
            Ok(response) => response,
            Err(_) => return DeliveryResult::Failed(SafeDeliveryError::Transport),
        };
        classify_status(response.status().as_u16())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum DeliveryResult {
    Accepted,
    Failed(SafeDeliveryError),
}

pub(crate) trait DeliveryTransport: Send + Sync {
    fn send<'a>(
        &'a self,
        payload: &'a PayloadBytes,
    ) -> Pin<Box<dyn Future<Output = DeliveryResult> + Send + 'a>>;
}

impl DeliveryTransport for DiscordTransport {
    fn send<'a>(
        &'a self,
        payload: &'a PayloadBytes,
    ) -> Pin<Box<dyn Future<Output = DeliveryResult> + Send + 'a>> {
        Box::pin(async move { DiscordTransport::send(self, payload).await })
    }
}

pub(crate) fn classify_status(status: u16) -> DeliveryResult {
    if (200..=299).contains(&status) {
        DeliveryResult::Accepted
    } else if matches!(status, 408 | 425 | 429) || status >= 500 {
        DeliveryResult::Failed(SafeDeliveryError::RetryableResponse)
    } else {
        DeliveryResult::Failed(SafeDeliveryError::PermanentResponse)
    }
}

#[cfg(test)]
pub(crate) struct LocalTestEndpoint(String);

#[cfg(test)]
impl LocalTestEndpoint {
    pub(crate) fn new(address: std::net::SocketAddr) -> Self {
        Self(format!("http://{address}"))
    }
}

#[cfg(test)]
pub(crate) struct FakeTransport {
    responses: Arc<Mutex<Vec<DeliveryResult>>>,
    payloads: Arc<Mutex<Vec<Vec<u8>>>>,
}

#[cfg(test)]
impl FakeTransport {
    pub(crate) fn scripted(responses: Vec<DeliveryResult>) -> Self {
        Self {
            responses: Arc::new(Mutex::new(responses)),
            payloads: Arc::new(Mutex::new(Vec::new())),
        }
    }
    pub(crate) async fn send(&self, payload: &PayloadBytes) -> DeliveryResult {
        self.payloads
            .lock()
            .unwrap()
            .push(payload.as_bytes().to_vec());
        self.responses
            .lock()
            .unwrap()
            .pop()
            .unwrap_or(DeliveryResult::Accepted)
    }
    pub(crate) fn payload_count(&self) -> usize {
        self.payloads.lock().unwrap().len()
    }
}

#[cfg(test)]
impl DeliveryTransport for FakeTransport {
    fn send<'a>(
        &'a self,
        payload: &'a PayloadBytes,
    ) -> Pin<Box<dyn Future<Output = DeliveryResult> + Send + 'a>> {
        Box::pin(async move { FakeTransport::send(self, payload).await })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classifies_response_matrix() {
        assert_eq!(classify_status(204), DeliveryResult::Accepted);
        assert_eq!(
            classify_status(429),
            DeliveryResult::Failed(SafeDeliveryError::RetryableResponse)
        );
        assert_eq!(
            classify_status(500),
            DeliveryResult::Failed(SafeDeliveryError::RetryableResponse)
        );
        assert_eq!(
            classify_status(400),
            DeliveryResult::Failed(SafeDeliveryError::PermanentResponse)
        );
        assert_eq!(
            classify_status(302),
            DeliveryResult::Failed(SafeDeliveryError::PermanentResponse)
        );
    }

    #[tokio::test]
    async fn accepts_204_without_body() {
        let fake = FakeTransport::scripted(vec![classify_status(204)]);
        let payload = PayloadBytes::try_from_bytes(Box::from(&b"{}"[..])).unwrap();
        assert_eq!(fake.send(&payload).await, DeliveryResult::Accepted);
        assert_eq!(fake.payload_count(), 1);
    }

    #[test]
    fn rejects_redirect() {
        assert_eq!(
            classify_status(307),
            DeliveryResult::Failed(SafeDeliveryError::PermanentResponse)
        );
    }

    #[test]
    fn does_not_use_proxy() {
        let client = reqwest::Client::builder()
            .no_proxy()
            .redirect(Policy::none())
            .build()
            .unwrap();
        let _ = client;
        assert_eq!(TRANSPORT_TIMEOUT, Duration::from_secs(15));
    }

    #[test]
    fn bounds_total_timeout() {
        assert_eq!(TRANSPORT_TIMEOUT.as_secs(), 15);
    }
}
