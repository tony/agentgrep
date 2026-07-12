"""Deterministic portable record export tests."""

from __future__ import annotations

import dataclasses
import io
import itertools
import json
import os
import pathlib
import stat
import sys
import typing as t

import pytest

import agentgrep.record_export as record_export
from agentgrep.conversations import ConversationFidelity, ConversationUnit
from agentgrep.identity import RecordIdentity
from agentgrep.record_export import (
    ExportArtifact,
    ExportEncodingError,
    ExportExistsError,
    ExportSafetyError,
    ExportSelectionError,
    ExportWriteError,
    render_export,
    write_export,
    write_private_export,
)
from agentgrep.records import RecordOrigin, RecordPosition, SearchRecord


def _record(
    text: str,
    *,
    kind: t.Literal["prompt", "history"] = "prompt",
    role: str | None = "user",
    session_id: str | None = "session-1",
    identity_namespace: str | None = "codex.session",
    position: RecordPosition | None = None,
    timestamp: str | None = "2026-07-12T12:00:00Z",
    model: str | None = "gpt-test",
    store: str = "codex.sessions",
    path: pathlib.Path = pathlib.Path("session.jsonl"),
) -> SearchRecord:
    """Build one export-focused normalized record."""
    return SearchRecord(
        kind=kind,
        agent="codex",
        store=store,
        adapter_id="codex.sessions_jsonl.v1",
        path=path,
        text=text,
        title="private display title",
        role=role,
        timestamp=timestamp,
        model=model,
        session_id=session_id,
        conversation_id=session_id,
        metadata={"private-metadata": "must-not-leak"},
        origin=RecordOrigin(cwd="/private/project", branch="private-branch"),
        identity_namespace=identity_namespace,
        position=position,
    )


def _ndjson_rows(artifact: ExportArtifact) -> list[dict[str, object]]:
    """Decode one NDJSON artifact into rows."""
    return [json.loads(line) for line in artifact.text.splitlines()]


@pytest.mark.parametrize("export_format", ("ndjson", "markdown"))
@pytest.mark.parametrize("include_bodies", (False, True), ids=("metadata", "bodies"))
@pytest.mark.parametrize("record_count", (0, 1, 3), ids=("zero", "one", "many"))
def test_render_export_covers_cardinality_format_and_body_permutations(
    export_format: record_export.ExportFormat,
    include_bodies: bool,
    record_count: int,
) -> None:
    """Every renderer permutation produces one self-consistent artifact."""
    records = tuple(
        _record(
            f"body-{index}",
            session_id=f"session-{index}",
            position=RecordPosition(ordinal=index, quality="source_order"),
        )
        for index in range(record_count)
    )

    artifact = render_export(records, format=export_format, include_bodies=include_bodies)

    assert artifact.format == export_format
    assert artifact.selection == "records"
    assert artifact.record_count == record_count
    assert artifact.thread_id is None
    assert artifact.fidelity is None
    assert artifact.byte_count == len(artifact.text.encode("utf-8"))
    for index in range(record_count):
        assert (f"body-{index}" in artifact.text) is include_bodies
    if export_format == "ndjson":
        assert len(_ndjson_rows(artifact)) == record_count
        assert artifact.text.endswith("\n") is (record_count > 0)
    else:
        assert artifact.text.startswith("# agentgrep record export\n")


@pytest.mark.parametrize(
    ("kind", "role", "timestamp", "model"),
    tuple(
        itertools.product(
            ("prompt", "history"),
            ("user", "assistant", None, ""),
            ("2026-07-12T12:00:00Z", None),
            ("gpt-test", None),
        ),
    ),
)
def test_ndjson_render_preserves_allowed_role_kind_and_null_metadata(
    kind: t.Literal["prompt", "history"],
    role: str | None,
    timestamp: str | None,
    model: str | None,
) -> None:
    """Allowed nullable scalars retain their exact normalized values."""
    record = _record(
        "body",
        kind=kind,
        role=role,
        timestamp=timestamp,
        model=model,
    )

    row = _ndjson_rows(render_export((record,), format="ndjson", include_bodies=False))[0]

    assert row["kind"] == kind
    assert row["role"] == role
    assert row["timestamp"] == timestamp
    assert row["model"] == model


