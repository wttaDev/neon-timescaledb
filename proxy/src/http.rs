//! HTTP client and server impls.
//! Other modules should use stuff from this module instead of
//! directly relying on deps like `reqwest` (think loose coupling).

pub mod conn_pool;
pub mod server;
pub mod sql_over_http;
pub mod websocket;

use std::time::Duration;

pub use reqwest::{Request, Response, StatusCode};
pub use reqwest_middleware::{ClientWithMiddleware, Error};
pub use reqwest_retry::{policies::ExponentialBackoff, RetryTransientMiddleware};

use crate::url::ApiUrl;
use reqwest_middleware::RequestBuilder;

/// This is the preferred way to create new http clients,
/// because it takes care of observability (OpenTelemetry).
/// We deliberately don't want to replace this with a public static.
pub fn new_client() -> ClientWithMiddleware {
    reqwest_middleware::ClientBuilder::new(reqwest::Client::new())
        .with(reqwest_tracing::TracingMiddleware::default())
        .build()
}

pub fn new_client_with_timeout(default_timout: Duration) -> ClientWithMiddleware {
    let timeout_client = reqwest::ClientBuilder::new()
        .timeout(default_timout)
        .build()
        .expect("Failed to create http client with timeout");

    let retry_policy =
        ExponentialBackoff::builder().build_with_total_retry_duration(default_timout);

    reqwest_middleware::ClientBuilder::new(timeout_client)
        .with(reqwest_tracing::TracingMiddleware::default())
        // As per docs, "This middleware always errors when given requests with streaming bodies".
        // That's all right because we only use this client to send `serde_json::RawValue`, which
        // is not a stream.
        .with(RetryTransientMiddleware::new_with_policy(retry_policy))
        .build()
}

/// Thin convenience wrapper for an API provided by an http endpoint.
#[derive(Debug, Clone)]
pub struct Endpoint {
    /// API's base URL.
    endpoint: ApiUrl,
    /// Connection manager with built-in pooling.
    client: ClientWithMiddleware,
}

impl Endpoint {
    /// Construct a new HTTP endpoint wrapper.
    /// Http client is not constructed under the hood so that it can be shared.
    pub fn new(endpoint: ApiUrl, client: impl Into<ClientWithMiddleware>) -> Self {
        Self {
            endpoint,
            client: client.into(),
        }
    }

    #[inline(always)]
    pub fn url(&self) -> &ApiUrl {
        &self.endpoint
    }

    /// Return a [builder](RequestBuilder) for a `GET` request,
    /// appending a single `path` segment to the base endpoint URL.
    pub fn get(&self, path: &str) -> RequestBuilder {
        let mut url = self.endpoint.clone();
        url.path_segments_mut().push(path);
        self.client.get(url.into_inner())
    }

    /// Execute a [request](reqwest::Request).
    pub async fn execute(&self, request: Request) -> Result<Response, Error> {
        self.client.execute(request).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use reqwest::Client;

    #[test]
    fn optional_query_params() -> anyhow::Result<()> {
        let url = "http://example.com".parse()?;
        let endpoint = Endpoint::new(url, Client::new());

        // Validate that this pattern makes sense.
        let req = endpoint
            .get("frobnicate")
            .query(&[
                ("foo", Some("10")), // should be just `foo=10`
                ("bar", None),       // shouldn't be passed at all
            ])
            .build()?;

        assert_eq!(req.url().as_str(), "http://example.com/frobnicate?foo=10");

        Ok(())
    }

    #[test]
    fn uuid_params() -> anyhow::Result<()> {
        let url = "http://example.com".parse()?;
        let endpoint = Endpoint::new(url, Client::new());

        let req = endpoint
            .get("frobnicate")
            .query(&[("session_id", uuid::Uuid::nil())])
            .build()?;

        assert_eq!(
            req.url().as_str(),
            "http://example.com/frobnicate?session_id=00000000-0000-0000-0000-000000000000"
        );

        Ok(())
    }
}
