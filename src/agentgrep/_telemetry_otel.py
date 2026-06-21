"""Lazy OpenTelemetry and Pyroscope backend."""

from __future__ import annotations

import collections.abc as cabc
import contextlib
import hashlib
import json
import logging
import os
import pathlib
import sys
import typing as t
import warnings

from agentgrep import _telemetry

if t.TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.trace.export import SpanExporter

_SAFE_LOG_ATTRIBUTE_KEYS: frozenset[str] = frozenset(
    {
        "agentgrep_path_kind",
        "agentgrep_env_path_status",
        "agentgrep_override_path_status",
    },
)
"""Structured log extras that are classifiers rather than private values."""

_STRUCTURED_LOG_IDENTITY_KEYS: frozenset[str] = frozenset(
    {
        "span_id",
        "spanid",
        "spanID",
        "trace_id",
        "traceid",
        "traceID",
    },
)
"""Non-project log record keys that are safe and useful in Loki bodies."""

_COUNTER_METRIC_NAMES: frozenset[str] = frozenset(
    {
        "agentgrep.otel.cpu_loops",
        "agentgrep.otel.sqlite_total",
    },
)
"""Monotonic counter metrics whose names do not carry a ``.count`` suffix."""


def _metric_is_counter(name: str) -> bool:
    """Return whether a metric name uses a monotonic counter instrument.

    Counters carry a ``.count`` suffix or appear in
    :data:`_COUNTER_METRIC_NAMES`; every other metric is a histogram.
    """
    return name.endswith(".count") or name in _COUNTER_METRIC_NAMES


@contextlib.contextmanager
def _pyroscope_span_scope(otel_span: t.Any) -> cabc.Iterator[None]:
    """Tag the active profiler thread with the span id for one child span.

    ``PyroscopeSpanProcessor`` only tags root spans, so CPU work that runs under
    child spans on executor threads carries no ``span_id`` for span-scoped
    profile linking. Tagging the active thread here lets ``tracesToProfilesV2``
    join a span to its flamegraph.
    """
    try:
        import pyroscope
        from opentelemetry.trace import format_span_id

        scope = pyroscope.tag_wrapper(
            {"span_id": format_span_id(otel_span.get_span_context().span_id)},
        )
    except Exception:
        yield
        return
    with scope:
        yield