@pytest.mark.parametrize("include_bodies", (False, True), ids=("metadata", "bodies"))
def test_ndjson_render_uses_exact_allowlist(include_bodies: bool) -> None:
    """Export rows never inherit the broader search serializer surface."""
    artifact = render_export(
        (_record("portable body", position=RecordPosition(ordinal=2, quality="source_order")),),
        format="ndjson",
        include_bodies=include_bodies,
    )
    row = _ndjson_rows(artifact)[0]
    expected = {
        "schema_version",
        "agent",
        "store",
        "kind",
        "role",
        "timestamp",
        "model",
        "content_id",
        "record_id",
        "record_id_stability",
        "thread_id",
    }
    if include_bodies:
        expected.add("text")

    assert set(row) == expected
    assert row["schema_version"] == "agentgrep.v1"
    assert ("text" in row) is include_bodies


def test_ndjson_render_preserves_repeated_content_occurrences() -> None:
    """Equal bodies at different source positions remain distinct turns."""
    records = (
        _record("repeat", position=RecordPosition(ordinal=9, quality="source_order")),
        _record("repeat", position=RecordPosition(ordinal=2, quality="source_order")),
    )

    rows = _ndjson_rows(render_export(records, format="ndjson", include_bodies=True))

    assert len(rows) == 2
    assert rows[0]["content_id"] == rows[1]["content_id"]
    assert rows[0]["record_id"] != rows[1]["record_id"]
    assert [row["text"] for row in rows] == ["repeat", "repeat"]


@pytest.mark.parametrize("export_format", ("ndjson", "markdown"))
@pytest.mark.parametrize("include_bodies", (False, True), ids=("metadata", "bodies"))
def test_render_export_bytes_are_stable_under_every_input_permutation(
    export_format: record_export.ExportFormat,
    include_bodies: bool,
) -> None:
    """Scheduler enumeration cannot affect portable artifact bytes."""
    records = (
        _record(
            "late-a",
            session_id="session-a",
            timestamp="2030-01-01T00:00:00Z",
            position=RecordPosition(ordinal=4, quality="source_order"),
        ),
        _record(
            "early-a",
            session_id="session-a",
            timestamp="2020-01-01T00:00:00Z",
            position=RecordPosition(ordinal=1, quality="source_order"),
        ),
        _record(
            "only-b",
            session_id="session-b",
            timestamp=None,
            position=RecordPosition(native_id="native-b", quality="native"),
        ),
    )

    artifacts = {
        render_export(
            permutation,
            format=export_format,
            include_bodies=include_bodies,
        ).text.encode("utf-8")
        for permutation in itertools.permutations(records)
    }

    assert len(artifacts) == 1


def test_ndjson_render_escapes_lone_surrogates_to_valid_utf8() -> None:
    """Imperfect source text remains portable through JSON escapes."""
    artifact = render_export(
        (_record("before\ud800after"),),
        format="ndjson",
        include_bodies=True,
    )

    encoded = artifact.text.encode("utf-8")

    assert b"before\\ud800after" in encoded
    assert _ndjson_rows(artifact)[0]["text"] == "before\ud800after"
    assert artifact.byte_count == len(encoded)


@pytest.mark.parametrize("include_bodies", (False, True), ids=("metadata", "bodies"))
def test_markdown_render_rejects_emitted_lone_surrogates(include_bodies: bool) -> None:
    """Markdown refuses invalid UTF-8 scalars instead of altering them."""
    record = _record("body\ud800" if include_bodies else "hidden\ud800", model="model\udfff")

    with pytest.raises(ExportEncodingError, match="valid UTF-8") as raised:
        render_export(
            (record,),
            format="markdown",
            include_bodies=include_bodies,
        )

    assert "session.jsonl" not in str(raised.value)
    assert "/private/project" not in str(raised.value)


@pytest.mark.parametrize(
    ("body", "expected_fence_length"),
    (
        pytest.param("plain", 3, id="no-backticks"),
        pytest.param("before ``` after", 4, id="triple"),
        pytest.param("before ```` after", 5, id="quadruple"),
        pytest.param("before ````````````````` after", 18, id="long-run"),
    ),
)
def test_markdown_render_uses_dynamic_backtick_fences(
    body: str,
    expected_fence_length: int,
) -> None:
    """A body can never terminate its own Markdown fence."""
    artifact = render_export(
        (_record(body),),
        format="markdown",
        include_bodies=True,
    )
    marker = "\n### Body\n\n"
    fenced = artifact.text.split(marker, 1)[1]
    opening, rendered_body, closing, _trailer = fenced.split("\n", 3)

    assert opening == "`" * expected_fence_length + "text"
    assert rendered_body == body
    assert closing == "`" * expected_fence_length


