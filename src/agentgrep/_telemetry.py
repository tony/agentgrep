"""Project-local telemetry helpers.

Application code instruments through this module. OpenTelemetry SDK/exporter
imports stay lazy in :mod:`agentgrep._telemetry_otel` so packaged users do not
need telemetry dependencies unless they opt in.
"""

from __future__ import annotations

import collections
import collections.abc as cabc
import contextlib
import contextvars
import dataclasses
import functools
import importlib.metadata
import logging
import os
import pathlib
import subprocess
import threading
import time
import typing as t
import urllib.parse
import uuid

if t.TYPE_CHECKING:
    import concurrent.futures


type TelemetryMode = t.Literal[
    "off",
    "local",
    "debug",
    "debug-console",
    "test",
    "live",
]
type TelemetryAttribute = str | int | float | bool | None
type TelemetryAttributes = dict[str, TelemetryAttribute]

APP_ROOT_SPAN_NAMES: frozenset[str] = frozenset(
    {
        "agentgrep.cli.invocation",
        "agentgrep.mcp.server",
        "agentgrep.tui.session",
        "agentgrep.mcp.request",
        "agentgrep.mcp.tool",
        "agentgrep.benchmark.run",
        "agentgrep.profile_engine.run",
        "agentgrep.pytest.session",
        "agentgrep.pytest.test",
        "agentgrep.otel.smoke",
    },
)
"""Allowed app-level root span names."""

_MODE_ALIASES: dict[str, TelemetryMode] = {
    "0": "off",
    "false": "off",
    "no": "off",
    "off": "off",
    "disabled": "off",
    "1": "local",
    "true": "local",
    "yes": "local",
    "on": "local",
    "local": "local",
    "debug": "debug",
    "debug-console": "debug-console",
    "console": "debug-console",
    "test": "test",
    "live": "live",
}

DEFAULT_SERVICE_NAME = "agentgrep"
SERVICE_NAMESPACE = "agentgrep"

_LOG_RECORD_BASE_KEYS: frozenset[str] = frozenset(logging.makeLogRecord({}).__dict__)


@dataclasses.dataclass(frozen=True, slots=True)
class SpanRecord:
    """One completed in-memory span."""

    name: str
    span_id: str
    trace_id: str
    parent_id: str | None
    attributes: TelemetryAttributes = dataclasses.field(default_factory=dict)
    status: str = "ok"
    duration_seconds: float = 0.0


@dataclasses.dataclass(frozen=True, slots=True)
class MetricRecord:
    """One in-memory metric point."""

    name: str
    value: int | float
    attributes: TelemetryAttributes = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True, slots=True)
class LogRecord:
    """One in-memory log export."""

    message: str
    level_name: str
    logger_name: str
    trace_id: str | None
    span_id: str | None
    attributes: TelemetryAttributes = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(slots=True)
class _SpanState:
    """Active project span context."""

    name: str
    span_id: str
    trace_id: str
    parent_id: str | None
    attributes: TelemetryAttributes
    started_at: float
    status: str = "ok"


