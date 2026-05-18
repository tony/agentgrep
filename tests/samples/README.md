# Store fixture samples

One representative record (or short sequence of records) per primary
``store_id`` declared in :mod:`agentgrep.store_catalog`. All content is
synthetic — these are *structural* fixtures, modeled on the real schemas but
containing no real user prompts, paths, or identifiers.

Future adapter PRs (F1, F2 in the plan) consume these via
:func:`tests.conftest.fixture_path` and lock parser output under syrupy
snapshots. The fixtures themselves are also linted by
``tests/test_stores.py::test_fixture_samples_are_well_formed``.

Layout
------

::

    tests/samples/<agent>/<store_id>/<filename>

Filenames mirror the real store naming where helpful — e.g.
``session-2026-05-17T00-00-00.jsonl`` for Gemini, ``rollout-...jsonl`` for
Codex — but the timestamps and UUIDs inside are placeholders.
