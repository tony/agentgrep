"""Sphinx domain for agentgrep storage identifiers."""

from __future__ import annotations

import typing as t

from docutils import nodes
from sphinx.domains import Domain, ObjType
from sphinx.roles import XRefRole
from sphinx.util import logging
from sphinx.util.nodes import make_refnode

from ._badges import build_coverage_badge
from ._roles import StorageStoreXRefRole

if t.TYPE_CHECKING:
    from sphinx.addnodes import pending_xref
    from sphinx.builders import Builder
    from sphinx.environment import BuildEnvironment

logger = logging.getLogger(__name__)

StorageObject = dict[str, str]


class StorageDomain(Domain):
    """Domain that stores cross-reference targets for catalog entries."""

    name = "storage"
    label = "agentgrep storage"
    data_version = 1

    object_types: t.ClassVar[dict[str, ObjType]] = {
        "store": ObjType("store", "store", "storeref", "storeiconl"),
        "agent": ObjType("agent"),
        "adapter": ObjType("adapter", "adapter"),
        "coverage": ObjType("coverage", "coverage"),
        "format": ObjType("format", "format"),
        "store-role": ObjType("store role", "store-role"),
        "strategy": ObjType("version strategy", "strategy"),
    }
    roles: t.ClassVar[dict[str, t.Any]] = {
        "store": StorageStoreXRefRole(show_badge=True),
        "storeref": StorageStoreXRefRole(show_badge=False),
        "storeiconl": StorageStoreXRefRole(show_badge=False, icon_pos="left"),
        "adapter": XRefRole(innernodeclass=nodes.literal, warn_dangling=True),
        "coverage": XRefRole(innernodeclass=nodes.literal, warn_dangling=True),
        "format": XRefRole(innernodeclass=nodes.literal, warn_dangling=True),
        "store-role": XRefRole(innernodeclass=nodes.literal, warn_dangling=True),
        "strategy": XRefRole(innernodeclass=nodes.literal, warn_dangling=True),
    }
    initial_data: t.ClassVar[dict[str, t.Any]] = {
        "objects": {},
    }

    @property
    def objects(self) -> dict[tuple[str, str], StorageObject]:
        """Return registered storage objects."""
        return t.cast(
            "dict[tuple[str, str], StorageObject]",
            self.data.setdefault("objects", {}),
        )

    def note_object(
        self,
        objtype: str,
        name: str,
        node_id: str,
        *,
        title: str = "",
        coverage: str = "",
    ) -> None:
        """Register a storage target in the domain inventory."""
        key = (objtype, name)
        existing = self.objects.get(key)
        if existing is not None:
            existing_docname = existing["docname"]
            existing_node_id = existing["node_id"]
            if (existing_docname, existing_node_id) != (
                self.env.current_document.docname,
                node_id,
            ):
                logger.warning(
                    "duplicate storage %s %s, other instance in %s",
                    objtype,
                    name,
                    existing_docname,
                )

        self.objects[key] = {
            "docname": self.env.current_document.docname,
            "node_id": node_id,
            "title": title or name,
            "coverage": coverage,
        }

    def clear_doc(self, docname: str) -> None:
        """Remove domain objects owned by *docname*."""
        for key, record in list(self.objects.items()):
            if record["docname"] == docname:
                del self.objects[key]

    def merge_domaindata(self, docnames: set[str], otherdata: dict[str, t.Any]) -> None:
        """Merge storage objects from parallel Sphinx workers."""
        other_objects = t.cast(
            "dict[tuple[str, str], StorageObject]",
            otherdata.get("objects", {}),
        )
        for key, record in other_objects.items():
            if record["docname"] in docnames:
                self.objects[key] = record

    def resolve_xref(
        self,
        env: BuildEnvironment,
        fromdocname: str,
        builder: Builder,
        typ: str,
        target: str,
        node: pending_xref,
        contnode: nodes.Element,
    ) -> nodes.reference | None:
        """Resolve storage-domain cross-references."""
        objtypes = self.objtypes_for_role(typ) or []
        for objtype in objtypes:
            record = self.objects.get((objtype, target))
            if record is None:
                continue
            content = self._reference_content(typ, record, contnode)
            return make_refnode(
                builder,
                fromdocname,
                record["docname"],
                record["node_id"],
                content,
                record["title"],
            )
        return None

    def resolve_any_xref(
        self,
        env: BuildEnvironment,
        fromdocname: str,
        builder: Builder,
        target: str,
        node: pending_xref,
        contnode: nodes.Element,
    ) -> list[tuple[str, nodes.reference]]:
        """Resolve ``any`` references against storage stores."""
        record = self.objects.get(("store", target))
        if record is None:
            return []
        content = self._reference_content("storeref", record, contnode)
        refnode = make_refnode(
            builder,
            fromdocname,
            record["docname"],
            record["node_id"],
            content,
            record["title"],
        )
        return [("storage:storeref", refnode)]

    def get_objects(self) -> t.Iterator[tuple[str, str, str, str, str, int]]:
        """Yield objects for Sphinx's object inventory."""
        for (objtype, name), record in self.objects.items():
            yield name, record["title"], objtype, record["docname"], record["node_id"], 1

    @staticmethod
    def _reference_content(
        typ: str,
        record: StorageObject,
        contnode: nodes.Element,
    ) -> nodes.Element:
        """Return reference contents with optional coverage badge."""
        if typ == "storeiconl":
            inline = nodes.inline()
            if record["coverage"]:
                inline += build_coverage_badge(record["coverage"], icon_only=True)
                inline += nodes.Text(" ")
            inline += contnode.deepcopy()
            return inline
        if typ == "store":
            inline = nodes.inline()
            inline += contnode.deepcopy()
            if record["coverage"]:
                inline += nodes.Text(" ")
                inline += build_coverage_badge(record["coverage"])
            return inline
        return t.cast(nodes.Element, contnode.deepcopy())
