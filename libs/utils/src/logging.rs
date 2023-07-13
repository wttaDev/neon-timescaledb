use std::str::FromStr;

use anyhow::Context;
use once_cell::sync::Lazy;
use strum_macros::{EnumString, EnumVariantNames};

#[derive(EnumString, EnumVariantNames, Eq, PartialEq, Debug, Clone, Copy)]
#[strum(serialize_all = "snake_case")]
pub enum LogFormat {
    Plain,
    Json,
    Test,
}

impl LogFormat {
    pub fn from_config(s: &str) -> anyhow::Result<LogFormat> {
        use strum::VariantNames;
        LogFormat::from_str(s).with_context(|| {
            format!(
                "Unrecognized log format. Please specify one of: {:?}",
                LogFormat::VARIANTS
            )
        })
    }
}

static TRACING_EVENT_COUNT: Lazy<metrics::IntCounterVec> = Lazy::new(|| {
    metrics::register_int_counter_vec!(
        "libmetrics_tracing_event_count",
        "Number of tracing events, by level",
        &["level"]
    )
    .expect("failed to define metric")
});

struct TracingEventCountLayer(&'static metrics::IntCounterVec);

impl<S> tracing_subscriber::layer::Layer<S> for TracingEventCountLayer
where
    S: tracing::Subscriber,
{
    fn on_event(
        &self,
        event: &tracing::Event<'_>,
        _ctx: tracing_subscriber::layer::Context<'_, S>,
    ) {
        let level = event.metadata().level();
        let level = match *level {
            tracing::Level::ERROR => "error",
            tracing::Level::WARN => "warn",
            tracing::Level::INFO => "info",
            tracing::Level::DEBUG => "debug",
            tracing::Level::TRACE => "trace",
        };
        self.0.with_label_values(&[level]).inc();
    }
}

/// Whether to add the `tracing_error` crate's `ErrorLayer`
/// to the global tracing subscriber.
///
pub enum TracingErrorLayerEnablement {
    /// Do not add the `ErrorLayer`.
    Disabled,
    /// Add the `ErrorLayer` with the filter specified by RUST_LOG, defaulting to `info` if `RUST_LOG` is unset.
    EnableWithRustLogFilter,
}

pub fn init(
    log_format: LogFormat,
    tracing_error_layer_enablement: TracingErrorLayerEnablement,
) -> anyhow::Result<()> {
    // We fall back to printing all spans at info-level or above if
    // the RUST_LOG environment variable is not set.
    let rust_log_env_filter = || {
        tracing_subscriber::EnvFilter::try_from_default_env()
            .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info"))
    };

    // NB: the order of the with() calls does not matter.
    // See https://docs.rs/tracing-subscriber/0.3.16/tracing_subscriber/layer/index.html#per-layer-filtering
    use tracing_subscriber::prelude::*;
    let r = tracing_subscriber::registry();
    let r = r.with({
        let log_layer = tracing_subscriber::fmt::layer()
            .with_target(false)
            .with_ansi(false)
            .with_writer(std::io::stdout);
        let log_layer = match log_format {
            LogFormat::Json => log_layer.json().boxed(),
            LogFormat::Plain => log_layer.boxed(),
            LogFormat::Test => log_layer.with_test_writer().boxed(),
        };
        log_layer.with_filter(rust_log_env_filter())
    });
    let r = r.with(TracingEventCountLayer(&TRACING_EVENT_COUNT).with_filter(rust_log_env_filter()));
    match tracing_error_layer_enablement {
        TracingErrorLayerEnablement::EnableWithRustLogFilter => r
            .with(tracing_error::ErrorLayer::default().with_filter(rust_log_env_filter()))
            .init(),
        TracingErrorLayerEnablement::Disabled => r.init(),
    }

    Ok(())
}

/// Disable the default rust panic hook by using `set_hook`.
///
/// For neon binaries, the assumption is that tracing is configured before with [`init`], after
/// that sentry is configured (if needed). sentry will install it's own on top of this, always
/// processing the panic before we log it.
///
/// When the return value is dropped, the hook is reverted to std default hook (prints to stderr).
/// If the assumptions about the initialization order are not held, use
/// [`TracingPanicHookGuard::disarm`] but keep in mind, if tracing is stopped, then panics will be
/// lost.
#[must_use]
pub fn replace_panic_hook_with_tracing_panic_hook() -> TracingPanicHookGuard {
    std::panic::set_hook(Box::new(tracing_panic_hook));
    TracingPanicHookGuard::new()
}

/// Drop guard which restores the std panic hook on drop.
///
/// Tracing should not be used when it's not configured, but we cannot really latch on to any
/// imaginary lifetime of tracing.
pub struct TracingPanicHookGuard {
    act: bool,
}

impl TracingPanicHookGuard {
    fn new() -> Self {
        TracingPanicHookGuard { act: true }
    }

    /// Make this hook guard not do anything when dropped.
    pub fn forget(&mut self) {
        self.act = false;
    }
}

impl Drop for TracingPanicHookGuard {
    fn drop(&mut self) {
        if self.act {
            let _ = std::panic::take_hook();
        }
    }
}

/// Named symbol for our panic hook, which logs the panic.
fn tracing_panic_hook(info: &std::panic::PanicInfo) {
    // following rust 1.66.1 std implementation:
    // https://github.com/rust-lang/rust/blob/90743e7298aca107ddaa0c202a4d3604e29bfeb6/library/std/src/panicking.rs#L235-L288
    let location = info.location();

    let msg = match info.payload().downcast_ref::<&'static str>() {
        Some(s) => *s,
        None => match info.payload().downcast_ref::<String>() {
            Some(s) => &s[..],
            None => "Box<dyn Any>",
        },
    };

    let thread = std::thread::current();
    let thread = thread.name().unwrap_or("<unnamed>");
    let backtrace = std::backtrace::Backtrace::capture();

    let _entered = if let Some(location) = location {
        tracing::error_span!("panic", %thread, location = %PrettyLocation(location))
    } else {
        // very unlikely to hit here, but the guarantees of std could change
        tracing::error_span!("panic", %thread)
    }
    .entered();

    if backtrace.status() == std::backtrace::BacktraceStatus::Captured {
        // this has an annoying extra '\n' in the end which anyhow doesn't do, but we cannot really
        // get rid of it as we cannot get in between of std::fmt::Formatter<'_>; we could format to
        // string, maybe even to a TLS one but tracing already does that.
        tracing::error!("{msg}\n\nStack backtrace:\n{backtrace}");
    } else {
        tracing::error!("{msg}");
    }

    // ensure that we log something on the panic if this hook is left after tracing has been
    // unconfigured. worst case when teardown is racing the panic is to log the panic twice.
    tracing::dispatcher::get_default(|d| {
        if let Some(_none) = d.downcast_ref::<tracing::subscriber::NoSubscriber>() {
            let location = location.map(PrettyLocation);
            log_panic_to_stderr(thread, msg, location, &backtrace);
        }
    });
}