class TelemetryBackend(t.Protocol):
    """Runtime backend interface."""

    def start_span(self, span: _SpanState) -> contextlib.AbstractContextManager[object]:
        """Start backend-specific span state."""

    def finish_span(self, span: _SpanState, *, status: str, duration_seconds: float) -> None:
        """Finish backend-specific span state."""

    def set_span_attribute(self, key: str, value: TelemetryAttribute) -> None:
        """Set an attribute on the active backend span."""

    def record_exception(self, error: BaseException) -> None:
        """Record an exception on the active backend span."""

    def set_span_status_error(self, description: str) -> None:
        """Mark the active backend span as errored without an exception."""

    def record_metric(
        self,
        name: str,
        value: int | float,
        attributes: TelemetryAttributes,
    ) -> None:
        """Record a metric point."""

    def emit_log(self, record: logging.LogRecord, active_span: _SpanState | None) -> None:
        """Export a log record."""

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Flush pending telemetry without releasing backend resources."""

    def shutdown(self) -> None:
        """Flush and release backend resources."""


class InMemoryTelemetryBackend:
    """Dependency-free backend for tests and smoke assertions."""

    def __init__(self, *, record_logs: bool = True) -> None:
        self.finished_spans: list[SpanRecord] = []
        self.metric_records: list[MetricRecord] = []
        self.log_records: list[LogRecord] = []
        self.record_logs = record_logs
        self.profiles_started = False
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def start_span(self, span: _SpanState) -> cabc.Iterator[None]:
        """Start a span."""
        del span
        yield

    def finish_span(self, span: _SpanState, *, status: str, duration_seconds: float) -> None:
        """Record a completed span and its standard metrics."""
        record = SpanRecord(
            name=span.name,
            span_id=span.span_id,
            trace_id=span.trace_id,
            parent_id=span.parent_id,
            attributes=dict(span.attributes),
            status=status,
            duration_seconds=max(0.0, duration_seconds),
        )
        with self._lock:
            self.finished_spans.append(record)
            self.metric_records.append(
                MetricRecord(
                    name="agentgrep.span.duration",
                    value=record.duration_seconds,
                    attributes=_metric_attributes(span.name, status, span.attributes),
                ),
            )
            self.metric_records.append(
                MetricRecord(
                    name="agentgrep.span.count",
                    value=1,
                    attributes=_metric_attributes(span.name, status, span.attributes),
                ),
            )

    def set_span_attribute(self, key: str, value: TelemetryAttribute) -> None:
        """Set an attribute on the active span."""
        del key, value

    def record_exception(self, error: BaseException) -> None:
        """Record an exception on the active span."""
        del error

    def set_span_status_error(self, description: str) -> None:
        """No-op: span status flows through the active span state."""
        del description

    def record_metric(
        self,
        name: str,
        value: int | float,
        attributes: TelemetryAttributes,
    ) -> None:
        """Record a metric point."""
        with self._lock:
            self.metric_records.append(
                MetricRecord(name=name, value=value, attributes=dict(attributes)),
            )

    def emit_log(self, record: logging.LogRecord, active_span: _SpanState | None) -> None:
        """Capture a trace-linked log record."""
        if not self.record_logs or active_span is None:
            return
        attributes = {
            key: _safe_attribute_value(value)
            for key, value in record.__dict__.items()
            if key.startswith("agentgrep_") and key not in _LOG_RECORD_BASE_KEYS
        }
        log_record = LogRecord(
            message=record.getMessage(),
            level_name=record.levelname,
            logger_name=record.name,
            trace_id=None if active_span is None else active_span.trace_id,
            span_id=None if active_span is None else active_span.span_id,
            attributes=attributes,
        )
        with self._lock:
            self.log_records.append(log_record)

    def shutdown(self) -> None:
        """Release backend resources."""

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Flush pending telemetry."""
        del timeout_millis
        return True

    def single_root_trace_ids(self) -> tuple[str, ...]:
        """Return trace IDs with exactly one span."""
        spans_by_trace: dict[str, list[SpanRecord]] = collections.defaultdict(list)
        for span_record in self.finished_spans:
            spans_by_trace[span_record.trace_id].append(span_record)
        return tuple(
            trace_id
            for trace_id, span_records in spans_by_trace.items()
            if len(span_records) == 1 and span_records[0].parent_id is None
        )


class _TelemetryLogHandler(logging.Handler):
    """Root logging handler that forwards records into telemetry."""

    def __init__(self, backend: TelemetryBackend) -> None:
        super().__init__(level=logging.NOTSET)
        self._backend = backend

    def emit(self, record: logging.LogRecord) -> None:
        """Forward ``record`` to the backend."""
        self._backend.emit_log(record, _CURRENT_SPAN.get())


