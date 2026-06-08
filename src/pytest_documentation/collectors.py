"""Built-in documentation example collectors."""

from __future__ import annotations

import ast
import dataclasses
import re
import shlex
import textwrap
import typing as t

from .core import DocumentationExample, ExampleDocument, ExampleLocation, redact_path

_FENCE_RE = re.compile(
    r"^(?P<prefix>(?:[ \t]*>[ \t]*)*)(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})(?P<info>[^\r\n]*)",
)
_DOCSTRING_PUNCTUATION_RE = re.compile(r"[rRuUbBfF]*(?P<quote>'''|\"\"\"|'|\")")


@dataclasses.dataclass(frozen=True, slots=True)
class _FenceInfo:
    """Parsed Markdown fence info string."""

    language: str
    tags: frozenset[str]
    settings: dict[str, str]
    group: str


@dataclasses.dataclass(frozen=True, slots=True)
class _OpenFence:
    """Absolute-position metadata for an open Markdown fence."""

    prefix: str
    indent: str
    fence: str
    info: str
    content_start: int
    line_number: int


class MarkdownFenceCollector:
    """Collect fenced Markdown/MyST code examples."""

    name = "markdown-fence"
    suffixes = frozenset({".md", ".mdx"})

    def __init__(self, *, languages: set[str] | frozenset[str] | None = None) -> None:
        """Create a Markdown fence collector.

        Parameters
        ----------
        languages : set[str] | frozenset[str] | None
            Optional lower-case language allowlist. ``None`` collects every fence.
        """
        if languages is None:
            self.languages = None
        else:
            self.languages = frozenset(language.lower() for language in languages)

    def collect(self, document: ExampleDocument) -> t.Iterable[DocumentationExample]:
        """Collect examples from ``document``."""
        yield from self.collect_range(document, 0, len(document.text))

    def collect_range(
        self,
        document: ExampleDocument,
        start_index: int,
        end_index: int,
    ) -> t.Iterable[DocumentationExample]:
        """Collect examples from an index range within ``document``."""
        text = document.text
        line_starts = _line_starts(text)
        open_fence: _OpenFence | None = None
        index = start_index
        while index < end_index:
            line_end = text.find("\n", index, end_index)
            if line_end == -1:
                line_end = end_index
                next_index = end_index
            else:
                next_index = line_end + 1
            line = text[index:line_end]
            match = _FENCE_RE.match(line)
            if match is not None:
                if open_fence is None:
                    if not self._language_allowed(match.group("info")):
                        index = next_index
                        continue
                    open_fence = _OpenFence(
                        prefix=match.group("prefix"),
                        indent=match.group("indent"),
                        fence=match.group("fence"),
                        info=match.group("info"),
                        content_start=next_index,
                        line_number=_line_number(line_starts, index),
                    )
                elif _closes_fence(match, open_fence):
                    yield from self._make_example(
                        document=document,
                        opening=open_fence,
                        closing_start=index,
                    )
                    open_fence = None
            index = next_index
        if open_fence is not None:
            location = f"{document.display_path}:{open_fence.line_number}"
            message = f"unclosed Markdown fence at {location}"
            raise ValueError(message)

    def _language_allowed(self, info: str) -> bool:
        """Return whether a fence info string should be collected."""
        if self.languages is None:
            return True
        parsed = _parse_info(info)
        return parsed.language.lower() in self.languages

    def _make_example(
        self,
        *,
        document: ExampleDocument,
        opening: _OpenFence,
        closing_start: int,
    ) -> t.Iterable[DocumentationExample]:
        """Build an example from an opening and closing fence."""
        info = _parse_info(opening.info)
        if self.languages is not None and info.language.lower() not in self.languages:
            return
        prefix = opening.prefix
        indent = opening.indent
        raw_content = document.text[opening.content_start : closing_start]
        raw_source, source, start_index, end_index = _strip_line_prefixes(
            raw_content,
            content_start=opening.content_start,
            prefix=prefix,
            indent=indent,
        )
        start_line = opening.line_number + 1
        line_count = source.count("\n")
        end_line = start_line + line_count - 1 if source else start_line - 1
        location = ExampleLocation(
            path=document.path,
            display_path=redact_path(document.path, project_root=document.context.project_root),
            start_line=start_line,
            end_line=end_line,
            start_index=start_index,
            end_index=end_index,
            prefix=prefix,
            indent=indent,
            group=info.group,
        )
        yield DocumentationExample(
            kind="code",
            language=info.language,
            source=source,
            raw_source=raw_source,
            location=location,
            tags=info.tags,
            settings=info.settings,
        )


