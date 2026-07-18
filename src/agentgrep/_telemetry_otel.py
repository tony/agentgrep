"""Lazy OpenTelemetry and Pyroscope backend."""

from __future__ import annotations

import collections.abc as cabc
import contextlib
import hashlib
import logging
import os
import pathlib
import sys
import time
import typing as t
import warnings

from opentelemetry.context import Context
from opentelemetry.sdk.trace import Event, ReadableSpan, Span, SpanProcessor
from opentelemetry.sdk.trace.sampling import Sampler, SamplingResult
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import Link, SpanKind, Status, StatusCode, TraceState
from opentelemetry.util.types import Attributes

from agentgrep import _telemetry

if t.TYPE_CHECKING:
    from opentelemetry.sdk.trace.export import SpanExporter

_SAFE_LOG_ATTRIBUTE_KEYS: frozenset[str] = frozenset(
    {
        "agentgrep_env_path_len",
        "agentgrep_env_path_redacted",
        "agentgrep_path_kind",
        "agentgrep_env_path_status",
        "agentgrep_override_path_status",
    },
)
"""Structured log extras that are classifiers rather than private values."""

_COUNTER_METRIC_NAMES: frozenset[str] = frozenset(
    {
        "agentgrep.otel.cpu_loops",
        "agentgrep.otel.sqlite_total",
    },
)
"""Monotonic counter metrics whose names do not carry a ``.count`` suffix."""

_FASTMCP_SPAN_NAMES: dict[str, str] = {
    "prompts/get": "fastmcp.prompts.get",
    "prompts/list": "fastmcp.prompts.list",
    "resources/list": "fastmcp.resources.list",
    "resources/read": "fastmcp.resources.read",
    "resources/templates/list": "fastmcp.resources.templates.list",
    "tools/call": "fastmcp.tools.call",
    "tools/list": "fastmcp.tools.list",
}
"""Finite native FastMCP method-to-span-name vocabulary."""

_FASTMCP_COMPONENT_TYPES: frozenset[str] = frozenset(
    {"prompt", "resource", "resource_template", "tool"},
)
"""Finite native FastMCP component classifiers safe for export."""


def _metric_is_counter(name: str) -> bool:
    """Return whether a metric name uses a monotonic counter instrument.

    Counters carry a ``.count`` suffix or appear in
    :data:`_COUNTER_METRIC_NAMES`; every other metric is a histogram.
    """
    return name.endswith(".count") or name in _COUNTER_METRIC_NAMES