@dataclasses.dataclass(slots=True)
class TelemetryHandle:
    """Configured telemetry lifecycle."""

    mode: TelemetryMode
    backend: TelemetryBackend | None = None
    _backend_token: contextvars.Token[TelemetryBackend | None] | None = None
    _resource_token: contextvars.Token[TelemetryAttributes | None] | None = None
    _remove_logging: cabc.Callable[[], None] | None = None

    def shutdown(self) -> None:
        """Flush telemetry and restore logging hooks."""
        if self._remove_logging is not None:
            self._remove_logging()
            self._remove_logging = None
        if self.backend is not None:
            backend = self.backend
            self.backend = None
            backend.shutdown()
        if self._backend_token is not None:
            _BACKEND.reset(self._backend_token)
            self._backend_token = None
        if self._resource_token is not None:
            _RESOURCE_ATTRIBUTES.reset(self._resource_token)
            self._resource_token = None

    def force_flush(self, *, timeout_millis: int = 30_000) -> bool:
        """Flush pending telemetry while keeping exporters active."""
        if self.backend is None:
            return True
        return self.backend.force_flush(timeout_millis=timeout_millis)


_BACKEND: contextvars.ContextVar[TelemetryBackend | None] = contextvars.ContextVar(
    "agentgrep_telemetry_backend",
    default=None,
)
_CURRENT_SPAN: contextvars.ContextVar[_SpanState | None] = contextvars.ContextVar(
    "agentgrep_current_span",
    default=None,
)
_RESOURCE_ATTRIBUTES: contextvars.ContextVar[TelemetryAttributes | None] = contextvars.ContextVar(
    "agentgrep_resource_attributes",
    default=None,
)
_SQL_STATEMENT_MAX = 512
_SQLITE_CONNECTION_FACTORY: type[t.Any] | None = None
_VCS_RESOURCE_TO_METRIC_ATTRIBUTES: tuple[tuple[str, str], ...] = (
    ("vcs.repository.name", "vcs_repository_name"),
    ("vcs.repository.url.full", "vcs_repository_url_full"),
    ("vcs.ref.head.name", "vcs_ref_head_name"),
    ("vcs.ref.head.revision", "vcs_ref_head_revision"),
    ("vcs.ref.head.type", "vcs_ref_head_type"),
)


def resolve_mode(
    *,
    env: cabc.Mapping[str, str] | None = None,
    repo_root: pathlib.Path | None = None,
) -> TelemetryMode:
    """Resolve the active telemetry mode."""
    active_env = os.environ if env is None else env
    raw_mode = active_env.get("AGENTGREP_OTEL")
    if raw_mode is not None:
        return _MODE_ALIASES.get(raw_mode.strip().lower(), "off")
    if _running_under_pytest(active_env):
        return "off"
    if repo_root is not None and (repo_root / ".git").exists():
        return "local"
    return "off"


def resolve_explicit(
    mode: TelemetryMode | None,
    env: cabc.Mapping[str, str] | None = None,
) -> bool:
    """Return whether telemetry is an explicit opt-in rather than passive local.

    Passive local telemetry is the auto-resolved ``local`` mode of a git
    checkout with ``AGENTGREP_OTEL`` unset; it keeps traces, metrics, and logs
    but skips the heavier Pyroscope profiler and auto-instrumentation to protect
    cold start. Any explicit ``mode`` argument or ``AGENTGREP_OTEL`` value is an
    opt-in that keeps every signal.
    """
    if mode is not None:
        return True
    active_env = os.environ if env is None else env
    return active_env.get("AGENTGREP_OTEL") is not None


