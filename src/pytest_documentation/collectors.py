"""Built-in documentation example collectors."""

from __future__ import annotations

import ast
import dataclasses
import fnmatch
import re
import shlex
import textwrap
import typing as t

from .core import DocumentationExample, ExampleDocument, ExampleLocation, redact_path

_FENCE_RE = re.compile(
    r"^(?P<prefix>(?:[ \t]*>[ \t]*)*)(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})(?P<info>[^\r\n]*)",
)
_DOCSTRING_PUNCTUATION_RE = re.compile(r"[rRuUbBfF]*(?P<quote>'''|\"\"\"|'|\")")
_MYST_CODE_DIRECTIVES = frozenset({"code", "code-block", "code-cell", "sourcecode"})


@dataclasses.dataclass(frozen=True, slots=True)
class _FenceInfo:
    """Parsed Markdown fence info string."""

    language: str
    tags: frozenset[str]
    settings: dict[str, str]
    group: str
    strip_directive_options: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class _OpenFence:
    """Absolute-position metadata for an open Markdown fence."""

    prefix: str
    indent: str
    fence: str
    info: str
    content_start: int
    line_number: int


@dataclasses.dataclass(frozen=True, slots=True)
class _NormalizedLine:
    """One source line after Markdown quote/list prefix removal."""

    text: str
    line_offset: int
    removed_prefix_length: int
    end_offset: int


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
            strip_myst_options=info.strip_directive_options,
        )
        line_starts = _line_starts(document.text)
        start_line = _line_number(line_starts, start_index) if source else opening.line_number + 1
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


class MarkdownPythonPageCollector:
    """Collect page-level Python narratives from Markdown/MyST documents."""

    name = "markdown-python-page"
    suffixes = frozenset({".md", ".mdx"})
    _DEFAULT_INCLUDE_GLOBS = ("README.md", "docs/library/*.md")

    def __init__(
        self,
        *,
        include_globs: t.Iterable[str] = _DEFAULT_INCLUDE_GLOBS,
        languages: set[str] | frozenset[str] | None = None,
    ) -> None:
        """Create a page-level Python collector.

        Parameters
        ----------
        include_globs : Iterable[str]
            Project-relative glob patterns eligible for page-level execution.
        languages : set[str] | frozenset[str] | None
            Fence languages treated as Python. ``None`` uses ``python`` and
            ``py``.
        """
        self.include_globs = tuple(include_globs)
        python_languages = {"python", "py"} if languages is None else languages
        self._markdown = MarkdownFenceCollector(languages=python_languages)

    def collect(self, document: ExampleDocument) -> t.Iterable[DocumentationExample]:
        """Collect one combined Python page example from ``document``."""
        if not _matches_include_globs(document.display_path, self.include_globs):
            return
        examples = list(self._markdown.collect(document))
        if not examples:
            return
        first = examples[0]
        last = examples[-1]
        source = _combine_python_page_source(examples)
        location = ExampleLocation(
            path=document.path,
            display_path=document.display_path,
            start_line=first.location.start_line,
            end_line=last.location.end_line,
            start_index=first.location.start_index,
            end_index=last.location.end_index,
        )
        yield DocumentationExample(
            kind="code",
            language="python-page",
            source=source,
            raw_source=source,
            location=location,
            test_id=f"{document.display_path}:python-page",
        )


class PythonDocstringCollector:
    """Collect Markdown fences from Python docstrings using AST locations."""

    name = "python-docstring"
    suffixes = frozenset({".py"})

    def __init__(self, *, languages: set[str] | frozenset[str] | None = None) -> None:
        """Create a Python docstring collector.

        Parameters
        ----------
        languages : set[str] | frozenset[str] | None
            Optional lower-case language allowlist. ``None`` collects every
            fence.
        """
        self._markdown = MarkdownFenceCollector(languages=languages)

    def collect(self, document: ExampleDocument) -> t.Iterable[DocumentationExample]:
        """Collect Markdown examples from Python docstring ranges."""
        for start, end in _docstring_ranges(document.text):
            yield from self._markdown.collect_range(document, start, end)


class FastMCPConfigCollector:
    """Collect ``fastmcp.json`` files as documentation examples."""

    name = "fastmcp-config"
    suffixes = frozenset({".json"})

    def collect(self, document: ExampleDocument) -> t.Iterable[DocumentationExample]:
        """Collect one example from a FastMCP config file."""
        if "fastmcp.json" not in document.path.name:
            return
        location = ExampleLocation(
            path=document.path,
            display_path=redact_path(document.path, project_root=document.context.project_root),
            start_line=1,
            end_line=max(document.text.count("\n"), 1),
            start_index=0,
            end_index=len(document.text),
            group="fastmcp.json",
        )
        yield DocumentationExample(
            kind="config",
            language="fastmcp-config",
            source=document.text,
            raw_source=document.text,
            location=location,
        )


