"""Core types and suite registry for documentation example collection."""

from __future__ import annotations

import dataclasses
import enum
import pathlib
import posixpath
import re
import subprocess
import traceback
import types
import typing as t

if t.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest


_HOME_PATH_RE = re.compile(r"(^|[^\w])/(?:home|Users)/([^/\s]+)/")


def redact_text(text: str, *, project_root: pathlib.Path | None = None) -> str:
    """Redact local paths from arbitrary diagnostic text.

    Parameters
    ----------
    text : str
        Text that may contain local paths.
    project_root : pathlib.Path | None
        Project root used for project-relative replacement when practical.

    Returns
    -------
    str
        Redacted text suitable for terminal output.
    """
    redacted = text
    if project_root is not None:
        root = str(project_root.expanduser().resolve())
        if root in redacted:
            redacted = redacted.replace(root, ".")
    home = pathlib.Path.home()
    home_text = str(home)
    if home_text in redacted:
        redacted = redacted.replace(home_text, "~")
    return _HOME_PATH_RE.sub(lambda match: f"{match.group(1)}/home/<user>/", redacted)


def redact_path(path: pathlib.Path, *, project_root: pathlib.Path | None = None) -> str:
    """Return a privacy-preserving display path.

    Parameters
    ----------
    path : pathlib.Path
        Path to render.
    project_root : pathlib.Path | None
        Root used for project-relative rendering.

    Returns
    -------
    str
        Relative, home-relative, or redacted absolute path.
    """
    expanded = path.expanduser()
    if project_root is not None:
        try:
            return expanded.resolve().relative_to(project_root.expanduser().resolve()).as_posix()
        except ValueError:
            pass
    try:
        return posixpath.join("~", expanded.resolve().relative_to(pathlib.Path.home()).as_posix())
    except ValueError:
        return redact_text(expanded.as_posix(), project_root=project_root)


@dataclasses.dataclass(frozen=True, slots=True)
class ExampleLocation:
    """Location metadata for one documentation example."""

    path: pathlib.Path
    display_path: str
    start_line: int
    end_line: int
    start_index: int
    end_index: int
    prefix: str = ""
    indent: str = ""
    group: str = ""

    def label(self) -> str:
        """Return a compact human-readable label.

        Returns
        -------
        str
            ``path:start-end`` plus group when present.
        """
        if self.start_line == self.end_line:
            line_span = f"{self.start_line}"
        else:
            line_span = f"{self.start_line}-{self.end_line}"
        if self.group:
            return f"{self.display_path}:{line_span} {self.group}"
        return f"{self.display_path}:{line_span}"


@dataclasses.dataclass(frozen=True, slots=True)
class DocumentationExample:
    """Collected documentation example."""

    kind: str
    language: str
    source: str
    raw_source: str
    location: ExampleLocation
    tags: frozenset[str] = dataclasses.field(default_factory=frozenset)
    settings: t.Mapping[str, str] = dataclasses.field(default_factory=dict)
    test_id: str = ""

    def __post_init__(self) -> None:
        """Normalize mapping and test id fields."""
        if not isinstance(self.tags, frozenset):
            object.__setattr__(self, "tags", frozenset(self.tags))
        object.__setattr__(self, "settings", types.MappingProxyType(dict(self.settings)))
        if not self.test_id:
            safe_language = self.language or self.kind
            test_id = f"{self.location.display_path}:{self.location.start_line}:{safe_language}"
            object.__setattr__(self, "test_id", test_id)

    def __str__(self) -> str:
        """Return a pytest-friendly example id."""
        return self.test_id


class EvaluationStatus(enum.Enum):
    """Terminal status for one documentation example evaluation."""

    PASSED = "passed"
    FAILED = "failed"


class EvaluationFailureKind(enum.Enum):
    """Machine-readable reason for a failed documentation example."""

    NONE = "none"
    BLOCKED_BY_POLICY = "blocked_by_policy"
    LONG_RUNNING_COMMAND = "long_running_command"
    DATA_DEPENDENT_EMPTY_RESULT = "data_dependent_empty_result"
    COMMAND_FAILED = "command_failed"
    CONFIG_INVALID = "config_invalid"
    DOCTEST_FAILED = "doctest_failed"
    HARNESS_ERROR = "harness_error"


@dataclasses.dataclass(frozen=True, slots=True)
class ExampleDocument:
    """Source document provided to collectors."""

    path: pathlib.Path
    text: str
    context: CollectionContext

    @property
    def display_path(self) -> str:
        """Privacy-preserving path for this document."""
        return redact_path(self.path, project_root=self.context.project_root)


@dataclasses.dataclass(frozen=True, slots=True)
class CollectionContext:
    """Collection-time configuration shared across collectors."""

    project_root: pathlib.Path
    encoding: str = "utf-8"


