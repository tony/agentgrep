"""Documentation contract tests for deterministic record identity."""

from __future__ import annotations

import pathlib
import re
import typing as t

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class DocumentationContractCase(t.NamedTuple):
    """Required terms for one public identity-documentation surface."""

    test_id: str
    relative_path: str
    required: tuple[str, ...]


SURFACE_CONTRACT_CASES: tuple[DocumentationContractCase, ...] = (
    DocumentationContractCase(
        test_id="cli",
        relative_path="docs/cli/search.md",
        required=(
            "adr-deterministic-record-identity",
            "`Record:`",
            "`Content:`",
            "`Thread:`",
            "`content_id`",
            "`record_id`",
            "`record_id_stability`",
            "`thread_id`",
            "JSON",
            "NDJSON",
            "em dash",
            "not resolvers",
        ),
    ),
    DocumentationContractCase(
        test_id="mcp",
        relative_path="docs/mcp/tools.md",
        required=(
            "adr-deterministic-record-identity",
            "`ref`",
            "`content_id`",
            "`record_id`",
            "`record_id_stability`",
            "`thread_id`",
            "`inspect_result`",
            "only `ref`",
        ),
    ),
    DocumentationContractCase(
        test_id="tui",
        relative_path="docs/tui/index.md",
        required=(
            "adr-deterministic-record-identity",
            "`Adapter:`",
            "`Record:`",
            "`Content:`",
            "`Thread:`",
            "`…`",
            "`—`",
            "compact",
            "greplog",
            "status",
        ),
    ),
)


def _read_text(relative_path: str) -> str:
    """Return one tracked documentation file's text."""
    path = _REPO_ROOT / relative_path
    assert path.is_file(), f"missing documentation contract: {relative_path}"
    return path.read_text(encoding="utf-8")


def test_identity_adr_is_indexed_with_stable_label() -> None:
    """The ADR index and document expose one stable cross-reference target."""
    index = _read_text("docs/dev/adr/index.md")
    adr = _read_text("docs/dev/adr/0015-deterministic-record-identity.md")

    assert "0015-deterministic-record-identity" in index
    assert "(adr-deterministic-record-identity)=" in adr


@pytest.mark.parametrize(
    "case",
    SURFACE_CONTRACT_CASES,
    ids=[case.test_id for case in SURFACE_CONTRACT_CASES],
)
def test_identity_surface_docs_expose_exact_public_shapes(
    case: DocumentationContractCase,
) -> None:
    """Each user surface names its shipped identity fields and presentation."""
    text = _read_text(case.relative_path)

    missing = tuple(term for term in case.required if term not in text)
    assert not missing, f"{case.relative_path} is missing {missing!r}"


def test_identity_adr_pins_canonical_payloads_and_vectors() -> None:
    """The ADR carries reproducible payloads, encoding, and fixed-width vectors."""
    adr = _read_text("docs/dev/adr/0015-deterministic-record-identity.md")
    payloads = (
        '{"kind":"prompt","role":"user","text_sha256":"2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824","type":"record-content","v":1}',
        '{"agent":"codex","key_kind":"session","key_value":"abc","namespace":"codex.session","type":"thread","v":1}',
        '{"agent":"codex","content_id":"agc1:2vlm1978v1np5kg5fkqv539kic","coordinate_kind":"native","coordinate_value":"msg-1","thread_id":"agt1:bkd9k19ok4vvbsf73jornija04","type":"record","v":1}',
    )
    vectors = (
        "agc1:2vlm1978v1np5kg5fkqv539kic",
        "agt1:bkd9k19ok4vvbsf73jornija04",
        "agr1:uuqn9q331f1fcgsr5gr8agefhs",
    )
    recipe = (
        "SHA-256",
        "first 128 bits",
        "lowercase",
        "unpadded",
        "base32hex",
        "31 characters",
        "compact",
        "sorted",
        "ensure_ascii=False",
        'separators=(",", ":")',
        "UTF-8",
        "surrogatepass",
        "casefold()",
    )

    assert all(payload in adr for payload in payloads)
    assert all(vector in adr and len(vector) == 31 for vector in vectors)
    assert all(term in adr for term in recipe)


def test_identity_adr_records_dedupe_and_observed_topology_limits() -> None:
    """The ADR keeps engine policy and incomplete topology claims explicit."""
    adr = _read_text("docs/dev/adr/0015-deterministic-record-identity.md")
    dedupe_forms = (
        "logical-native",
        "logical-ordinal",
        "physical-native",
        "physical-ordinal",
        "fallback-thread",
        "fallback-path",
    )
    topology_limits = (
        "completeness",
        "revision",
        "connectivity",
        "acyclicity",
        "root",
        "active leaf",
        "branch",
        "transcript order",
    )

    assert all(f"`{form}`" in adr for form in dedupe_forms)
    assert "(kind, normalized role, exact text)" in adr
    assert "no cryptographic hashing" in adr
    assert "Any truthy `session_id` is accepted without path-shape filtering." in adr
    assert "A fallback `conversation_id` must be non-path-shaped." in adr
    assert "`(store, adapter_id)`" in adr
    assert "store-scoped `fallback-thread`" in adr
    assert "does not require an identity namespace" in adr
    assert "observed topology" in adr
    assert all(limit in adr for limit in topology_limits)
    assert "issue #81" in adr


def test_identity_adr_keeps_refs_and_privacy_boundaries_positive() -> None:
    """The ADR tells readers what handles reveal and what still resolves."""
    adr = _read_text("docs/dev/adr/0015-deterministic-record-identity.md")
    required = (
        "`agref1:`",
        "`agcur1:`",
        "physical locator",
        "`inspect_result`",
        "not accepted refs",
        "pseudonymous",
        "equality",
        "dictionary-guessable",
        "not secrets",
        "not authentication",
        "not anonymization",
        "observed upstream schemas are not stable APIs",
    )

    assert all(term in adr for term in required)


def test_identity_changelog_has_one_unreleased_deliverable() -> None:
    """The unreleased section describes one product-level issue 80 outcome."""
    changes = _read_text("CHANGES")
    release_match = re.search(
        r"^## agentgrep 0\.1\.0a38 \(Yet to be released\)\n"
        r"(?P<body>.*?)(?=^## agentgrep |\Z)",
        changes,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert release_match is not None
    release = release_match.group("body")
    heading = "#### Consistent record handles across search (#80)"

    assert release.count(heading) == 1
    assert changes.count(heading) == 1
    assert not re.search(r"agentgrep 0\.1\.0a38 (?:is|ships|focuses)", release)
    assert "### What's new" in release
    assert "adr-deterministic-record-identity" in release
    assert re.search(r"repeated\s+content", release)
    assert "stored turns" in release
    assert "refs remain unchanged" in release
    assert "missing" in release and "invent" in release
    assert all(term in release for term in ("bookmarks", "export", "similarity"))