def test_render_thread_rejects_null_and_mixed_thread_identity() -> None:
    """A thread label requires exactly one non-null canonical unit."""
    threadless = _record("flat", session_id=None, identity_namespace=None)
    first = _record("first", session_id="thread-a")
    second = _record("second", session_id="thread-b")

    for records in ((threadless,), (first, second), (threadless, first)):
        with pytest.raises(ExportSelectionError, match="exactly one observed thread"):
            render_export(
                records,
                format="ndjson",
                include_bodies=False,
                selection="thread",
            )


@pytest.mark.parametrize(
    ("records", "expected_fidelity"),
    (
        pytest.param(
            (
                _record(
                    "first",
                    position=RecordPosition(ordinal=0, quality="source_order"),
                ),
                _record(
                    "second",
                    position=RecordPosition(ordinal=1, quality="source_order"),
                ),
            ),
            "source_order",
            id="source-order",
        ),
        pytest.param(
            (
                _record(
                    "root",
                    position=RecordPosition(native_id="root", quality="native"),
                ),
                _record(
                    "child",
                    position=RecordPosition(
                        native_id="child",
                        parent_native_id="root",
                        quality="native",
                    ),
                ),
            ),
            "native_tree",
            id="native-tree",
        ),
        pytest.param(
            (
                _record(
                    "one",
                    position=RecordPosition(native_id="one", quality="native"),
                ),
                _record(
                    "two",
                    position=RecordPosition(native_id="two", quality="native"),
                ),
            ),
            "unordered",
            id="unordered",
        ),
    ),
)
def test_markdown_render_thread_discloses_every_fidelity(
    records: tuple[SearchRecord, ...],
    expected_fidelity: ConversationFidelity,
) -> None:
    """Thread Markdown labels the observed unit without inventing order."""
    artifact = render_export(
        records,
        format="markdown",
        include_bodies=False,
        selection="thread",
    )

    assert artifact.selection == "thread"
    assert artifact.thread_id is not None
    assert artifact.fidelity == expected_fidelity
    assert artifact.record_count == 2
    assert artifact.text.startswith("# agentgrep observed thread export\n")
    assert f"- Fidelity: {expected_fidelity}\n" in artifact.text
    assert f"- Thread ID: {artifact.thread_id}\n" in artifact.text


@pytest.mark.parametrize("export_format", ("ndjson", "markdown"))
@pytest.mark.parametrize("include_bodies", (False, True), ids=("metadata", "bodies"))
def test_render_thread_uses_export_order_under_all_input_permutations(
    export_format: record_export.ExportFormat,
    include_bodies: bool,
) -> None:
    """Thread validation does not replace the export-owned total order."""
    late = _record(
        "late body",
        timestamp="2030-01-01T00:00:00Z",
        position=RecordPosition(ordinal=0, quality="source_order"),
    )
    early = _record(
        "early body",
        timestamp="2020-01-01T00:00:00Z",
        position=RecordPosition(ordinal=1, quality="source_order"),
    )

    artifacts = tuple(
        render_export(
            permutation,
            format=export_format,
            include_bodies=include_bodies,
            selection="thread",
        )
        for permutation in itertools.permutations((late, early))
    )

    assert len({artifact.text.encode("utf-8") for artifact in artifacts}) == 1
    text = artifacts[0].text
    if export_format == "ndjson":
        assert [row["timestamp"] for row in _ndjson_rows(artifacts[0])] == [
            "2020-01-01T00:00:00Z",
            "2030-01-01T00:00:00Z",
        ]
    else:
        assert text.index("2020-01-01T00:00:00Z") < text.index("2030-01-01T00:00:00Z")
    if include_bodies:
        assert text.index("early body") < text.index("late body")


@pytest.mark.parametrize("export_format", ("ndjson", "markdown"))
@pytest.mark.parametrize("include_bodies", (False, True), ids=("metadata", "bodies"))
def test_render_export_never_leaks_excluded_record_fields(
    export_format: record_export.ExportFormat,
    include_bodies: bool,
) -> None:
    """Paths, titles, native anchors, origins, and metadata stay private."""
    excluded = (
        "secret-source-name.jsonl",
        "private display title",
        "secret-native-session",
        "/private/project",
        "private-branch",
        "private-metadata",
        "must-not-leak",
        "codex.sessions_jsonl.v1",
    )
    record = _record(
        "portable body",
        session_id="secret-native-session",
        path=pathlib.Path("secret-source-name.jsonl"),
    )

    artifact = render_export(
        (record,),
        format=export_format,
        include_bodies=include_bodies,
    )

    assert all(value not in artifact.text for value in excluded)