@dataclasses.dataclass(frozen=True, slots=True)
class EvaluationResult:
    """Result returned by documentation example evaluators."""

    status: EvaluationStatus
    example: DocumentationExample
    failure_kind: EvaluationFailureKind = EvaluationFailureKind.NONE
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    message: str = ""

    @property
    def passed(self) -> bool:
        """Return whether the example evaluation passed."""
        return self.status is EvaluationStatus.PASSED

    @classmethod
    def passed_result(
        cls,
        example: DocumentationExample,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        message: str = "",
    ) -> EvaluationResult:
        """Create a passed evaluation result."""
        return cls(
            status=EvaluationStatus.PASSED,
            example=example,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            message=message,
        )

    @classmethod
    def failed_result(
        cls,
        example: DocumentationExample,
        *,
        failure_kind: EvaluationFailureKind,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        message: str = "",
    ) -> EvaluationResult:
        """Create a failed evaluation result."""
        return cls(
            status=EvaluationStatus.FAILED,
            example=example,
            failure_kind=failure_kind,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            message=message,
        )

    def failure_message(self) -> str:
        """Return a redacted failure message for pytest rendering.

        Returns
        -------
        str
            Diagnostic text with location, command output, and source.
        """
        if self.passed:
            return ""
        parts = [
            f"{self.example.location.label()}: classified documentation example failure",
            f"failure_kind: {self.failure_kind.value}",
        ]
        if self.message:
            parts.append(self.message)
        if self.returncode:
            parts.append(f"returncode: {self.returncode}")
        if self.stdout:
            parts.append("stdout:\n" + self.stdout.rstrip())
        if self.stderr:
            parts.append("stderr:\n" + self.stderr.rstrip())
        if self.example.source:
            parts.append("source:\n" + self.example.source.rstrip())
        return redact_text("\n\n".join(parts), project_root=self.example.location.path.parent)


# Evaluated-example failures are deliberately distinct from harness errors.
class DocumentationExampleFailure(AssertionError):  # noqa: N818
    """Raised by pytest items when an example evaluation fails."""

    def __init__(self, result: EvaluationResult) -> None:
        """Store the evaluation result for custom pytest rendering."""
        super().__init__(result.failure_message())
        self.result = result


class DocumentationCollector(t.Protocol):
    """Collector protocol for documentation examples."""

    name: str
    suffixes: frozenset[str]

    def collect(self, document: ExampleDocument) -> cabc.Iterable[DocumentationExample]:
        """Collect examples from ``document``."""


class ExampleEvaluator(t.Protocol):
    """Evaluator protocol for collected examples."""

    def evaluate(self, example: DocumentationExample) -> EvaluationResult:
        """Evaluate ``example`` and return the result."""


class _FunctionCollector:
    """Adapter for function-shaped collectors."""

    def __init__(
        self,
        *,
        name: str,
        suffixes: set[str] | frozenset[str],
        collect: t.Callable[[ExampleDocument], cabc.Iterable[DocumentationExample]],
    ) -> None:
        """Create a collector from a function."""
        self.name = name
        self.suffixes = frozenset(suffixes)
        self._collect = collect

    def collect(self, document: ExampleDocument) -> cabc.Iterable[DocumentationExample]:
        """Collect examples from ``document``."""
        return self._collect(document)