def setup(
    *,
    mode: TelemetryMode | None = None,
    env: cabc.Mapping[str, str] | None = None,
    repo_root: pathlib.Path | None = None,
    service_name: str | None = None,
    service_version: str | None = None,
) -> TelemetryHandle:
    """Configure telemetry for the current execution context."""
    active_env = os.environ if env is None else env
    active_mode = resolve_mode(env=active_env, repo_root=repo_root) if mode is None else mode
    if active_mode == "off":
        return TelemetryHandle(mode=active_mode)
    explicit = resolve_explicit(mode, active_env)
    resource_attributes = build_resource_attributes(
        env=active_env,
        repo_root=repo_root,
        service_name=service_name,
        service_version=service_version or package_version(),
    )
    if active_mode == "test":
        backend: TelemetryBackend = InMemoryTelemetryBackend()
    else:
        try:
            from agentgrep import _telemetry_otel

            backend = _telemetry_otel.build_backend(
                mode=active_mode,
                resource_attributes=resource_attributes,
                explicit=explicit,
            )
        except Exception:
            return TelemetryHandle(mode=active_mode)
    token = _BACKEND.set(backend)
    resource_token = _RESOURCE_ATTRIBUTES.set(resource_attributes)
    handle = TelemetryHandle(
        mode=active_mode,
        backend=backend,
        _backend_token=token,
        _resource_token=resource_token,
    )
    if active_mode in {"local", "debug", "debug-console", "live", "test"}:
        handle._remove_logging = install_logging_exporter(backend)
    return handle


def package_version() -> str:
    """Return the installed package version."""
    try:
        return importlib.metadata.version("agentgrep")
    except importlib.metadata.PackageNotFoundError:
        return "0+unknown"


def build_resource_attributes(
    *,
    env: cabc.Mapping[str, str] | None = None,
    repo_root: pathlib.Path | None = None,
    service_name: str | None = None,
    service_version: str,
) -> TelemetryAttributes:
    """Build resource attributes without overloading ``service.version``."""
    active_env = os.environ if env is None else env
    attributes: TelemetryAttributes = {
        "service.name": _resolve_service_name(active_env, service_name),
        "service.namespace": SERVICE_NAMESPACE,
        "service.version": service_version,
    }
    for env_name, attr_name in (
        ("AGENTGREP_DEBUG_SESSION_ID", "agentgrep.debug.session_id"),
        ("AGENTGREP_DEBUG_CANDIDATE_ID", "agentgrep.debug.candidate_id"),
        ("AGENTGREP_PYTEST_RUN_ID", "agentgrep.pytest.run_id"),
    ):
        value = active_env.get(env_name)
        if value:
            attributes[attr_name] = value
    attempt = active_env.get("AGENTGREP_DEBUG_ATTEMPT")
    if attempt:
        with contextlib.suppress(ValueError):
            attributes["agentgrep.debug.attempt"] = int(attempt)
    attributes.update(_vcs_resource_attributes(repo_root))
    return attributes


def _resolve_service_name(active_env: cabc.Mapping[str, str], service_name: str | None) -> str:
    """Return the OTel service name for this process."""
    env_service_name = active_env.get("OTEL_SERVICE_NAME")
    if env_service_name and env_service_name.strip():
        return env_service_name.strip()
    if service_name and service_name.strip():
        return service_name.strip()
    return DEFAULT_SERVICE_NAME


def configure_backend(backend: TelemetryBackend | None) -> None:
    """Set the active backend for tests."""
    _BACKEND.set(backend)


def active_backend() -> TelemetryBackend | None:
    """Return the active telemetry backend."""
    return _BACKEND.get()


def current_span_id() -> str | None:
    """Return the active project span ID."""
    active_span = _CURRENT_SPAN.get()
    return None if active_span is None else active_span.span_id


def current_trace_id() -> str | None:
    """Return the active project trace ID."""
    active_span = _CURRENT_SPAN.get()
    return None if active_span is None else active_span.trace_id


def sql_span(name: str, **attributes: object) -> contextlib.AbstractContextManager[object]:
    """Create a SQL child span only inside an active project trace."""
    if _BACKEND.get() is None or _CURRENT_SPAN.get() is None:
        return contextlib.nullcontext()
    return span(name, **attributes)


