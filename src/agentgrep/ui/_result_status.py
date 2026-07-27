"""Textual-free human copy derived from engine-owned search summaries."""

from __future__ import annotations

from agentgrep.progress import format_match_count
from agentgrep.results import NextAction, RunSummary

__all__ = [
    "format_depth_offer_lead",
    "format_depth_offer_rows",
    "format_empty_evidence",
    "format_empty_outcome",
    "format_next_action_hint",
    "format_run_status",
]


_EMPTY_OUTCOME_LABELS = {
    "no_prompt_match": "No prompt matches",
    "no_candidate_conversation": "No candidate conversations",
    "no_selected_conversation_match": "No matches in selected conversations",
    "no_exhaustive_match": "No matches in readable conversations",
    "undetermined": "Search incomplete",
}

#: What each empty outcome actually proves. Every line names the surface the
#: engine read, so a shallow miss is never presented as a corpus-wide negative.
_EMPTY_OUTCOME_EVIDENCE = {
    "no_prompt_match": "Prompt history only; conversation bodies were not read",
    "no_candidate_conversation": "Prompt evidence selected no conversation to read",
    "no_selected_conversation_match": (
        "Only the conversations selected from prompt evidence were read"
    ),
    "no_exhaustive_match": "Every readable conversation was read",
    "undetermined": "Coverage is incomplete, so this is not a negative result",
}

_ACTION_HINTS = {
    "search.targeted": "/deep selected conversations",
    "search.exhaustive": "/exhaustive all conversations",
}
_SCOPE_CHANGE_HINT = "change the explicit scope to all before searching conversations"

#: What each offered depth action would read, in the same vocabulary the empty
#: outcomes use. Keyed by the engine's stable ``action_id``.
_DEPTH_OFFER_HINTS = {
    "search.targeted": "read the conversations selected from prompt evidence",
    "search.exhaustive": "read every readable conversation",
}

_DEPTH_OFFER_LEADS = {
    "search.targeted": "Enter searches prompt history; conversation bodies are not read.",
    "search.exhaustive": "Deep search can omit a conversation.",
}
_DEPTH_OFFER_CONFIRMATION_LEAD = "Change the explicit scope to all before searching conversations."


def format_empty_outcome(summary: RunSummary) -> str:
    """Return concise empty-state copy grounded in terminal engine evidence."""
    return _EMPTY_OUTCOME_LABELS.get(summary.outcome, "No matches")


def format_empty_evidence(summary: RunSummary) -> str:
    """Return the surface an empty run actually read, or ``""`` when unknown.

    The label from :func:`format_empty_outcome` names the outcome; this names
    the evidence behind it. Pairing them keeps a shallow miss legible as "this
    rung found nothing" rather than "your history does not contain this".

    Parameters
    ----------
    summary : RunSummary
        Terminal engine evidence for one finished run.

    Returns
    -------
    str
        One sentence-fragment without trailing punctuation, or ``""`` when the
        outcome carries matches and needs no coverage caveat.
    """
    return _EMPTY_OUTCOME_EVIDENCE.get(summary.outcome, "")


def format_depth_offer_lead(actions: tuple[NextAction, ...]) -> str:
    """Return the pre-run sentence naming what the current rung will read.

    Parameters
    ----------
    actions : tuple[NextAction, ...]
        Engine-authored depth escalations from
        :func:`~agentgrep.results.offered_depth_actions`.

    Returns
    -------
    str
        One sentence, or ``""`` when the engine offers nothing to escalate to.
    """
    if not actions:
        return ""
    if any(action.requires_confirmation for action in actions):
        return _DEPTH_OFFER_CONFIRMATION_LEAD
    return _DEPTH_OFFER_LEADS.get(actions[0].action_id, "")


def format_depth_offer_rows(
    actions: tuple[NextAction, ...],
) -> tuple[tuple[str, str], ...]:
    """Return selectable ``(action_id, row text)`` pairs for a pre-run offer.

    An offer that broadens an explicitly selected scope is not selectable, so
    it yields no rows; :func:`format_depth_offer_lead` carries the guidance
    instead. This keeps the explicit-scope confirmation contract identical
    before and after a run.

    Parameters
    ----------
    actions : tuple[NextAction, ...]
        Engine-authored depth escalations from
        :func:`~agentgrep.results.offered_depth_actions`.

    Returns
    -------
    tuple[tuple[str, str], ...]
        One pair per selectable escalation, cheapest first.
    """
    if any(action.requires_confirmation for action in actions):
        return ()
    rows: list[tuple[str, str]] = []
    for action in actions:
        hint = _DEPTH_OFFER_HINTS.get(action.action_id)
        if hint is None:
            continue
        rows.append((action.action_id, f"{action.label} — {hint}"))
    return tuple(rows)


def format_next_action_hint(summary: RunSummary) -> str:
    """Return slash-command guidance for engine-authored follow-up actions."""
    choices = tuple(
        dict.fromkeys(
            (
                _SCOPE_CHANGE_HINT
                if action.requires_confirmation
                else _ACTION_HINTS[action.action_id]
            )
            for action in summary.next_actions
            if action.action_id in _ACTION_HINTS
        ),
    )
    return f"Next: {' · '.join(choices)}" if choices else ""


def format_run_status(summary: RunSummary) -> str:
    """Return one compact terminal status with effort-specific coverage."""
    if summary.status.state == "failed":
        return "Search incomplete"
    matches = format_match_count(summary.match_count)
    if summary.status.state == "cancelled" or summary.status.reason == "answer_now":
        return f"Stopped at {matches}"
    coverage = summary.coverage
    if summary.requested_effort == "prompt":
        return f"Prompt search: {matches}; conversation bodies not read"
    if summary.requested_effort == "targeted":
        return (
            f"Targeted search: {matches}; "
            f"{coverage.conversations_completed}/{coverage.conversations_selected} "
            "selected conversations read"
        )
    return (
        f"Exhaustive search: {matches}; "
        f"{coverage.sources_completed}/{coverage.sources_planned} sources completed"
    )