class DocumentationSuite:
    """Registry for collectors, evaluators, and pytest integration."""

    def __init__(
        self,
        *,
        project_root: pathlib.Path | None = None,
        encoding: str = "utf-8",
        include_paths: cabc.Iterable[pathlib.Path | str] = (),
        exclude_parts: cabc.Iterable[str] = (),
    ) -> None:
        """Create a documentation suite.

        Parameters
        ----------
        project_root : pathlib.Path | None
            Project root for path redaction and relative ids.
        encoding : str
            File encoding used during collection.
        include_paths : Iterable[pathlib.Path | str]
            Optional collection roots. Empty means any matching file is eligible.
        exclude_parts : Iterable[str]
            Path components that disable collection when present.
        """
        self.context = CollectionContext(
            project_root=(project_root or pathlib.Path.cwd()).expanduser().resolve(),
            encoding=encoding,
        )
        self.collectors: list[DocumentationCollector] = []
        self.evaluators: dict[str, ExampleEvaluator] = {}
        self.include_paths = tuple(self._resolve_include_path(path) for path in include_paths)
        self.exclude_parts = frozenset(exclude_parts)

    def register_collector(self, collector: DocumentationCollector) -> DocumentationSuite:
        """Register a class-based collector.

        Returns
        -------
        DocumentationSuite
            The suite, for fluent configuration.
        """
        self.collectors.append(collector)
        return self

    def register_function_collector(
        self,
        *,
        name: str,
        suffixes: set[str] | frozenset[str],
        collect: t.Callable[[ExampleDocument], cabc.Iterable[DocumentationExample]],
    ) -> DocumentationSuite:
        """Register a function-shaped collector.

        Returns
        -------
        DocumentationSuite
            The suite, for fluent configuration.
        """
        self.collectors.append(_FunctionCollector(name=name, suffixes=suffixes, collect=collect))
        return self

    def register_evaluator(self, language: str, evaluator: ExampleEvaluator) -> DocumentationSuite:
        """Register an evaluator for a language or kind.

        Returns
        -------
        DocumentationSuite
            The suite, for fluent configuration.
        """
        self.evaluators[language.lower()] = evaluator
        return self

    def collect(self, paths: cabc.Iterable[pathlib.Path | str]) -> list[DocumentationExample]:
        """Collect examples from files or directories.

        Parameters
        ----------
        paths : Iterable[pathlib.Path | str]
            Files or directories to scan.

        Returns
        -------
        list[DocumentationExample]
            Collected examples in filesystem order.
        """
        examples: list[DocumentationExample] = []
        for path in _iter_paths(paths):
            if self.should_collect_path(path):
                examples.extend(self.collect_file(path))
        return examples

    def collect_file(self, path: pathlib.Path) -> list[DocumentationExample]:
        """Collect examples from one file."""
        document = ExampleDocument(
            path=path,
            text=path.read_text(encoding=self.context.encoding),
            context=self.context,
        )
        examples: list[DocumentationExample] = []
        for collector in self.collectors:
            if path.suffix in collector.suffixes:
                examples.extend(collector.collect(document))
        return examples

    def should_collect_path(self, path: pathlib.Path) -> bool:
        """Return whether ``path`` is eligible for collection."""
        resolved = path.resolve()
        if self.exclude_parts.intersection(resolved.parts):
            return False
        if not any(path.suffix in collector.suffixes for collector in self.collectors):
            return False
        if not self.include_paths:
            return True
        return any(_is_relative_to(resolved, include_path) for include_path in self.include_paths)

    def evaluator_for(self, example: DocumentationExample) -> ExampleEvaluator | None:
        """Return the evaluator for ``example`` if one is registered."""
        return self.evaluators.get(example.language.lower()) or self.evaluators.get(
            example.kind.lower(),
        )

    def pytest_collect_file(
        self,
        file_path: pathlib.Path,
        parent: pytest.Collector,
    ) -> pytest.File | None:
        """Pytest ``pytest_collect_file`` hook implementation."""
        from .plugin import DocumentationFile

        if self.should_collect_path(file_path):
            return DocumentationFile.from_parent(parent, path=file_path, suite=self)
        return None

    def _resolve_include_path(self, path: pathlib.Path | str) -> pathlib.Path:
        """Resolve one include path relative to the project root."""
        include_path = pathlib.Path(path)
        if include_path.is_absolute():
            return include_path.resolve()
        return (self.context.project_root / include_path).resolve()


def collect_examples(
    paths: cabc.Iterable[pathlib.Path | str],
    *,
    collectors: cabc.Iterable[DocumentationCollector],
    project_root: pathlib.Path | None = None,
    encoding: str = "utf-8",
) -> list[DocumentationExample]:
    """Collect examples with a temporary suite.

    Parameters
    ----------
    paths : Iterable[pathlib.Path | str]
        Files or directories to scan.
    collectors : Iterable[DocumentationCollector]
        Collectors to apply.
    project_root : pathlib.Path | None
        Root for relative display paths.
    encoding : str
        File encoding.

    Returns
    -------
    list[DocumentationExample]
        Collected examples.
    """
    suite = DocumentationSuite(project_root=project_root, encoding=encoding)
    for collector in collectors:
        suite.register_collector(collector)
    return suite.collect(paths)


def failure_from_exception(
    example: DocumentationExample,
    exception: BaseException,
    *,
    project_root: pathlib.Path | None = None,
) -> EvaluationResult:
    """Create an evaluation failure result from an exception.

    Parameters
    ----------
    example : DocumentationExample
        Example whose evaluation raised.
    exception : BaseException
        Exception converted into a harness-error failure.
    project_root : pathlib.Path | None
        Root stripped from paths in the redacted traceback message.

    Returns
    -------
    EvaluationResult
        Failed result carrying the redacted traceback.
    """
    message = "".join(traceback.format_exception(exception))
    return EvaluationResult.failed_result(
        example,
        failure_kind=EvaluationFailureKind.HARNESS_ERROR,
        message=redact_text(message, project_root=project_root),
    )


def blocked_command_error(command: str) -> subprocess.SubprocessError:
    """Return a standard blocked-command exception."""
    return subprocess.SubprocessError(f"blocked command: {command}")


def _iter_paths(paths: cabc.Iterable[pathlib.Path | str]) -> cabc.Iterator[pathlib.Path]:
    """Yield files under ``paths`` in deterministic order."""
    for raw_path in paths:
        path = pathlib.Path(raw_path)
        if path.is_file():
            yield path
        elif path.is_dir():
            yield from sorted(child for child in path.rglob("*") if child.is_file())
        else:
            message = f"not a file or directory: {path}"
            raise ValueError(message)


def _is_relative_to(path: pathlib.Path, root: pathlib.Path) -> bool:
    """Compatibility wrapper for ``Path.is_relative_to``."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