@pytest.mark.parametrize("selection", ("records", "thread"))
def test_render_export_prepares_each_record_identity_once(
    monkeypatch: pytest.MonkeyPatch,
    selection: record_export.ExportSelection,
) -> None:
    """Rendering caches the cryptographic identity bundle per record."""
    records = (
        _record("one", position=RecordPosition(ordinal=0, quality="source_order")),
        _record("two", position=RecordPosition(ordinal=1, quality="source_order")),
    )
    real_record_identity = record_export.record_identity
    calls: list[SearchRecord] = []

    def counting_record_identity(record: SearchRecord) -> record_export.RecordIdentity:
        calls.append(record)
        return real_record_identity(record)

    monkeypatch.setattr(record_export, "record_identity", counting_record_identity)

    _ = render_export(
        records,
        format="ndjson",
        include_bodies=True,
        selection=selection,
    )

    assert calls == list(records)


def test_render_thread_reuses_conversation_grouping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thread validation and fidelity stay owned by conversations."""
    record = _record(
        "one",
        position=RecordPosition(ordinal=0, quality="source_order"),
    )
    real_group = record_export.group_prepared_conversation_units
    calls: list[tuple[tuple[SearchRecord, RecordIdentity], ...]] = []

    def counting_group(
        records: t.Iterable[tuple[SearchRecord, RecordIdentity]],
    ) -> tuple[ConversationUnit, ...]:
        consumed = tuple(records)
        calls.append(consumed)
        return real_group(consumed)

    monkeypatch.setattr(record_export, "group_prepared_conversation_units", counting_group)

    _ = render_export(
        (record,),
        format="markdown",
        include_bodies=False,
        selection="thread",
    )

    assert len(calls) == 1
    assert [item[0] for item in calls[0]] == [record]


def test_render_export_artifact_has_exact_immutable_contract() -> None:
    """The frontend-neutral return value stays shallow-frozen and slot-backed."""
    artifact = render_export((), format="ndjson", include_bodies=False)

    assert tuple(field.name for field in dataclasses.fields(artifact)) == (
        "format",
        "selection",
        "record_count",
        "thread_id",
        "fidelity",
        "text",
        "byte_count",
    )
    assert not hasattr(artifact, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.cast("t.Any", artifact).record_count = 1


def _writer_artifact(
    text: str = "portable body",
    *,
    export_format: record_export.ExportFormat = "ndjson",
) -> ExportArtifact:
    """Render one canonical artifact for writer tests."""
    return render_export(
        (
            _record(
                text,
                position=RecordPosition(ordinal=7, quality="source_order"),
            ),
        ),
        format=export_format,
        include_bodies=True,
    )


def test_write_export_writes_exact_artifact_bytes_without_stdout(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """File output depends only on the rendered artifact."""
    artifact = _writer_artifact("café 🦀")
    destination = tmp_path / "artifact.ndjson"
    fake_stdout = io.StringIO("unrelated terminal state")
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    result = write_export(artifact, destination)

    assert result == destination
    assert destination.read_bytes() == artifact.text.encode("utf-8")
    assert fake_stdout.getvalue() == "unrelated terminal state"


def test_write_export_creates_fresh_private_file(tmp_path: pathlib.Path) -> None:
    """A new destination receives the complete artifact at mode 0600."""
    artifact = _writer_artifact()
    destination = tmp_path / "fresh.ndjson"

    write_export(artifact, destination)

    assert destination.read_text(encoding="utf-8") == artifact.text
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_write_export_refuses_existing_target_without_modifying_it(
    tmp_path: pathlib.Path,
) -> None:
    """No-clobber output preserves a pre-existing destination."""
    destination = tmp_path / "existing.ndjson"
    destination.write_text("keep me", encoding="utf-8")

    with pytest.raises(ExportExistsError, match="already exists") as raised:
        write_export(_writer_artifact(), destination)

    assert destination.read_text(encoding="utf-8") == "keep me"
    assert str(destination) not in str(raised.value)


def test_write_export_force_atomically_replaces_regular_file(tmp_path: pathlib.Path) -> None:
    """Explicit force replaces a regular destination with artifact bytes."""
    artifact = _writer_artifact("replacement")
    destination = tmp_path / "replace.ndjson"
    destination.write_text("old", encoding="utf-8")

    write_export(artifact, destination, force=True)

    assert destination.read_bytes() == artifact.text.encode("utf-8")
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


@pytest.mark.parametrize("force", (False, True), ids=("no-force", "force"))
def test_write_export_rejects_destination_symlink_without_following_it(
    tmp_path: pathlib.Path,
    force: bool,
) -> None:
    """A final-component symlink can never redirect exported text."""
    target = tmp_path / "source-secret.jsonl"
    target.write_text("source bytes", encoding="utf-8")
    destination = tmp_path / "export.ndjson"
    destination.symlink_to(target)

    with pytest.raises(ExportSafetyError, match="unsafe") as raised:
        write_export(_writer_artifact(), destination, force=force)

    assert target.read_text(encoding="utf-8") == "source bytes"
    assert destination.is_symlink()
    assert str(destination) not in str(raised.value)
    assert str(target) not in str(raised.value)


def test_write_export_rejects_parent_symlink_traversal(tmp_path: pathlib.Path) -> None:
    """No ancestor symlink can redirect a same-directory temporary file."""
    real_parent = tmp_path / "real-private-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-private-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    destination = linked_parent / "artifact.ndjson"

    with pytest.raises(ExportSafetyError, match="unsafe") as raised:
        write_export(_writer_artifact(), destination)

    assert not (real_parent / "artifact.ndjson").exists()
    assert str(linked_parent) not in str(raised.value)


def test_write_export_rejects_protected_source_alias(tmp_path: pathlib.Path) -> None:
    """Normalized path aliases cannot overwrite a selected source."""
    source = tmp_path / "source-private.jsonl"
    source.write_text("source bytes", encoding="utf-8")
    alias = tmp_path / "nested" / ".." / source.name

    with pytest.raises(ExportSafetyError, match="protected source") as raised:
        write_export(
            _writer_artifact(),
            alias,
            force=True,
            protected_paths=(source,),
        )

    assert source.read_text(encoding="utf-8") == "source bytes"
    assert str(source) not in str(raised.value)


def test_write_export_rejects_hard_link_to_protected_source(tmp_path: pathlib.Path) -> None:
    """Inode aliases cannot bypass protected source checks under force."""
    source = tmp_path / "source-private.jsonl"
    source.write_text("source bytes", encoding="utf-8")
    destination = tmp_path / "hard-link.ndjson"
    destination.hardlink_to(source)

    with pytest.raises(ExportSafetyError, match="protected source") as raised:
        write_export(
            _writer_artifact(),
            destination,
            force=True,
            protected_paths=(source,),
        )

    assert source.read_text(encoding="utf-8") == "source bytes"
    assert destination.read_text(encoding="utf-8") == "source bytes"
    assert str(source) not in str(raised.value)


def test_write_export_retries_short_os_writes(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Complete-write semantics tolerate positive short writes."""
    artifact = _writer_artifact("x" * 257)
    destination = tmp_path / "short-write.ndjson"
    real_write = os.write
    write_sizes: list[int] = []

    def short_write(fd: int, data: bytes | bytearray | memoryview) -> int:
        chunk = data[:7]
        write_sizes.append(len(chunk))
        return real_write(fd, chunk)

    monkeypatch.setattr(record_export.os, "write", short_write)

    write_export(artifact, destination)

    assert len(write_sizes) > 1
    assert destination.read_bytes() == artifact.text.encode("utf-8")