class _SanitizingSpanProcessor(SpanProcessor):
    """Forward immutable privacy-safe span views to one processor."""

    def __init__(self, delegate: SpanProcessor) -> None:
        self._delegate = delegate

    def on_start(
        self,
        span: Span,
        parent_context: Context | None = None,
    ) -> None:
        """Forward span start so processors can retain lifecycle behavior."""
        with contextlib.suppress(Exception):
            self._delegate.on_start(span, parent_context=parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        """Sanitize the ended span before a processor can export it."""
        try:
            sanitized = _sanitized_export_span(span)
        except Exception:
            return
        with contextlib.suppress(Exception):
            self._delegate.on_end(sanitized)

    def shutdown(self) -> None:
        """Shut down the wrapped processor."""
        with contextlib.suppress(Exception):
            self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Flush the wrapped processor."""
        try:
            return self._delegate.force_flush(timeout_millis)
        except Exception:
            return False


class _AppRootSpanProcessor(SpanProcessor):
    """Expose only finite app roots to the Pyroscope span processor."""

    def __init__(self, delegate: SpanProcessor) -> None:
        self._delegate = delegate

    def on_start(
        self,
        span: Span,
        parent_context: Context | None = None,
    ) -> None:
        """Forward approved app-root starts without renaming the live span."""
        scope = span.instrumentation_scope
        if (
            scope is None
            or scope.name != "agentgrep"
            or span.name not in _telemetry.APP_ROOT_SPAN_NAMES
        ):
            return
        with contextlib.suppress(Exception):
            self._delegate.on_start(span, parent_context=parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        """Forward the matching approved app-root end."""
        scope = span.instrumentation_scope
        if (
            scope is None
            or scope.name != "agentgrep"
            or span.name not in _telemetry.APP_ROOT_SPAN_NAMES
        ):
            return
        with contextlib.suppress(Exception):
            self._delegate.on_end(span)

    def shutdown(self) -> None:
        """Shut down the wrapped processor."""
        with contextlib.suppress(Exception):
            self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Flush the wrapped processor."""
        try:
            return self._delegate.force_flush(timeout_millis)
        except Exception:
            return False


def _sanitized_export_span(span: ReadableSpan) -> ReadableSpan:
    """Return a default-deny export view for native FastMCP spans."""
    scope = span.instrumentation_scope
    if scope is None or scope.name != "fastmcp":
        return span

    raw_attributes = span.attributes or {}
    raw_method = raw_attributes.get("mcp.method.name")
    method = raw_method if isinstance(raw_method, str) else ""
    name = _FASTMCP_SPAN_NAMES.get(method, "fastmcp.request")
    safe_attributes: dict[str, str] = {
        "mcp.method.name": method if method in _FASTMCP_SPAN_NAMES else "unknown",
    }
    raw_component_type = raw_attributes.get("fastmcp.component.type")
    if isinstance(raw_component_type, str) and raw_component_type in _FASTMCP_COMPONENT_TYPES:
        safe_attributes["fastmcp.component.type"] = raw_component_type

    exception_events = [event for event in span.events if event.name == "exception"]
    has_error = (
        span.status.status_code is StatusCode.ERROR
        or raw_attributes.get("error.type") is not None
        or bool(exception_events)
    )
    error_type = "tool_error" if raw_attributes.get("error.type") == "tool_error" else "Exception"
    if has_error:
        safe_attributes["error.type"] = error_type
    safe_events = ()
    if exception_events:
        safe_events = (
            Event(
                "exception",
                {"exception.type": error_type},
                timestamp=exception_events[0].timestamp,
            ),
        )

    return ReadableSpan(
        name=name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=safe_attributes,
        events=safe_events,
        links=tuple(Link(link.context) for link in span.links),
        kind=span.kind,
        status=Status(span.status.status_code),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=InstrumentationScope("fastmcp"),
    )


def attach_otel_context(inbound: object) -> cabc.Callable[[], None]:
    """Attach an inbound OTel context, returning a detach callback."""
    from opentelemetry import context as otel_context

    token = otel_context.attach(t.cast("t.Any", inbound))
    return lambda: otel_context.detach(token)


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
        self.profiles_started = profiles_started
        self._shutdown = False

    @contextlib.contextmanager
    def start_span(self, span: _telemetry._SpanState) -> cabc.Iterator[t.Any]:
        """Start an OTel span and adopt its native trace and span ids.

        The facade mints placeholder ids; in live mode the OTel SDK owns the
        real W3C ids, so the active span state mirrors them. ``parent_id`` is
        already the parent's adopted span id because parents are started first.
        """
        from opentelemetry import trace
        from opentelemetry.trace import format_span_id, format_trace_id

        context = None
        if span.parent_id is None and not span.inherit_otel_context:
            context = trace.set_span_in_context(trace.INVALID_SPAN)
        with self._tracer.start_as_current_span(
            span.name,
            context=context,
            record_exception=False,
            set_status_on_exception=False,
        ) as otel_span:
            span_context = otel_span.get_span_context()
            span.trace_id = format_trace_id(span_context.trace_id)
            span.span_id = format_span_id(span_context.span_id)
            for key, value in span.attributes.items():
                if value is not None:
                    otel_span.set_attribute(key, value)
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
        """Record one bounded exception event on the current OTel span."""
        from opentelemetry import trace
        from opentelemetry.trace import Status, StatusCode

        active_span = trace.get_current_span()
        error_type = _telemetry.error_type_name(error)
        active_span.add_event("exception", {"exception.type": error_type})
        active_span.set_attribute("agentgrep_error_type", error_type)
        active_span.set_status(Status(StatusCode.ERROR))

    def set_span_status_error(self, description: str) -> None:
        """Mark the current OTel span as errored without an exception."""
        from opentelemetry import trace
        from opentelemetry.trace import Status, StatusCode

        del description
        trace.get_current_span().set_status(Status(StatusCode.ERROR))

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
        """Flush every provider under one shared timeout budget."""
        deadline = time.monotonic() + max(0, timeout_millis) / 1_000
        results: list[bool] = []
        for provider in (
            self._tracer_provider,
            self._meter_provider,
            self._logger_provider,
        ):
            remaining_millis = max(0, int((deadline - time.monotonic()) * 1_000))
            results.append(
                _force_flush_provider(provider, timeout_millis=remaining_millis),
            )
        return all(results)

    def shutdown(self) -> None:
        """Release telemetry processors exactly once."""
        if self._shutdown:
            return
        self._shutdown = True
        with contextlib.suppress(Exception):
            self._tracer_provider.shutdown()
        with contextlib.suppress(Exception):
            self._meter_provider.shutdown()
        with contextlib.suppress(Exception):
            self._logger_provider.shutdown()
        # Only tear down Pyroscope when this backend actually started it.
        if self.profiles_started:
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


class _AppRootSampler(Sampler):
    """Sample app-root traces and inherit their local sampling decision."""

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes = None,
        links: cabc.Sequence[Link] | None = None,
        trace_state: TraceState | None = None,
    ) -> SamplingResult:
        """Return a bounded start-time decision without retaining trace state."""
        del trace_id, kind, links, trace_state
        from opentelemetry import trace
        from opentelemetry.sdk.trace.sampling import Decision
        from opentelemetry.trace import TraceFlags

        parent = trace.get_current_span(parent_context).get_span_context()
        parent_sampled = bool(parent.trace_flags & TraceFlags.SAMPLED)
        if not parent.is_valid:
            sampled = name in _telemetry.APP_ROOT_SPAN_NAMES
        elif parent.is_remote:
            sampled = name in _telemetry.APP_ROOT_SPAN_NAMES and parent_sampled
        else:
            sampled = parent_sampled
        decision = Decision.RECORD_AND_SAMPLE if sampled else Decision.DROP
        return SamplingResult(
            decision,
            attributes=attributes if sampled else None,
            trace_state=parent.trace_state,
        )

    def get_description(self) -> str:
        """Return the stable sampler description used by the SDK."""
        return "agentgrep-app-root"


