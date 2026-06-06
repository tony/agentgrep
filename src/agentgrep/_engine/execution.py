"""Re-exports of physical-plan search execution helpers.

Source-local scanning lives in :mod:`agentgrep._engine.scanning` and
driver scheduling in :mod:`agentgrep._engine.scheduling`; this module
keeps both reachable through one import path.
"""

from __future__ import annotations

from agentgrep._engine.runtime import SearchRuntime
from agentgrep._engine.scanning import (
    SourceScanBatch,
    SourceScanCache,
    SourceScanCacheStats,
    SourceScanResult,
    iter_source_task_batches,
    iter_source_task_records,
    raw_text_skip_line_for_haystack_query,
    raw_text_skip_line_for_query,
    record_source_profile_sample,
    scan_source_task,
)
from agentgrep._engine.scheduling import (
    ExecutionDriver,
    ExecutionDriverConfig,
    ExecutionRecordEmitted,
    ExecutionSourceFinished,
    ExecutionSourceStarted,
    FrontierExecutionDriver,
    InlineExecutionDriver,
    SearchExecutionEvent,
    _FrontierState,
    select_execution_driver,
)

__all__ = [
    "ExecutionDriver",
    "ExecutionDriverConfig",
    "ExecutionRecordEmitted",
    "ExecutionSourceFinished",
    "ExecutionSourceStarted",
    "FrontierExecutionDriver",
    "InlineExecutionDriver",
    "SearchExecutionEvent",
    "SearchRuntime",
    "SourceScanBatch",
    "SourceScanCache",
    "SourceScanCacheStats",
    "SourceScanResult",
    "_FrontierState",
    "iter_source_task_batches",
    "iter_source_task_records",
    "raw_text_skip_line_for_haystack_query",
    "raw_text_skip_line_for_query",
    "record_source_profile_sample",
    "scan_source_task",
    "select_execution_driver",
]