class PythonDocstringCollector:
    """Collect Markdown fences from Python docstrings using AST locations."""

    name = "python-docstring"
    suffixes = frozenset({".py"})

    def __init__(self, *, languages: set[str] | frozenset[str] | None = None) -> None:
        """Create a Python docstring collector."""
        self._markdown = MarkdownFenceCollector(languages=languages)

    def collect(self, document: ExampleDocument) -> t.Iterable[DocumentationExample]:
        """Collect Markdown examples from Python docstring ranges."""
        for start, end in _docstring_ranges(document.text):
            yield from self._markdown.collect_range(document, start, end)


def _parse_info(info: str) -> _FenceInfo:
    """Parse a Markdown fence info string."""
    stripped = info.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        stripped = stripped[1:-1].strip()
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        tokens = stripped.split()
    language = ""
    tags: set[str] = set()
    settings: dict[str, str] = {}
    for token in tokens:
        if "=" in token and not token.startswith("="):
            key, value = token.split("=", 1)
            settings[key.lstrip(".")] = value
        elif token.startswith("."):
            tag = token[1:]
            tags.add(tag)
            if not language:
                language = tag
        elif not language:
            language = token
        else:
            tags.add(token.lstrip("."))
    group = settings.get("group", "")
    return _FenceInfo(
        language=language.lower(),
        tags=frozenset(tags),
        settings=settings,
        group=group,
    )


def _closes_fence(match: re.Match[str], opening: _OpenFence) -> bool:
    """Return whether ``match`` closes ``opening``."""
    current = match.group("fence")
    original = opening.fence
    return (
        current[0] == original[0]
        and len(current) >= len(original)
        and match.group("prefix") == opening.prefix
        and not match.group("info").strip()
    )


def _strip_line_prefixes(
    raw_content: str,
    *,
    content_start: int,
    prefix: str,
    indent: str,
) -> tuple[str, str, int, int]:
    """Strip Markdown quote/list prefixes and indentation from content lines."""
    strip_token = prefix + indent
    raw_lines = raw_content.splitlines(keepends=True)
    normalized_lines: list[str] = []
    first_offset: int | None = None
    consumed = 0
    last_end = content_start
    for raw_line in raw_lines:
        line_offset = content_start + consumed
        stripped_line = raw_line
        removed = 0
        if strip_token and raw_line.startswith(strip_token):
            stripped_line = raw_line[len(strip_token) :]
            removed = len(strip_token)
        elif prefix and raw_line.startswith(prefix):
            stripped_line = raw_line[len(prefix) :]
            removed = len(prefix)
        normalized_lines.append(stripped_line)
        if first_offset is None and stripped_line.strip():
            first_offset = line_offset + removed
        last_end = line_offset + len(raw_line)
        consumed += len(raw_line)
    raw_source = "".join(normalized_lines)
    source = textwrap.dedent(raw_source)
    if first_offset is None:
        first_offset = content_start
    if normalized_lines:
        line_delta = len(raw_source) - len(source)
        first_offset += max(line_delta, 0)
    return raw_source, source, first_offset, last_end


def _line_starts(text: str) -> list[int]:
    """Return 0-based line start offsets for ``text``."""
    starts = [0]
    for index, character in enumerate(text):
        if character == "\n":
            starts.append(index + 1)
    return starts


def _line_number(line_starts: list[int], index: int) -> int:
    """Return 1-based line number for ``index``."""
    low = 0
    high = len(line_starts)
    while low < high:
        middle = (low + high) // 2
        if line_starts[middle] <= index:
            low = middle + 1
        else:
            high = middle
    return low


def _docstring_ranges(source: str) -> t.Iterator[tuple[int, int]]:
    """Yield source ranges for Python docstring bodies."""
    line_starts = _line_starts(source)
    module = ast.parse(source)
    for node in ast.walk(module):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if not isinstance(first, ast.Expr) or not isinstance(first.value, ast.Constant):
            continue
        if not isinstance(first.value.value, str):
            continue
        start = line_starts[first.value.lineno - 1] + first.value.col_offset
        end_lineno = first.value.end_lineno
        end_col_offset = first.value.end_col_offset
        if end_lineno is None or end_col_offset is None:
            continue
        end = line_starts[end_lineno - 1] + end_col_offset
        match = _DOCSTRING_PUNCTUATION_RE.match(source, start, end)
        if match is None:
            continue
        quote = match.group("quote")
        yield match.end(), end - len(quote)