class OtelTelemetryBackend:
    """OpenTelemetry-backed telemetry backend."""

    def __init__(
        self,
        *,
        tracer: t.Any,
        tracer_provider: t.Any,
        meter: t.Any,
        meter_provider: t.Any,
        logger_provider: t.Any,
        logging_handler: logging.Handler,
        span_counter: t.Any,
        span_duration: t.Any,
        instrumentations: tuple[t.Any, ...],
        profiles_started: bool,
        trace_api: t.Any | None = None,
    ) -> None:
        self._tracer = tracer
        self._tracer_provider = tracer_provider
        self._meter = meter
        self._meter_provider = meter_provider
        self._logger_provider = logger_provider
        self._logging_handler = logging_handler
        self._span_counter = span_counter
        self._span_duration = span_duration
        self._trace_api = trace_api
        self._counters: dict[str, t.Any] = {}
        self._histograms: dict[str, t.Any] = {}
        self._instrumentations = instrumentations
        self.profiles_started = profiles_started

    @contextlib.contextmanager
    def start_span(self, span: _telemetry._SpanState) -> cabc.Iterator[t.Any]:
        """Start an OTel span."""
        from opentelemetry import trace

        context = None
        if span.parent_id is None:
            context = trace.set_span_in_context(trace.INVALID_SPAN)
        with self._tracer.start_as_current_span(span.name, context=context) as otel_span:
            for key, value in span.attributes.items():
                if value is not None:
                    otel_span.set_attribute(key, value)
            if self.profiles_started and span.parent_id is not None:
                with _pyroscope_span_scope(otel_span):
                    yield otel_span
            else:
                yield otel_span

    def finish_span(
        self,
        span: _telemetry._SpanState,
        *,
        status: str,
        duration_seconds: float,
    ) -> None:
        """Record standard span metrics."""
        attributes = _telemetry._metric_attributes(span.name, status, span.attributes)
        self._span_counter.add(1, attributes=attributes)
        self._span_duration.record(max(0.0, duration_seconds), attributes=attributes)

    def set_span_attribute(self, key: str, value: _telemetry.TelemetryAttribute) -> None:
        """Set an attribute on the current OTel span."""
        if value is None:
            return
        from opentelemetry import trace

        trace.get_current_span().set_attribute(key, value)

    def record_exception(self, error: BaseException) -> None:
        """Record an exception on the current OTel span."""
        from opentelemetry import trace
        from opentelemetry.trace import Status, StatusCode

        active_span = trace.get_current_span()
        active_span.record_exception(error)
        active_span.set_status(Status(StatusCode.ERROR, str(error)))

    def set_span_status_error(self, description: str) -> None:
        """Mark the current OTel span as errored without an exception."""
        from opentelemetry import trace
        from opentelemetry.trace import Status, StatusCode

        trace.get_current_span().set_status(Status(StatusCode.ERROR, description))

    def record_metric(
        self,
        name: str,
        value: int | float,
        attributes: _telemetry.TelemetryAttributes,
    ) -> None:
        """Record a named metric point."""
        if _metric_is_counter(name):
            counter = self._counters.get(name)
            if counter is None:
                counter = self._meter.create_counter(name)
                self._counters[name] = counter
            counter.add(value, attributes=attributes)
        else:
            histogram = self._histograms.get(name)
            if histogram is None:
                histogram = self._meter.create_histogram(name)
                self._histograms[name] = histogram
            histogram.record(value, attributes=attributes)

    def emit_log(
        self,
        record: logging.LogRecord,
        active_span: _telemetry._SpanState | None,
    ) -> None:
        """Export ``record`` through OTel logs."""
        if active_span is None and not self._has_current_otel_span():
            return
        self._logging_handler.emit(_sanitized_log_record(record))

    def _has_current_otel_span(self) -> bool:
        """Return whether the OTel context has a valid current span."""
        trace_api = self._trace_api
        if trace_api is None:
            try:
                from opentelemetry import trace as trace_api
            except Exception:
                return False
        try:
            current_span = trace_api.get_current_span()
            if current_span is getattr(trace_api, "INVALID_SPAN", None):
                return False
            span_context = current_span.get_span_context()
            return bool(getattr(span_context, "is_valid", False))
        except Exception:
            return False

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Flush pending traces, metrics, and logs."""
        return all(
            _force_flush_provider(provider, timeout_millis=timeout_millis)
            for provider in (
                self._tracer_provider,
                self._meter_provider,
                self._logger_provider,
            )
        )

    def shutdown(self) -> None:
        """Flush and release telemetry processors."""
        for instrumentation in self._instrumentations:
            with contextlib.suppress(Exception):
                instrumentation.uninstrument()
        with contextlib.suppress(Exception):
            self.force_flush()
        with contextlib.suppress(Exception):
            self._tracer_provider.shutdown()
        with contextlib.suppress(Exception):
            self._meter_provider.shutdown()
        with contextlib.suppress(Exception):
            self._logger_provider.shutdown()
        with contextlib.suppress(Exception):
            import pyroscope

            shutdown = getattr(pyroscope, "shutdown", None)
            if shutdown is not None:
                shutdown()


def _force_flush_provider(provider: t.Any, *, timeout_millis: int) -> bool:
    """Force-flush one OTel provider across SDK signature variants."""
    force_flush = getattr(provider, "force_flush", None)
    if force_flush is None:
        return True
    try:
        result = force_flush(timeout_millis=timeout_millis)
    except TypeError:
        try:
            result = force_flush()
        except Exception:
            return False
    except Exception:
        return False
    return result is not False


class _FilteringSpanExporter:
    """Drop root spans that are not app-level roots."""

    def __init__(self, wrapped: t.Any) -> None:
        self._wrapped = wrapped

    def export(self, spans: cabc.Sequence[ReadableSpan]) -> t.Any:
        """Export spans after app-root filtering."""
        filtered = [
            span
            for span in spans
            if span.parent is not None or span.name in _telemetry.APP_ROOT_SPAN_NAMES
        ]
        if not filtered:
            from opentelemetry.sdk.trace.export import SpanExportResult

            return SpanExportResult.SUCCESS
        return self._wrapped.export(filtered)

    def shutdown(self) -> None:
        """Shut down the wrapped exporter."""
        self._wrapped.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Flush the wrapped exporter."""
        return bool(self._wrapped.force_flush(timeout_millis=timeout_millis))