def sqlite_connection_factory() -> type[t.Any]:
    """Return a SQLite connection class that traces connection shortcuts."""
    global _SQLITE_CONNECTION_FACTORY
    if _SQLITE_CONNECTION_FACTORY is None:
        import sqlite3

        class _TelemetrySqliteConnection(sqlite3.Connection):
            """Trace ``Connection`` shortcut methods missed by DB-API wrappers."""

            def execute(self, *args: t.Any, **kwargs: t.Any) -> t.Any:
                """Execute SQL under an agentgrep SQL child span."""
                with sql_span(
                    "agentgrep.sqlite.execute",
                    **_sqlite_span_attributes("execute", args, kwargs),
                ):
                    try:
                        return super().execute(*args, **kwargs)
                    finally:
                        _record_sqlite_metric("execute")

            def executemany(self, *args: t.Any, **kwargs: t.Any) -> t.Any:
                """Execute batched SQL under an agentgrep SQL child span."""
                with sql_span(
                    "agentgrep.sqlite.executemany",
                    **_sqlite_span_attributes("executemany", args, kwargs),
                ):
                    try:
                        return super().executemany(*args, **kwargs)
                    finally:
                        _record_sqlite_metric("executemany")

            def executescript(self, *args: t.Any, **kwargs: t.Any) -> t.Any:
                """Execute a SQL script under an agentgrep SQL child span."""
                with sql_span(
                    "agentgrep.sqlite.executescript",
                    **_sqlite_span_attributes("executescript", args, kwargs),
                ):
                    try:
                        return super().executescript(*args, **kwargs)
                    finally:
                        _record_sqlite_metric("executescript")

        _SQLITE_CONNECTION_FACTORY = _TelemetrySqliteConnection
    return _SQLITE_CONNECTION_FACTORY


def set_span_attribute(key: str, value: object) -> None:
    """Set an attribute on the active span."""
    active_span = _CURRENT_SPAN.get()
    safe_value = _safe_attribute_value(value)
    if active_span is not None:
        active_span.attributes[key] = safe_value
    backend = _BACKEND.get()
    if backend is not None:
        backend.set_span_attribute(key, safe_value)


def mark_span_error(description: str) -> None:
    """Mark the active span as errored without raising an exception.

    Use for handled, non-exception failures such as a CLI parse error so the
    span carries ``StatusCode.ERROR`` and Tempo's error filter selects it.
    Exception paths already set status through
    :meth:`TelemetryBackend.record_exception`.
    """
    active_span = _CURRENT_SPAN.get()
    if active_span is not None:
        active_span.status = "error"
    backend = _BACKEND.get()
    if backend is not None:
        backend.set_span_status_error(description)


@contextlib.contextmanager
def span(name: str, **attributes: object) -> cabc.Iterator[None]:
    """Create a span in the active backend."""
    backend = _BACKEND.get()
    if backend is None:
        yield
        return
    parent = _CURRENT_SPAN.get()
    # Placeholder ids: the OTel backend adopts the native span ids in live mode;
    # the in-memory backend keeps these for offline deterministic tests.
    active_span = _SpanState(
        name=name,
        span_id=uuid.uuid4().hex[:16],
        trace_id=parent.trace_id if parent is not None else uuid.uuid4().hex,
        parent_id=parent.span_id if parent is not None else None,
        attributes={key: _safe_attribute_value(value) for key, value in attributes.items()},
        started_at=time.perf_counter(),
    )
    try:
        with backend.start_span(active_span):
            token = _CURRENT_SPAN.set(active_span)
            try:
                yield
            except BaseException as exc:
                active_span.status = "error"
                backend.record_exception(exc)
                raise
            finally:
                _CURRENT_SPAN.reset(token)
    finally:
        backend.finish_span(
            active_span,
            status=active_span.status,
            duration_seconds=time.perf_counter() - active_span.started_at,
        )


@contextlib.contextmanager
def root_span(name: str, **attributes: object) -> cabc.Iterator[None]:
    """Create a span that starts a new trace even when a span is active."""
    token = _CURRENT_SPAN.set(None)
    try:
        with span(name, **attributes):
            yield
    finally:
        _CURRENT_SPAN.reset(token)


