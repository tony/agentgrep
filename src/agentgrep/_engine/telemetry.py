"""Shared, content-free telemetry for public engine operations."""

from __future__ import annotations

import collections.abc as cabc
import contextlib
import contextvars
import dataclasses
import functools
import logging
import sys
import time
import typing as t

from agentgrep import _telemetry

logger = logging.getLogger(__name__)

type EngineOperationKind = t.Literal["search", "find"]

_PLANNED_MESSAGES: dict[EngineOperationKind, str] = {
    "search": "search sources planned",
    "find": "find sources planned",
}
_COMPLETED_MESSAGES: dict[EngineOperationKind, str] = {
    "search": "search query completed",
    "find": "find query completed",
}
_CANCELLED_MESSAGES: dict[EngineOperationKind, str] = {
    "search": "search query cancelled",
    "find": "find query cancelled",
}
_FAILED_MESSAGES: dict[EngineOperationKind, str] = {
    "search": "search query failed",
    "find": "find query failed",
}


@dataclasses.dataclass(slots=True)
class EngineOperationTelemetry:
    """Bounded counters and events for one list or stream operation."""

    kind: EngineOperationKind
    attributes: dict[str, object]
    started_at: float
    source_count: int = 0
    finished: bool = False

    def sources_planned(self, source_count: int, planned_source_count: int) -> None:
        """Record post-filter and physical-plan source counts."""
        self.source_count = source_count
        _telemetry.set_span_attribute("agentgrep_source_count", source_count)
        _telemetry.set_span_attribute(
            "agentgrep_planned_source_count",
            planned_source_count,
        )
        logger.info(
            _PLANNED_MESSAGES[self.kind],
            extra={
                **self.attributes,
                "agentgrep_operation": f"{self.kind}.plan",
                "agentgrep_source_count": source_count,
                "agentgrep_planned_source_count": planned_source_count,
            },
        )

    def complete(self, result_count: int) -> None:
        """Record successful completion with bounded counts."""
        self.finished = True
        metric_attributes: dict[str, object] = {
            "agentgrep_surface": "engine",
            "agentgrep_component": "core",
            "agentgrep_component_kind": "in_process",
        }
        if self.kind == "search":
            metric_attributes["agentgrep_scope"] = self.attributes["agentgrep_scope"]
        else:
            metric_attributes["agentgrep_agent_count"] = self.attributes["agentgrep_agent_count"]
        _telemetry.record_metric(
            f"agentgrep.{self.kind}.sources",
            self.source_count,
            **metric_attributes,
        )
        _telemetry.record_metric(
            f"agentgrep.{self.kind}.results",
            result_count,
            **metric_attributes,
        )
        attributes = self._terminal_attributes("ok")
        attributes["agentgrep_result_count"] = result_count
        _telemetry.set_span_attribute("agentgrep_result_count", result_count)
        logger.info(_COMPLETED_MESSAGES[self.kind], extra=attributes)

    def cancel(self) -> None:
        """Record a consumer closing an event stream before completion."""
        if self.finished:
            return
        self.finished = True
        logger.info(
            _CANCELLED_MESSAGES[self.kind],
            extra=self._terminal_attributes("cancelled"),
        )

    def fail(self, error: BaseException) -> None:
        """Record a safe exception classifier without exception text."""
        self.finished = True
        attributes = self._terminal_attributes("error")
        attributes["agentgrep_error_type"] = type(error).__name__
        _telemetry.set_span_attribute("agentgrep_error_type", type(error).__name__)
        logger.error(_FAILED_MESSAGES[self.kind], extra=attributes)

    def _terminal_attributes(self, outcome: str) -> dict[str, object]:
        """Return common terminal attributes and update the active span."""
        duration_ms = (time.monotonic() - self.started_at) * 1000.0
        _telemetry.set_span_attribute("agentgrep_source_count", self.source_count)
        _telemetry.set_span_attribute("agentgrep_outcome", outcome)
        _telemetry.set_span_attribute("agentgrep_duration_ms", duration_ms)
        return {
            **self.attributes,
            "agentgrep_source_count": self.source_count,
            "agentgrep_outcome": outcome,
            "agentgrep_duration_ms": duration_ms,
        }


class _ContextBoundGenerator[YieldT, SendT, ReturnT](
    cabc.Generator[YieldT, SendT, ReturnT],
):
    """Delegate one generator's protocol inside its first operation context."""

    def __init__(
        self,
        generator: cabc.Generator[YieldT, SendT, ReturnT],
    ) -> None:
        self._context: contextvars.Context | None = None
        self._generator = generator

    def _run_in_context[ResultT](
        self,
        operation: cabc.Callable[..., ResultT],
        /,
        *args: t.Any,
    ) -> ResultT:
        """Run one operation, retaining a new context only after success."""
        if self._context is not None:
            return self._context.run(operation, *args)
        candidate = contextvars.copy_context()
        try:
            result = candidate.run(operation, *args)
        except StopIteration:
            self._context = candidate
            raise
        self._context = candidate
        return result

    def __next__(self) -> YieldT:
        """Resume the bound generator inside its retained context."""
        return self._run_in_context(self._generator.__next__)

    def send(self, value: SendT, /) -> YieldT:
        """Send a value inside the retained context."""
        return self._run_in_context(self._generator.send, value)

    def throw(self, *args: t.Any) -> YieldT:
        """Inject an exception inside the retained context."""
        return self._run_in_context(self._generator.throw, *args)

    def close(self) -> ReturnT | None:
        """Close the bound generator inside its retained context."""
        return self._run_in_context(self._generator.close)

    def __del__(self) -> None:
        """Close an abandoned generator inside its retained context."""
        try:
            if self._context is None:
                self._generator.close()
            else:
                self._context.run(self._generator.close)
        except Exception:
            if not sys.is_finalizing():
                raise


def isolate_generator_context[**Params, YieldT, SendT, ReturnT](
    function: cabc.Callable[Params, cabc.Generator[YieldT, SendT, ReturnT]],
) -> cabc.Callable[Params, cabc.Generator[YieldT, SendT, ReturnT]]:
    """Run each generator in its first protocol operation's context."""

    @functools.wraps(function)
    def wrapped(
        *args: Params.args,
        **kwargs: Params.kwargs,
    ) -> cabc.Generator[YieldT, SendT, ReturnT]:
        generator = function(*args, **kwargs)
        return _ContextBoundGenerator(generator)

    return wrapped


@contextlib.contextmanager
def engine_operation(
    kind: EngineOperationKind,
    *,
    agent_count: int,
    scope: str | None = None,
    limit: int | None = None,
    pattern_present: bool | None = None,
) -> t.Iterator[EngineOperationTelemetry]:
    """Bracket one public list or event-stream engine boundary."""
    attributes: dict[str, object] = {
        "agentgrep_surface": "engine",
        "agentgrep_component": "core",
        "agentgrep_component_kind": "in_process",
        "agentgrep_operation": f"{kind}.run",
        "agentgrep_agent_count": agent_count,
        "agentgrep_limit": limit,
    }
    if scope is not None:
        attributes["agentgrep_scope"] = scope
    if pattern_present is not None:
        attributes["agentgrep_pattern_present"] = pattern_present
    operation = EngineOperationTelemetry(
        kind=kind,
        attributes=attributes,
        started_at=time.monotonic(),
    )
    with _telemetry.span(f"agentgrep.{kind}.run", **attributes):
        try:
            yield operation
        except GeneratorExit:
            operation.cancel()
        except BaseException as error:
            operation.fail(error)
            raise
        else:
            if not operation.finished:
                operation.cancel()
