"""Structural typing shims for optional third-party surfaces.

Private module (the leading underscore marks it internal): these
``typing.Protocol`` definitions describe the minimal surfaces agentgrep needs
from pydantic, argparse help themes, Rich, and Textual so the engine and CLI
stay duck-typed and the pydantic-free fallback keeps working. They carry no
behavior.
"""

from __future__ import annotations

import typing as t

if t.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = [
    "HelpTheme",
    "PydanticModule",
    "PydanticTypeAdapter",
    "PydanticTypeAdapterFactory",
    "QueryAppLike",
    "RichTextModule",
    "RunnableAppLike",
    "SearchColors",
    "StaticLike",
    "StreamingAppLike",
    "TextualAppModule",
    "TextualBindingModule",
    "TextualContainersModule",
    "TextualMessageModule",
    "TextualOptionListInternalsModule",
    "TextualWidgetsModule",
]


class PydanticTypeAdapter(t.Protocol):
    """Minimal TypeAdapter surface used by ``agentgrep``."""

    def validate_python(self, value: object, /) -> object:
        """Validate a Python object."""
        ...

    def dump_python(self, value: object, /, *, mode: str = "python") -> object:
        """Dump a Python object."""
        ...


class PydanticTypeAdapterFactory(t.Protocol):
    """Factory for creating TypeAdapters."""

    def __call__(self, value_type: object, /) -> PydanticTypeAdapter:
        """Create a TypeAdapter."""
        ...


class PydanticModule(t.Protocol):
    """Minimal Pydantic module surface used at runtime."""

    TypeAdapter: PydanticTypeAdapterFactory


class HelpTheme(t.Protocol):
    """Minimal argparse help theme surface."""

    heading: str
    reset: str
    label: str
    long_option: str
    short_option: str
    prog: str
    action: str
    inline_code: str
    query_keyword: str
    query_operator: str
    query_field: str
    query_punct: str
    query_value: str
    query_wildcard: str
    query_negation: str


class SearchColors(t.Protocol):
    """Structural surface implemented by :class:`AnsiColors` (used by the CLI chrome)."""

    def success(self, text: str) -> str:
        """Style ``text`` as success."""
        ...

    def warning(self, text: str) -> str:
        """Style ``text`` as warning."""
        ...

    def error(self, text: str) -> str:
        """Style ``text`` as error."""
        ...

    def info(self, text: str) -> str:
        """Style ``text`` as informational."""
        ...

    def heading(self, text: str) -> str:
        """Style ``text`` as a status heading."""
        ...

    def highlight(self, text: str) -> str:
        """Style ``text`` as highlighted."""
        ...

    def muted(self, text: str) -> str:
        """Style ``text`` as muted."""
        ...

    def white(self, text: str) -> str:
        """Style ``text`` as plain white."""
        ...


class TextualContainersModule(t.Protocol):
    """Minimal Textual containers module surface."""

    Horizontal: cabc.Callable[..., t.ContextManager[object]]
    Vertical: cabc.Callable[..., t.ContextManager[object]]
    VerticalScroll: cabc.Callable[..., t.ContextManager[object]]


class TextualAppModule(t.Protocol):
    """Minimal Textual app module surface."""

    App: type[object]


class TextualMessageModule(t.Protocol):
    """Minimal Textual message module surface."""

    Message: type[object]


class RichTextModule(t.Protocol):
    """Minimal Rich text module surface."""

    Text: cabc.Callable[..., t.Any]


class StreamingAppLike(t.Protocol):
    """App methods needed by the streaming TUI: workers, timers, cross-thread calls."""

    def post_message(self, message: object) -> bool:
        """Post a message to the app's queue (thread-safe)."""
        ...

    def call_from_thread(
        self,
        callback: cabc.Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> object:
        """Invoke ``callback(*args, **kwargs)`` on the event loop from a worker thread.

        Bypasses the message queue, so high-frequency data updates don't
        starve keystroke and timer events.
        """
        ...

    def query_one(self, selector: object, expect_type: object | None = None) -> object:
        """Look up one widget."""
        ...

    def run_worker(
        self,
        work: cabc.Callable[..., object],
        *,
        name: str = ...,
        group: str = ...,
        description: str = ...,
        thread: bool = ...,
        exclusive: bool = ...,
    ) -> object:
        """Spawn a background worker."""
        ...

    def set_interval(
        self,
        interval: float,
        callback: cabc.Callable[[], object],
    ) -> object:
        """Register a recurring callback."""
        ...


class StaticLike(t.Protocol):
    """Minimal Static widget surface used by the TUI."""

    def update(self, content: str) -> None:
        """Update widget contents."""
        ...


class QueryAppLike(t.Protocol):
    """Minimal Textual app query surface used by the TUI."""

    def query_one(self, selector: object, expect_type: object | None = None) -> object:
        """Look up one widget."""
        ...


class RunnableAppLike(t.Protocol):
    """Minimal runnable app surface."""

    def run(self) -> None:
        """Run the application."""
        ...


class TextualWidgetsModule(t.Protocol):
    """Minimal Textual widgets module surface."""

    Footer: cabc.Callable[[], object]
    Header: cabc.Callable[[], object]
    Input: type[object]
    OptionList: type[object]
    Static: type[object]


class TextualOptionListInternalsModule(t.Protocol):
    """Minimal Textual option_list module surface for the ``Option`` class."""

    Option: t.Any


class TextualBindingModule(t.Protocol):
    """Minimal Textual binding module surface for the ``Binding`` class."""

    Binding: t.Any