def record_metric(name: str, value: int | float, **attributes: object) -> None:
    """Record a metric point with active run identity."""
    backend = _BACKEND.get()
    if backend is None:
        return
    backend.record_metric(
        name,
        value,
        {
            **{key: _safe_attribute_value(val) for key, val in attributes.items()},
            **_metric_identity_attributes(),
        },
    )


def record_work_metric(value: int | float, *, work_kind: str, **attributes: object) -> None:
    """Record a CPU-impacting app work counter."""
    if value <= 0:
        return
    if attributes.get("agentgrep_surface") == "engine":
        attributes.setdefault("agentgrep_component", "core")
        attributes.setdefault("agentgrep_component_kind", "in_process")
    record_metric(
        "agentgrep.otel.cpu_loops",
        value,
        agentgrep_work_kind=work_kind,
        **attributes,
    )


def install_logging_exporter(backend: TelemetryBackend) -> cabc.Callable[[], None]:
    """Attach telemetry log export to the root logger."""
    handler = _TelemetryLogHandler(backend)
    root_logger = logging.getLogger()
    agentgrep_logger = logging.getLogger("agentgrep")
    previous_agentgrep_level = agentgrep_logger.level
    if agentgrep_logger.level > logging.INFO or agentgrep_logger.level == logging.NOTSET:
        agentgrep_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)

    def remove_handler() -> None:
        root_logger.removeHandler(handler)
        agentgrep_logger.setLevel(previous_agentgrep_level)
        handler.close()

    return remove_handler


def executor_submit(
    executor: concurrent.futures.Executor,
    fn: cabc.Callable[..., t.Any],
    /,
    *args: t.Any,
    **kwargs: t.Any,
) -> concurrent.futures.Future[t.Any]:
    """Submit work while preserving telemetry context."""
    context = contextvars.copy_context()
    return executor.submit(context.run, fn, *args, **kwargs)


async def to_thread(
    fn: cabc.Callable[..., t.Any],
    /,
    *args: t.Any,
    **kwargs: t.Any,
) -> t.Any:
    """Run ``fn`` in a thread while preserving telemetry context."""
    import asyncio

    context = contextvars.copy_context()
    return await asyncio.to_thread(context.run, fn, *args, **kwargs)


def wrap_callable_context(fn: cabc.Callable[..., t.Any]) -> cabc.Callable[..., t.Any]:
    """Return ``fn`` wrapped in the current context."""
    context = contextvars.copy_context()

    @functools.wraps(fn)
    def wrapped(*args: t.Any, **kwargs: t.Any) -> t.Any:
        return context.run(fn, *args, **kwargs)

    return wrapped


def flatten_safe_attributes(prefix: str, value: object) -> TelemetryAttributes:
    """Flatten a redacted nested payload into scalar attributes."""
    attributes: TelemetryAttributes = {}

    def visit(name: str, item: object) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                visit(f"{name}.{key}", child)
            return
        if isinstance(item, list):
            attributes[f"{name}.count"] = len(item)
            for index, child in enumerate(item):
                visit(f"{name}.{index}", child)
            return
        attributes[name] = _safe_attribute_value(item)

    visit(prefix, value)
    return attributes


def _metric_attributes(
    span_name: str,
    status: str,
    span_attributes: TelemetryAttributes,
) -> TelemetryAttributes:
    """Return metric attributes for a completed span."""
    attributes: TelemetryAttributes = {
        "operation": span_name,
        "outcome": status,
    }
    for key in (
        "agentgrep_surface",
        "agentgrep_command",
        "agentgrep_scope",
        "agentgrep_tool",
        "agentgrep_sql_method",
        "agentgrep_work_kind",
        "agentgrep_source_strategy",
        "agentgrep_source_cost_hint",
        "agentgrep_subprocess_kind",
        "agentgrep_benchmark_command",
    ):
        value = span_attributes.get(key)
        if value is not None:
            attributes[key] = value
    attributes.update(_metric_identity_attributes())
    return attributes