class JustfileRecipeCollector:
    """Collect selected recipes from justfiles."""

    name = "justfile-recipe"
    suffixes = frozenset({""})

    def __init__(self, *, recipe_names: set[str] | frozenset[str] | None = None) -> None:
        """Create a justfile recipe collector.

        Parameters
        ----------
        recipe_names : set[str] | frozenset[str] | None
            Optional case-insensitive recipe allowlist. ``None`` collects
            every recipe.
        """
        self.recipe_names = (
            None if recipe_names is None else frozenset(name.lower() for name in recipe_names)
        )

    def collect(self, document: ExampleDocument) -> t.Iterable[DocumentationExample]:
        """Collect recipes from a justfile."""
        if document.path.name != "justfile":
            return
        lines = document.text.splitlines(keepends=True)
        line_starts = _line_starts(document.text)
        for line_index, line in enumerate(lines):
            recipe = _parse_recipe_header(line)
            if recipe is None:
                continue
            if self.recipe_names is not None and recipe.lower() not in self.recipe_names:
                continue
            start_index = line_starts[line_index]
            end_index = _recipe_end_index(lines, line_starts, line_index)
            source = document.text[start_index:end_index]
            location = ExampleLocation(
                path=document.path,
                display_path=redact_path(
                    document.path,
                    project_root=document.context.project_root,
                ),
                start_line=line_index + 1,
                end_line=_line_number(line_starts, max(end_index - 1, start_index)),
                start_index=start_index,
                end_index=end_index,
                group=recipe,
            )
            yield DocumentationExample(
                kind="recipe",
                language="just-recipe",
                source=source,
                raw_source=source,
                location=location,
                test_id=f"{location.display_path}:{location.start_line}:just:{recipe}",
            )


def _parse_info(info: str) -> _FenceInfo:
    """Parse a Markdown fence info string."""
    stripped = info.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        stripped = stripped[1:-1].strip()
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        tokens = stripped.split()
    strip_directive_options = False
    if tokens:
        directive = _myst_directive_name(tokens[0])
        if directive in _MYST_CODE_DIRECTIVES:
            strip_directive_options = True
            tokens = tokens[1:]
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
        strip_directive_options=strip_directive_options,
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
    strip_myst_options: bool = False,
) -> tuple[str, str, int, int]:
    """Strip Markdown quote/list prefixes and indentation from content lines."""
    strip_token = prefix + indent
    raw_lines = raw_content.splitlines(keepends=True)
    normalized_lines: list[_NormalizedLine] = []
    consumed = 0
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
        last_end = line_offset + len(raw_line)
        normalized_lines.append(
            _NormalizedLine(
                text=stripped_line,
                line_offset=line_offset,
                removed_prefix_length=removed,
                end_offset=last_end,
            ),
        )
        consumed += len(raw_line)
    if strip_myst_options:
        normalized_lines = _strip_myst_directive_options(normalized_lines)
    first_offset: int | None = None
    for line in normalized_lines:
        if line.text.strip():
            first_offset = line.line_offset + line.removed_prefix_length
            break
    raw_source = "".join(line.text for line in normalized_lines)
    source = textwrap.dedent(raw_source)
    if first_offset is None:
        first_offset = content_start
    if normalized_lines:
        line_delta = len(raw_source) - len(source)
        first_offset += max(line_delta, 0)
    last_end = normalized_lines[-1].end_offset if normalized_lines else content_start
    return raw_source, source, first_offset, last_end


def _myst_directive_name(token: str) -> str:
    """Return a MyST directive name from ``{directive}`` info tokens."""
    stripped = token.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return ""
    return stripped[1:-1].strip().lower()


def _strip_myst_directive_options(lines: list[_NormalizedLine]) -> list[_NormalizedLine]:
    """Drop leading MyST directive options and the blank separator that follows them."""
    index = 0
    stripped_option = False
    while index < len(lines) and _is_myst_directive_option(lines[index].text):
        index += 1
        stripped_option = True
    if stripped_option:
        while index < len(lines) and not lines[index].text.strip():
            index += 1
    return lines[index:]


def _is_myst_directive_option(line: str) -> bool:
    """Return whether ``line`` is a MyST directive option line."""
    stripped = line.strip()
    return stripped.startswith(":") and not stripped.startswith("::") and ":" in stripped[1:]


def _matches_include_globs(display_path: str, include_globs: t.Iterable[str]) -> bool:
    """Return whether a project-relative display path is included."""
    return any(fnmatch.fnmatchcase(display_path, pattern) for pattern in include_globs)


def _combine_python_page_source(examples: t.Sequence[DocumentationExample]) -> str:
    """Combine document-ordered Python fences while preserving source line numbers."""
    parts: list[str] = []
    current_line = 1
    for example in examples:
        gap = example.location.start_line - current_line
        if gap > 0:
            parts.append("\n" * gap)
        parts.append(example.source)
        current_line = example.location.end_line + 1
    return "".join(parts)


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


def _parse_recipe_header(line: str) -> str | None:
    """Return a just recipe name when ``line`` starts one."""
    if line.startswith((" ", "\t", "@", "#", "[")) or ":=" in line:
        return None
    match = re.match(r"(?P<name>[A-Za-z_][A-Za-z0-9_-]*)(?:\s+[^:]*)?:", line)
    if match is None:
        return None
    return match.group("name")


def _recipe_end_index(lines: list[str], line_starts: list[int], start_line: int) -> int:
    """Return the absolute end index for a just recipe body."""
    for line_index in range(start_line + 1, len(lines)):
        line = lines[line_index]
        if line.startswith("["):
            return line_starts[line_index]
        if (
            line.strip()
            and not line.startswith((" ", "\t", "#", "@", "["))
            and (_parse_recipe_header(line) is not None or ":=" in line)
        ):
            return line_starts[line_index]
    return sum(len(line) for line in lines)