#[cold]
fn log_panic_to_stderr(
    thread: &str,
    msg: &str,
    location: Option<PrettyLocation<'_, '_>>,
    backtrace: &std::backtrace::Backtrace,
) {
    eprintln!("panic while tracing is unconfigured: thread '{thread}' panicked at '{msg}', {location:?}\nStack backtrace:\n{backtrace}");
}

struct PrettyLocation<'a, 'b>(&'a std::panic::Location<'b>);

impl std::fmt::Display for PrettyLocation<'_, '_> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}:{}:{}", self.0.file(), self.0.line(), self.0.column())
    }
}

impl std::fmt::Debug for PrettyLocation<'_, '_> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        <Self as std::fmt::Display>::fmt(self, f)
    }
}

#[cfg(test)]
mod tests {
    use metrics::{core::Opts, IntCounterVec};

    use super::TracingEventCountLayer;

    #[test]
    fn tracing_event_count_metric() {
        let counter_vec =
            IntCounterVec::new(Opts::new("testmetric", "testhelp"), &["level"]).unwrap();
        let counter_vec = Box::leak(Box::new(counter_vec)); // make it 'static
        let layer = TracingEventCountLayer(counter_vec);
        use tracing_subscriber::prelude::*;

        tracing::subscriber::with_default(tracing_subscriber::registry().with(layer), || {
            tracing::trace!("foo");
            tracing::debug!("foo");
            tracing::info!("foo");
            tracing::warn!("foo");
            tracing::error!("foo");
        });

        assert_eq!(counter_vec.with_label_values(&["trace"]).get(), 1);
        assert_eq!(counter_vec.with_label_values(&["debug"]).get(), 1);
        assert_eq!(counter_vec.with_label_values(&["info"]).get(), 1);
        assert_eq!(counter_vec.with_label_values(&["warn"]).get(), 1);
        assert_eq!(counter_vec.with_label_values(&["error"]).get(), 1);
    }
}