def _metric_identity_attributes() -> TelemetryAttributes:
    """Return active debug/run identity for metrics."""
    resource_attributes = _RESOURCE_ATTRIBUTES.get()
    if resource_attributes is None:
        return {}
    metric_attributes: TelemetryAttributes = {}
    for resource_key, metric_key in (
        ("agentgrep.debug.session_id", "agentgrep_debug_session_id"),
        ("agentgrep.debug.attempt", "agentgrep_debug_attempt"),
        ("agentgrep.pytest.run_id", "agentgrep_pytest_run_id"),
        *_VCS_RESOURCE_TO_METRIC_ATTRIBUTES,
    ):
        value = resource_attributes.get(resource_key)
        if value is not None:
            metric_attributes[metric_key] = _safe_attribute_value(value)
    return metric_attributes


def _vcs_resource_attributes(repo_root: pathlib.Path | None) -> TelemetryAttributes:
    """Return OpenTelemetry VCS semantic-convention resource attributes."""
    if repo_root is None:
        return {}
    git_root = _git_output(repo_root, "rev-parse", "--show-toplevel")
    if git_root is None:
        return {}
    resolved_root = pathlib.Path(git_root)
    attributes: TelemetryAttributes = {}
    head_revision = _git_output(resolved_root, "rev-parse", "HEAD")
    if head_revision is not None:
        attributes["vcs.ref.head.revision"] = head_revision
    head_branch = _git_head_branch(resolved_root)
    if head_branch is not None:
        attributes["vcs.ref.head.name"] = head_branch
        attributes["vcs.ref.head.type"] = "branch"
    else:
        head_tag = _git_output(resolved_root, "describe", "--tags", "--exact-match", "HEAD")
        if head_tag is not None:
            attributes["vcs.ref.head.name"] = head_tag
            attributes["vcs.ref.head.type"] = "tag"
    repository_url = _canonical_repository_url(
        _git_output(resolved_root, "config", "--get", "remote.origin.url"),
    )
    if repository_url is not None:
        attributes["vcs.repository.url.full"] = repository_url
        repository_name = _repository_name_from_url(repository_url)
    else:
        repository_name = resolved_root.name
    if repository_name:
        attributes["vcs.repository.name"] = repository_name
    return attributes


def _git_head_branch(repo_root: pathlib.Path) -> str | None:
    """Return the current branch, including detached-at-branch-tip worktrees."""
    branch = _git_output(repo_root, "branch", "--show-current")
    if branch is not None:
        return branch
    for refs_pattern in ("refs/heads/*", "refs/remotes/*"):
        candidate = _git_output(
            repo_root,
            "name-rev",
            "--name-only",
            f"--refs={refs_pattern}",
            "HEAD",
        )
        branch = _normalize_git_branch_name(candidate)
        if branch is not None:
            return branch
    return None


def _normalize_git_branch_name(candidate: str | None) -> str | None:
    """Return a clean branch name from ``git name-rev`` output."""
    if candidate is None or candidate in {"", "HEAD", "undefined"}:
        return None
    if "~" in candidate or "^" in candidate:
        return None
    if candidate.startswith("remotes/"):
        parts = candidate.split("/")
        if len(parts) >= 3:
            return "/".join(parts[2:])
    return candidate