def build_backend(
    *,
    mode: _telemetry.TelemetryMode,
    resource_attributes: _telemetry.TelemetryAttributes,
    explicit: bool = True,
    env: cabc.Mapping[str, str] | None = None,
) -> OtelTelemetryBackend:
    """Build an explicitly enabled OpenTelemetry/Pyroscope backend."""
    active_env = os.environ if env is None else env
    traces_enabled = explicit and _signal_export_enabled(active_env, "traces")
    metrics_enabled = explicit and _signal_export_enabled(active_env, "metrics")
    logs_enabled = explicit and _signal_export_enabled(active_env, "logs")
    from opentelemetry import metrics, trace
    from opentelemetry._logs import set_logger_provider

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="`LoggingHandler` in `opentelemetry-sdk` is deprecated.*",
            category=DeprecationWarning,
        )
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogRecordExporter
    from opentelemetry.sdk.metrics import (
        Counter,
        Histogram,
        MeterProvider,
        TraceBasedExemplarFilter,
    )
    from opentelemetry.sdk.metrics.export import (
        AggregationTemporality,
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

    tracer_provider = TracerProvider(resource=resource, sampler=_AppRootSampler())
    if profiles_started:
        with contextlib.suppress(Exception):
            from pyroscope.otel import PyroscopeSpanProcessor

            tracer_provider.add_span_processor(
                _AppRootSpanProcessor(PyroscopeSpanProcessor()),
            )
    if traces_enabled:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        tracer_provider.add_span_processor(
            _SanitizingSpanProcessor(
                BatchSpanProcessor(
                    t.cast(
                        "SpanExporter",
                        OTLPSpanExporter(timeout=_timeout_seconds(active_env)),
                    ),
                ),
            ),
        )
    if mode == "debug-console" and traces_enabled:
        tracer_provider.add_span_processor(
            _SanitizingSpanProcessor(
                BatchSpanProcessor(
                    t.cast(
                        "SpanExporter",
                        ConsoleSpanExporter(out=sys.stderr),
                    ),
                ),
            ),
        )
    trace.set_tracer_provider(tracer_provider)

    preferred_temporality: dict[type, AggregationTemporality] | None = None
    if mode == "live":
        # Live mode targets the LGTM stack, whose collector runs
        # deltatocumulative. Each one-shot CLI/benchmark/profile-engine process
        # exports a fresh CUMULATIVE stream that plateaus at its in-process
        # total and then dies, so rate()/increase() read 0 across processes.
        # Exporting DELTA lets the long-lived collector sum each process's
        # increment into one climbing cumulative series. Exemplars attach to
        # each delta point and survive the conversion (the processor's adder
        # seeds state from the first delta and never strips exemplars).
        preferred_temporality = {
            Counter: AggregationTemporality.DELTA,
            Histogram: AggregationTemporality.DELTA,
        }
    metric_readers: list[t.Any] = []
    if metrics_enabled:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

        metric_readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(
                    timeout=_timeout_seconds(active_env),
                    preferred_temporality=preferred_temporality,
                ),
                export_interval_millis=1_000,
            ),
        )
    if mode == "debug-console" and metrics_enabled:
        metric_readers.append(
            PeriodicExportingMetricReader(
                ConsoleMetricExporter(out=sys.stderr),
                export_interval_millis=1_000,
            ),
        )
    # Pin the trace-based exemplar filter so histogram/counter points recorded
    # inside a sampled span carry an exemplar (trace_id + span_id). This is the
    # SDK default, but pinning it keeps the metric->trace pivot working even if
    # the default changes or OTEL_METRICS_EXEMPLAR_FILTER is set in the env.
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=metric_readers,
        exemplar_filter=TraceBasedExemplarFilter(),
    )
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
    if logs_enabled:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter

        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(timeout=_timeout_seconds(active_env)),
            ),
        )
    if mode == "debug-console" and logs_enabled:
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

    return OtelTelemetryBackend(
        tracer=trace.get_tracer("agentgrep"),
        tracer_provider=tracer_provider,
        meter=meter,
        meter_provider=meter_provider,
        logger_provider=logger_provider,
        logging_handler=logging_handler,
        span_counter=span_counter,
        span_duration=span_duration,
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
            # 997 Hz (prime near 1 kHz, avoids aliasing) instead of the 100 Hz
            # default: a sub-second one-shot CLI run yields ~7 samples at 100 Hz
            # — too coarse to read — versus ~70+ here. Only opt-in dev/debug/live
            # modes reach this path, so the extra sampling cost is acceptable.
            # gil_only stays on, so native GIL-free CPU (rapidfuzz, sqlite) is
            # still excluded — long-lived surfaces profile that via wall time.
            sample_rate=997,
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


def _signal_export_enabled(env: cabc.Mapping[str, str], signal: str) -> bool:
    """Return whether the standard per-signal exporter is enabled."""
    value = env.get(f"OTEL_{signal.upper()}_EXPORTER")
    return value is None or value.strip().lower() != "none"


def _timeout_seconds(env: cabc.Mapping[str, str] | None = None) -> float:
    """Return a short OTLP timeout."""
    active_env = os.environ if env is None else env
    raw_timeout = active_env.get("OTEL_EXPORTER_OTLP_TIMEOUT")
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
    exc_info = copied.get("exc_info")
    if isinstance(exc_info, tuple) and exc_info and isinstance(exc_info[0], type):
        copied["agentgrep_exception_type"] = _telemetry.error_type_name(
            t.cast("type[BaseException]", exc_info[0]),
        )
    copied["exc_info"] = None
    copied["exc_text"] = None
    copied["stack_info"] = None
    for key, value in tuple(copied.items()):
        if not _is_sensitive_log_attribute(key):
            continue
        del copied[key]
        copied.update(_redacted_log_attribute_metadata(key, value))
    return copied


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