def test_write_export_cleans_temporary_file_after_failure(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed complete write leaves neither destination nor temp debris."""
    destination = tmp_path / "failed.ndjson"

    def fail_write(_fd: int, _data: bytes | bytearray | memoryview) -> t.NoReturn:
        raise OSError

    monkeypatch.setattr(record_export.os, "write", fail_write)

    with pytest.raises(ExportWriteError, match="could not be written") as raised:
        write_export(_writer_artifact(), destination)

    assert list(tmp_path.iterdir()) == []
    assert str(destination) not in str(raised.value)
    assert "synthetic" not in str(raised.value)


def test_write_export_fsyncs_file_and_parent_directory(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful install durably syncs content and directory metadata."""
    destination = tmp_path / "durable.ndjson"
    real_fsync = os.fsync
    synced_types: list[str] = []

    def tracking_fsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        synced_types.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(fd)

    monkeypatch.setattr(record_export.os, "fsync", tracking_fsync)

    write_export(_writer_artifact(), destination)

    assert synced_types.count("file") == 1
    assert synced_types.count("directory") == 1


def test_write_export_no_clobber_install_wins_race_safely(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A competitor created after the temp write is never overwritten."""
    destination = tmp_path / "raced.ndjson"
    real_link = os.link

    def competing_link(
        source: str,
        target: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        competitor_fd = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=dst_dir_fd,
        )
        try:
            _ = os.write(competitor_fd, b"competitor")
        finally:
            os.close(competitor_fd)
        real_link(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(record_export.os, "link", competing_link)

    with pytest.raises(ExportExistsError, match="already exists"):
        write_export(_writer_artifact(), destination)

    assert destination.read_bytes() == b"competitor"
    assert [path.name for path in tmp_path.iterdir()] == [destination.name]


def test_write_private_export_enforces_directory_mode_and_collision_suffix(
    tmp_path: pathlib.Path,
) -> None:
    """Private output is 0700/0600 and allocates stable canonical names."""
    directory = tmp_path / "exports"
    artifact = _writer_artifact("body-private-token", export_format="markdown")
    record_id = _ndjson_rows(_writer_artifact("body-private-token"))[0]["record_id"]
    assert isinstance(record_id, str)
    slug = record_id.replace(":", "-")

    first = write_private_export(artifact, directory=directory)
    second = write_private_export(artifact, directory=directory)

    assert first == directory / f"agentgrep-{slug}.md"
    assert second == directory / f"agentgrep-{slug}-2.md"
    assert first.read_text(encoding="utf-8") == artifact.text
    assert second.read_text(encoding="utf-8") == artifact.text
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert "body-private-token" not in first.name
    assert "private" not in first.name


def test_write_private_export_uses_thread_id_slug(tmp_path: pathlib.Path) -> None:
    """Observed thread names derive only from their canonical thread ID."""
    artifact = render_export(
        (
            _record(
                "private thread body",
                position=RecordPosition(ordinal=0, quality="source_order"),
            ),
        ),
        format="ndjson",
        include_bodies=True,
        selection="thread",
    )
    assert artifact.thread_id is not None

    destination = write_private_export(artifact, directory=tmp_path / "exports")

    assert destination.name == f"agentgrep-{artifact.thread_id.replace(':', '-')}.ndjson"
    assert "private" not in destination.name


def test_write_private_export_ignores_noncanonical_thread_id(tmp_path: pathlib.Path) -> None:
    """A public artifact cannot turn an arbitrary thread value into a path."""
    artifact = ExportArtifact(
        format="ndjson",
        selection="thread",
        record_count=0,
        thread_id="../../private-path",
        fidelity="unordered",
        text="",
        byte_count=0,
    )
    directory = tmp_path / "exports"

    destination = write_private_export(artifact, directory=directory)

    assert destination == directory / "agentgrep-empty.ndjson"


def test_write_private_export_never_reads_id_shaped_markdown_body(
    tmp_path: pathlib.Path,
) -> None:
    """Only structural metadata, never record text, may supply a slug."""
    fake_id = "agr1:00000000000000000000000000"
    text = f"# export\n\n### Body\n\n```text\n- Record ID: {fake_id}\n```\n"
    artifact = ExportArtifact(
        format="markdown",
        selection="records",
        record_count=1,
        thread_id=None,
        fidelity=None,
        text=text,
        byte_count=len(text.encode("utf-8")),
    )

    destination = write_private_export(artifact, directory=tmp_path / "exports")

    assert destination.name == "agentgrep-empty.md"
    assert fake_id.replace(":", "-") not in destination.name


def test_write_private_export_uses_content_id_before_id_shaped_record_body(
    tmp_path: pathlib.Path,
) -> None:
    """A rendered null record ID cannot make body prose control its name."""
    fake_id = "agr1:00000000000000000000000000"
    record = _record(
        f"body\n- Record ID: {fake_id}",
        session_id=None,
        identity_namespace=None,
        position=None,
    )
    artifact = render_export(
        (record,),
        format="markdown",
        include_bodies=True,
    )
    content_id = record_export.record_identity(record).content_id

    destination = write_private_export(artifact, directory=tmp_path / "exports")

    assert destination.name == f"agentgrep-{content_id.replace(':', '-')}.md"
    assert fake_id.replace(":", "-") not in destination.name


def test_write_export_errors_never_disclose_destination_path(tmp_path: pathlib.Path) -> None:
    """Filesystem failures expose stable guidance rather than local paths."""
    destination = tmp_path / "secret-parent" / "secret-artifact.ndjson"

    with pytest.raises(ExportWriteError) as raised:
        write_export(_writer_artifact(), destination)

    message = str(raised.value)
    assert str(destination) not in message
    assert "secret-parent" not in message
    assert "secret-artifact" not in message