def build_backend(
    *,
    mode: _telemetry.TelemetryMode,
    resource_attributes: _telemetry.TelemetryAttributes,
    explicit: bool = True,
) -> OtelTelemetryBackend:
    """Build an OpenTelemetry/Pyroscope backend.

    Passive local telemetry (``explicit`` false) still exports traces, metrics,
    and logs but skips Pyroscope and auto-instrumentation so an in-repo
    ``agentgrep --help`` stays fast.
    """
    from opentelemetry import metrics, trace
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="`LoggingHandler` in `opentelemetry-sdk` is deprecated.*",
            category=DeprecationWarning,
        )
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogRecordExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import (
        ConsoleMetricExporter,
        PeriodicExportingMetricReader,
    )
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    resource = Resource.create(
        {key: value for key, value in resource_attributes.items() if value is not None},
    )
    profiles_started = _configure_profiles(resource_attributes) if explicit else False

    tracer_provider = TracerProvider(resource=resource)
    if profiles_started:
        with contextlib.suppress(Exception):
            from pyroscope.otel import PyroscopeSpanProcessor

            tracer_provider.add_span_processor(PyroscopeSpanProcessor())
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            t.cast(
                "SpanExporter",
                _FilteringSpanExporter(OTLPSpanExporter(timeout=_timeout_seconds())),
            ),
        ),
    )
    if mode == "debug-console":
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                t.cast(
                    "SpanExporter",
                    _FilteringSpanExporter(ConsoleSpanExporter(out=sys.stderr)),
                ),
            ),
        )
    trace.set_tracer_provider(tracer_provider)

    metric_readers = [
        PeriodicExportingMetricReader(
            OTLPMetricExporter(timeout=_timeout_seconds()),
            export_interval_millis=1_000,
        ),
    ]
    if mode == "debug-console":
        metric_readers.append(
            PeriodicExportingMetricReader(
                ConsoleMetricExporter(out=sys.stderr),
                export_interval_millis=1_000,
            ),
        )
    meter_provider = MeterProvider(resource=resource, metric_readers=metric_readers)
    metrics.set_meter_provider(meter_provider)
    meter = metrics.get_meter("agentgrep")
    span_counter = meter.create_counter(
        "agentgrep.span.count",
        description="Number of completed agentgrep spans.",
    )
    span_duration = meter.create_histogram(
        "agentgrep.span.duration",
        unit="s",
        description="Duration of completed agentgrep spans.",
    )

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(timeout=_timeout_seconds())),
    )
    if mode == "debug-console":
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(ConsoleLogRecordExporter(out=sys.stderr)),
        )
    set_logger_provider(logger_provider)
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="`LoggingHandler` in `opentelemetry-sdk` is deprecated.*",
            category=DeprecationWarning,
        )
        logging_handler = LoggingHandler(logger_provider=logger_provider)
    logging_handler.setFormatter(_StructuredTelemetryLogFormatter())

    instrumentations = _install_auto_instrumentation(mode) if explicit else ()
    return OtelTelemetryBackend(
        tracer=trace.get_tracer("agentgrep"),
        tracer_provider=tracer_provider,
        meter=meter,
        meter_provider=meter_provider,
        logger_provider=logger_provider,
        logging_handler=logging_handler,
        span_counter=span_counter,
        span_duration=span_duration,
        instrumentations=instrumentations,
        profiles_started=profiles_started,
    )


def _configure_profiles(resource_attributes: _telemetry.TelemetryAttributes) -> bool:
    """Start Pyroscope profiling for enabled telemetry modes."""
    try:
        import pyroscope
    except Exception:
        return False
    server_address = os.environ.get("PYROSCOPE_SERVER_ADDRESS", "http://localhost:4040")
    tags = _profile_tags(resource_attributes)
    try:
        pyroscope.configure(
            application_name=str(resource_attributes.get("service.name") or "agentgrep"),
            server_address=server_address,
            sample_rate=100,
            oncpu=True,
            gil_only=True,
            enable_logging=False,
            tags=tags,
        )
    except Exception:
        return False
    return True


def _profile_tags(resource_attributes: _telemetry.TelemetryAttributes) -> dict[str, str]:
    """Return Pyroscope tags derived from privacy-safe resource attributes."""
    tags = {
        key.replace(".", "_"): str(value)
        for key, value in resource_attributes.items()
        if value is not None and key not in {"service.name", "service.version"}
    }
    repository = resource_attributes.get("vcs.repository.url.full")
    if repository is not None:
        tags["service_repository"] = str(repository)
        tags["service_root_path"] = "."
    git_ref = resource_attributes.get("vcs.ref.head.revision") or resource_attributes.get(
        "vcs.ref.head.name",
    )
    if git_ref is not None:
        tags["service_git_ref"] = str(git_ref)
    return tags


