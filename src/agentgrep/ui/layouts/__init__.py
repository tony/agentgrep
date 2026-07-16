"""Pluggable explorer layouts (ADR 0013).

A *layout* is the structure axis of the TUI: a :class:`~agentgrep.ui.layouts._base.LayoutScreen`
(a Textual ``Screen``) that owns its ``compose``, CSS, bindings, and how it
presents streamed records. The App shell mounts one layout as the default and can
be constructed with another for tests or embedding; each app mounts one layout
for its lifetime. Every layout shares the engine seam through the injected
:class:`~agentgrep.ui._context.UiContext`.

Layouts import Textual at module scope, so they are reached only from inside the
app factory (and the tests), never by the eager ``import agentgrep`` path
(ADR 0010).
"""

from __future__ import annotations

import logging

from agentgrep.ui.layouts._base import LayoutScreen

logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = ["LayoutScreen"]