def _git_output(repo_root: pathlib.Path, *args: str) -> str | None:
    """Run a bounded read-only git command and return stripped stdout."""
    try:
        completed = subprocess.run(
            ("git", *args),
            cwd=repo_root,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            capture_output=True,
            text=True,
            check=False,
            timeout=0.5,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if completed.returncode != 0:
        return None
    output = completed.stdout.strip()
    return output or None


def _canonical_repository_url(raw_url: str | None) -> str | None:
    """Return a browser-style repository URL without local path remotes."""
    if raw_url is None:
        return None
    candidate = raw_url.strip()
    if not candidate:
        return None
    scp_like_url = _canonical_scp_like_git_url(candidate)
    if scp_like_url is not None:
        return scp_like_url
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        netloc = _parsed_url_netloc_without_userinfo(parsed)
        if netloc is None:
            return None
        return _strip_git_suffix(
            urllib.parse.urlunparse(
                (
                    parsed.scheme,
                    netloc,
                    parsed.path.rstrip("/"),
                    "",
                    "",
                    "",
                ),
            ),
        )
    if parsed.scheme == "ssh" and parsed.hostname and parsed.path:
        return _strip_git_suffix(f"https://{parsed.hostname}/{parsed.path.lstrip('/')}")
    return None


def _parsed_url_netloc_without_userinfo(parsed: urllib.parse.ParseResult) -> str | None:
    """Return parsed URL host and port without username or password."""
    host = parsed.hostname
    if host is None:
        return None
    netloc = f"[{host}]" if ":" in host and not host.startswith("[") else host
    with contextlib.suppress(ValueError):
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
    return netloc


def _canonical_scp_like_git_url(candidate: str) -> str | None:
    """Convert ``git@example.com:owner/repo.git`` remotes to HTTPS URLs."""
    if "://" in candidate or "@" not in candidate or ":" not in candidate:
        return None
    user_host, repository_path = candidate.split(":", 1)
    if "/" in user_host:
        return None
    host = user_host.rsplit("@", 1)[-1]
    if not host or not repository_path:
        return None
    return _strip_git_suffix(f"https://{host}/{repository_path.lstrip('/')}")


def _strip_git_suffix(url: str) -> str:
    """Return ``url`` without the conventional trailing Git suffix."""
    stripped = url.rstrip("/")
    return stripped[:-4] if stripped.endswith(".git") else stripped


def _repository_name_from_url(repository_url: str) -> str | None:
    """Return the terminal repository name from a canonical repository URL."""
    parsed = urllib.parse.urlparse(repository_url)
    path = parsed.path.rstrip("/")
    if not path:
        return None
    return pathlib.PurePosixPath(path).name


def _running_under_pytest(env: cabc.Mapping[str, str]) -> bool:
    """Return whether the current process is managed by pytest."""
    return "PYTEST_CURRENT_TEST" in env or "PYTEST_VERSION" in env


def _safe_attribute_value(value: object) -> TelemetryAttribute:
    """Convert ``value`` to an OTel-safe scalar."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _sqlite_span_attributes(
    method: str,
    args: tuple[t.Any, ...],
    kwargs: dict[str, t.Any],
) -> dict[str, object]:
    """Build safe low-cardinality SQLite span attributes."""
    statement = _sqlite_statement_arg(args, kwargs)
    attributes: dict[str, object] = {
        "db.system": "sqlite",
        "agentgrep_sql_method": method,
    }
    if statement is None:
        return attributes
    normalized = _normalize_sql_statement(statement)
    if normalized:
        attributes["db.statement"] = normalized
        operation = normalized.split(maxsplit=1)[0].casefold()
        if operation:
            attributes["db.operation.name"] = operation
    return attributes


def _sqlite_statement_arg(args: tuple[t.Any, ...], kwargs: dict[str, t.Any]) -> object | None:
    """Return the SQL statement/script argument without reading parameters."""
    if args:
        return args[0]
    for key in ("sql", "sql_script"):
        if key in kwargs:
            return kwargs[key]
    return None


def _normalize_sql_statement(statement: object) -> str:
    """Return a bounded one-line SQL statement string."""
    if isinstance(statement, bytes):
        rendered = statement.decode("utf-8", errors="replace")
    else:
        rendered = str(statement)
    normalized = " ".join(rendered.split())
    if len(normalized) > _SQL_STATEMENT_MAX:
        return f"{normalized[:_SQL_STATEMENT_MAX]}..."
    return normalized


def _record_sqlite_metric(method: str) -> None:
    """Record one SQLite shortcut execution when it belongs to an app trace."""
    if _BACKEND.get() is None or _CURRENT_SPAN.get() is None:
        return
    record_metric(
        "agentgrep.otel.sqlite_total",
        1,
        agentgrep_surface="sqlite",
        agentgrep_sql_method=method,
    )