def _install_auto_instrumentation(mode: _telemetry.TelemetryMode) -> tuple[t.Any, ...]:
    """Install debug/live auto-instrumentation.

    SQLite spans come solely from
    :func:`agentgrep._telemetry.sqlite_connection_factory`, which traces the
    ``Connection.execute`` shortcut path agentgrep uses; ``SQLite3Instrumentor``
    only covers the cursor path agentgrep never takes, so it is not installed.
    """
    if mode not in {"local", "debug", "debug-console", "live"}:
        return ()
    installed: list[t.Any] = []
    with contextlib.suppress(Exception):
        from opentelemetry.instrumentation.asyncio import AsyncioInstrumentor

        asyncio_instrumentor = AsyncioInstrumentor()
        asyncio_instrumentor.instrument()
        installed.append(asyncio_instrumentor)
    return tuple(installed)


def _timeout_seconds() -> float:
    """Return a short OTLP timeout."""
    raw_timeout = os.environ.get("OTEL_EXPORTER_OTLP_TIMEOUT")
    if raw_timeout:
        with contextlib.suppress(ValueError):
            return max(0.1, float(raw_timeout))
    return 0.5


def _sanitized_log_record(record: logging.LogRecord) -> logging.LogRecord:
    """Return a shallow log-record copy without local absolute path metadata."""
    copied = logging.makeLogRecord(_sanitized_log_record_dict(record))
    if copied.pathname:
        copied.pathname = pathlib.Path(copied.pathname).name
    if copied.filename:
        copied.filename = pathlib.Path(copied.filename).name
    return copied


def _sanitized_log_record_dict(record: logging.LogRecord) -> dict[str, object]:
    """Return a copied record dict with private project extras redacted."""
    copied: dict[str, object] = record.__dict__.copy()
    for key, value in tuple(copied.items()):
        if not _is_sensitive_log_attribute(key):
            continue
        del copied[key]
        copied.update(_redacted_log_attribute_metadata(key, value))
    return copied


def _structured_log_body(record: logging.LogRecord) -> str:
    """Return a privacy-safe structured JSON body for exported OTel logs."""
    copied = _sanitized_log_record_dict(record)
    body: dict[str, object] = {
        "message": record.getMessage(),
        "level": record.levelname,
        "logger": record.name,
    }
    for key in sorted(copied):
        value = copied[key]
        if not (key.startswith("agentgrep_") or key in _STRUCTURED_LOG_IDENTITY_KEYS):
            continue
        if _is_log_json_scalar(value):
            body[key] = value
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def _is_log_json_scalar(value: object) -> bool:
    """Return whether ``value`` is safe to encode as a structured log scalar."""
    return isinstance(value, str | int | float | bool) or value is None


class _StructuredTelemetryLogFormatter(logging.Formatter):
    """Formatter that sends structured JSON bodies to OTel only."""

    def format(self, record: logging.LogRecord) -> str:
        """Return the structured JSON log body."""
        return _structured_log_body(record)


def _is_sensitive_log_attribute(key: str) -> bool:
    """Return whether a structured log extra can contain private user data."""
    if key in _SAFE_LOG_ATTRIBUTE_KEYS or not key.startswith("agentgrep_"):
        return False
    key_folded = key.casefold()
    return (
        "path" in key_folded
        or "query" in key_folded
        or "argv" in key_folded
        or key_folded.endswith("_env")
        or "_env_" in key_folded
    )


def _redacted_log_attribute_metadata(key: str, value: object) -> dict[str, object]:
    """Return structured metadata for a redacted log extra."""
    metadata: dict[str, object] = {f"{key}_redacted": True}
    if isinstance(value, str):
        metadata[f"{key}_len"] = len(value)
        metadata[f"{key}_sha256_prefix"] = hashlib.sha256(
            value.encode("utf-8"),
        ).hexdigest()[:12]
        metadata[f"{key}_is_absolute"] = pathlib.PurePath(value).is_absolute()
    else:
        metadata[f"{key}_type"] = type(value).__name__
    return metadata
