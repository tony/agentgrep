"""Deterministic Rich rendering for the default HUD detail body.

The module has no direct Textual dependency. Callers snapshot pump-owned query,
filter, theme, and width state into :class:`DetailRenderRequest`; the same
function then serves the bounded inline path and the off-pump worker path.
Scheduling, cache ownership, and widget mutation remain layout responsibilities.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass

from rich.console import Console
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.text import Text

from agentgrep._text import (
    DETAIL_BODY_MAX_CHARS,
    DETAIL_BODY_MAX_LINES,
    detect_content_format,
    find_first_match_line,
    looks_like_code,
    looks_like_markup,
    truncate_lines,
)
from agentgrep.ui import _streaming
from agentgrep.ui.highlighter import MarkupHighlighter

_DETAIL_RICH_FORMAT_MAX_CHARS = 2048
_CODE_GUESS_SAMPLE_BYTES = 4096
_CODE_GUESS_MIN_CONFIDENCE = 0.3


@dataclass(frozen=True, slots=True)
class DetailRenderRequest:
    """Immutable input snapshot for one detail-body render.

    Attributes
    ----------
    body_text : str
        Body already bounded by the layout's detail truncation limits.
    query_terms : tuple[str, ...]
        Literal query terms eligible for decorative highlighting.
    case_sensitive : bool
        Whether query-term highlighting preserves case.
    regex : bool
        Whether query terms are regex expressions and must not be re-run for
        decorative highlighting.
    filter_terms : tuple[str, ...]
        Literal filter terms highlighted case-insensitively.
    search_style : str
        Resolved Rich style for query-term matches.
    filter_style : str
        Resolved Rich style for filter-term matches.
    syntax_theme : str
        Resolved Pygments theme for JSON, code, and Markdown fences.
    render_width : int
        Detail body width used to flatten Markdown and syntax-highlighted code.
    guess_code : bool
        Whether to run the worker-only Pygments language guess.
    """

    body_text: str
    query_terms: tuple[str, ...]
    case_sensitive: bool
    regex: bool
    filter_terms: tuple[str, ...]
    search_style: str
    filter_style: str
    syntax_theme: str
    render_width: int
    guess_code: bool = False


@dataclass(frozen=True, slots=True)
class DetailRenderResult:
    """Rendered body and the text projections used by find and copy.

    The dataclass is frozen, but Rich renderables are mutable. The receiving
    layout owns ``renderable`` after publication and must copy it before adding
    transient overlays.

    Attributes
    ----------
    renderable : rich.text.Text | rich.syntax.Syntax
        Styled body painted by the detail widget.
    find_source : str
        Text whose offsets align with the displayed body for find-in-detail.
    rendered_plain : str
        Flattened text copied by the rendered-copy action.
    """

    renderable: Text | Syntax
    find_source: str
    rendered_plain: str


def apply_filter_highlight(
    text: Text,
    *,
    terms: tuple[str, ...],
    style: str,
) -> None:
    """Overlay bounded, case-insensitive filter highlights.

    Parameters
    ----------
    text : rich.text.Text
        Caller-owned text to mutate.
    terms : tuple[str, ...]
        Literal filter terms.
    style : str
        Resolved Rich style for every match.
    """
    _streaming._apply_bounded_literal_highlights(
        text,
        text.plain,
        terms,
        case_sensitive=False,
        style=style,
    )


def _guess_code_lexer(body_text: str) -> str | None:
    """Return a Pygments lexer alias for confidently detected code.

    Parameters
    ----------
    body_text : str
        Bounded body sampled for language detection.

    Returns
    -------
    str | None
        The first lexer alias, or ``None`` when confidence is too low.
    """
    from pygments.lexers import guess_lexer
    from pygments.lexers.special import TextLexer
    from pygments.util import ClassNotFound

    sample = body_text[:_CODE_GUESS_SAMPLE_BYTES]
    with contextlib.suppress(ClassNotFound):
        lexer = guess_lexer(sample)
        if (
            not isinstance(lexer, TextLexer)
            and lexer.aliases
            and lexer.analyse_text(sample) >= _CODE_GUESS_MIN_CONFIDENCE
        ):
            return lexer.aliases[0]
    return None


def _flatten_syntax(
    body_text: str,
    *,
    render_width: int,
    lexer: str,
    theme: str,
) -> Text:
    """Render code into selectable styled text.

    Parameters
    ----------
    body_text : str
        Source body.
    render_width : int
        Width used for word wrapping.
    lexer : str
        Pygments lexer alias.
    theme : str
        Pygments theme name.

    Returns
    -------
    rich.text.Text
        Flattened visible code and syntax styles.
    """
    console = Console(
        width=max(1, render_width),
        color_system="truecolor",
        force_terminal=False,
        highlight=False,
        markup=False,
        emoji=False,
    )
    syntax = Syntax(
        body_text,
        lexer,
        theme=theme,
        word_wrap=True,
        background_color="default",
    )
    styled = Text(no_wrap=False)
    for line in console.render_lines(syntax, pad=False):
        for segment in line:
            if segment.control or not segment.text:
                continue
            styled.append(segment.text, segment.style)
        styled.append("\n")
    return styled


def _flatten_markdown(
    body_text: str,
    *,
    render_width: int,
    code_theme: str,
) -> Text:
    """Render Markdown into selectable styled text.

    Parameters
    ----------
    body_text : str
        Markdown source.
    render_width : int
        Width used for layout and wrapping.
    code_theme : str
        Pygments theme for fenced code.

    Returns
    -------
    rich.text.Text
        Flattened visible Markdown and Rich styles.
    """
    console = Console(
        width=max(1, render_width),
        color_system="truecolor",
        force_terminal=False,
        highlight=False,
        markup=False,
        emoji=False,
    )
    markdown = Markdown(body_text, code_theme=code_theme)
    styled = Text(no_wrap=False)
    for line in console.render_lines(markdown, pad=False):
        for segment in line:
            if segment.control or not segment.text:
                continue
            styled.append(segment.text, segment.style)
        styled.append("\n")
    return styled


def build_detail_body(request: DetailRenderRequest) -> DetailRenderResult:
    """Build a detail body solely from an immutable render snapshot.

    ``guess_code=True`` is reserved for the layout's worker path because
    Pygments language detection and syntax flattening are not pump-cheap.

    Parameters
    ----------
    request : DetailRenderRequest
        Complete body, query, filter, style, theme, and width snapshot.

    Returns
    -------
    DetailRenderResult
        Styled body plus find and rendered-copy text.

    Examples
    --------
    >>> request = DetailRenderRequest(
    ...     body_text="alpha",
    ...     query_terms=(),
    ...     case_sensitive=False,
    ...     regex=False,
    ...     filter_terms=(),
    ...     search_style="bold yellow",
    ...     filter_style="bold black on cyan",
    ...     syntax_theme="ansi_dark",
    ...     render_width=80,
    ... )
    >>> result = build_detail_body(request)
    >>> result.find_source, result.rendered_plain
    ('alpha', 'alpha')
    """
    safe_query_terms = (
        ()
        if request.regex
        else _streaming._bounded_literal_terms(
            request.query_terms,
            case_sensitive=request.case_sensitive,
        )
    )
    body_text = request.body_text
    fmt = detect_content_format(body_text)
    code_body: Text | None = None
    if request.guess_code and looks_like_code(body_text):
        lexer = _guess_code_lexer(body_text)
        if lexer is not None:
            code_body = _flatten_syntax(
                body_text,
                render_width=request.render_width,
                lexer=lexer,
                theme=request.syntax_theme,
            )
    if code_body is not None:
        _streaming._apply_bounded_literal_highlights(
            code_body,
            code_body.plain,
            safe_query_terms,
            case_sensitive=request.case_sensitive,
            style=request.search_style,
        )
        apply_filter_highlight(
            code_body,
            terms=request.filter_terms,
            style=request.filter_style,
        )
        return DetailRenderResult(code_body, body_text, code_body.plain)
    if fmt == "json":
        formatted = body_text
        if _streaming._json_pretty_print_is_bounded(body_text):
            with contextlib.suppress(RecursionError, ValueError):
                formatted = json.dumps(
                    json.loads(body_text),
                    indent=2,
                    ensure_ascii=False,
                )
        formatted = truncate_lines(
            formatted,
            DETAIL_BODY_MAX_LINES,
            max_chars=DETAIL_BODY_MAX_CHARS,
        )
        match_line = find_first_match_line(
            formatted,
            safe_query_terms,
            case_sensitive=request.case_sensitive,
            regex=False,
        )
        highlight_lines = {match_line + 1} if match_line is not None else None
        if len(formatted) <= _DETAIL_RICH_FORMAT_MAX_CHARS:
            renderable: Text | Syntax = Syntax(
                formatted,
                "json",
                theme=request.syntax_theme,
                word_wrap=True,
                highlight_lines=highlight_lines,
            )
        else:
            plain = Text(formatted, no_wrap=False)
            _streaming._apply_bounded_literal_highlights(
                plain,
                formatted,
                safe_query_terms,
                case_sensitive=request.case_sensitive,
                style=request.search_style,
            )
            apply_filter_highlight(
                plain,
                terms=request.filter_terms,
                style=request.filter_style,
            )
            renderable = plain
        return DetailRenderResult(renderable, formatted, formatted)
    if fmt == "markdown":
        styled = _flatten_markdown(
            body_text,
            render_width=request.render_width,
            code_theme=request.syntax_theme,
        )
        _streaming._apply_bounded_literal_highlights(
            styled,
            styled.plain,
            safe_query_terms,
            case_sensitive=request.case_sensitive,
            style=request.search_style,
        )
        apply_filter_highlight(
            styled,
            terms=request.filter_terms,
            style=request.filter_style,
        )
        return DetailRenderResult(styled, body_text, styled.plain)
    highlighted = Text(body_text, no_wrap=False)
    _streaming._apply_bounded_literal_highlights(
        highlighted,
        body_text,
        safe_query_terms,
        case_sensitive=request.case_sensitive,
        style=request.search_style,
    )
    apply_filter_highlight(
        highlighted,
        terms=request.filter_terms,
        style=request.filter_style,
    )
    if looks_like_markup(body_text):
        MarkupHighlighter(dark=request.syntax_theme != "ansi_light").highlight(highlighted)
    return DetailRenderResult(highlighted, body_text, body_text)
