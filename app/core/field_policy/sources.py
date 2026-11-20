"""Source evidence closure. Delivery must independently reauthorize each source."""

from dataclasses import dataclass

from .contracts import (
    DOMAINS,
    Access,
    Catalog,
    FieldRef,
    canonical,
    limit,
    name,
    primitive,
    require,
    sequence,
    tenant,
)


@dataclass(frozen=True, order=True)
class SourceRef:
    domain_kind: str
    tenant_id: int | None
    record_key: str
    ref: FieldRef
    access: Access
    catalog_digest: str
    projection_version: str


@dataclass(frozen=True)
class SourceNode:
    node_id: str
    refs: tuple[SourceRef, ...]
    parents: tuple[str, ...]


def collect_sources(catalog, nodes, roots, *, tenant_id, projection_version):
    require(type(catalog) is Catalog)
    tenant(tenant_id)
    name(projection_version)
    sequence(nodes, 16384, SourceNode)
    sequence(roots, 16384, str)
    require(bool(roots) and len(set(roots)) == len(roots))
    index, count, edges = {}, 0, 0
    for node in nodes:
        name(node.node_id)
        require(node.node_id not in index)
        sequence(node.refs, 16384, SourceRef)
        sequence(node.parents, 16384, str)
        count += len(node.refs)
        edges += len(node.parents)
        limit(count <= 16384 and edges <= 65536)
        require(len(set(node.parents)) == len(node.parents))
        index[node.node_id] = node
        for source in node.refs:
            require(type(source.domain_kind) is str and source.domain_kind in DOMAINS)
            require(
                type(source.ref) is FieldRef
                and source.ref in catalog.fields
                and type(source.access) is Access
            )
            field = catalog.fields[source.ref]
            require(
                source.domain_kind == catalog.objects[source.ref.object_id].domain_kind
                and source.access in field.accesses
            )
            require(
                type(source.record_key) is str and 0 < len(source.record_key) <= 128
            )
            require(
                type(source.catalog_digest) is str
                and source.catalog_digest == catalog.digest
            )
            require(
                type(source.projection_version) is str
                and source.projection_version == projection_version
            )
            if source.domain_kind == "tenant":
                tenant(source.tenant_id)
                require(source.tenant_id == tenant_id)
            else:
                require(source.tenant_id is None)
    canonical([primitive(node) for node in nodes], 4 * 1024 * 1024)
    visiting, depths, closures = set(), {}, {}
    expanded, union_work = 0, 0

    def visit(key):
        nonlocal expanded, union_work
        require(key in index and key not in visiting)
        if key in depths:
            return depths[key]
        visiting.add(key)
        limit(len(visiting) <= 16)
        depth = 1 + max((visit(parent) for parent in index[key].parents), default=0)
        limit(depth <= 16)
        visiting.remove(key)
        depths[key] = depth
        sources = set(index[key].refs)
        for parent in index[key].parents:
            union_work += len(closures[parent])
            limit(union_work <= 1048576)
            sources.update(closures[parent])
            limit(len(sources) <= 16384)
        expanded += len(sources)
        limit(expanded <= 262144)
        # Each copy must carry its own ancestry, not borrow evidence from an
        # unrelated sibling which happens to appear in the final root result.
        available = {(s.ref, s.record_key) for s in sources}
        refs = {s.ref for s in sources}
        for source in index[key].refs:
            for dependency in catalog.fields[source.ref].sources:
                require(
                    (dependency, source.record_key) in available
                    if dependency.object_id == source.ref.object_id
                    else dependency in refs
                )
        closures[key] = sources
        return depth

    for root in roots:
        visit(root)
    sources = set().union(*(closures[root] for root in roots))
    require(len(closures) == len(index) and bool(sources))
    # Cross-object record relationships are still checked by the trusted owner.
    return tuple(sorted(sources))
