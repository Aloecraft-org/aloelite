Each requirement is identified by a stable prefix and number for reference. Requirements use *must* for invariants the system enforces and *may* for permitted-but-optional behavior. This section is self-contained and does not depend on the surrounding abstract or discussion.

## Nodes

- **NODE-1** : Every node has a globally unique identity expressed as a uuid7, assigned once at creation and never reminted or backdated. The identity reflects the node's actual creation time and defines a stable time order.
- **NODE-2** : A node is exactly one of two types, Container or Entry, fixed at creation. No other node types exist in this iteration.
- **NODE-3** : A node carries its own name. The name is a property of node identity and is independent of where, or whether, the node is placed.
- **NODE-4** : A node carries a `created_at` value distinct from its uuid7. Unlike the uuid7, `created_at` may hold a value other than the moment the row was created (for example, a value preserved from a source node during a copy).
- **NODE-5** : Sibling name collisions are permitted. Among nodes sharing a name within the same container, the one with the greatest uuid7 is the resolved (visible) node; the others are hidden from normal resolution but remain fully addressable by id.
- **NODE-6** : A node may carry a shallow string-to-string metadata map, independent of name and placement. Metadata is preserved across Copy and round-tripped by Pack/Unpack. Setting metadata does not alter `modified_at`.

## Edges

- **EDGE-1** : An edge is a directed relationship from a `from_id` to a `to_id`, expressing that the `to_id` node is placed within the `from_id` container.
- **EDGE-2** : Edges are immutable: once created, an edge is never modified. All structural change is expressed by creating new edges and archiving or removing existing ones.
- **EDGE-3** : Each edge has its own identity (uuid7) and carries `from_id`, `to_id`, `volume_id`, and an `archived` flag.
- **EDGE-4** : `from_id` must reference a Container. `to_id` may reference either a Container or an Entry.
- **EDGE-5** : The `archived` flag marks a placement inactive without removing the edge row. Archived edges are excluded from normal resolution but are retained for history and recovery.
- **EDGE-6** : An edge targets nodes only. A volume is never the `from_id` or `to_id` of an edge.

## Volumes

- **VOL-1** : A volume is a distinct entity with its own identity, modeled separately from nodes and edges. It is neither a node nor an edge.
- **VOL-2** : A volume designates exactly one root node as its origin. The root is referenced by the volume itself, not by any edge; consequently a volume's root node has no incoming edge.
- **VOL-3** : Every edge references the volume it belongs to via `volume_id`, and that reference must resolve to an existing volume.
- **VOL-4** : A volume must not be moved or re-parented. Relocating a volume's contents is ordinary edge activity beneath its root; any operation that would establish a volume's origin elsewhere instead produces a new, distinct volume (a fork).

## Placement and Integrity

- **PI-1** : Within a single volume, a node may be the `to_id` of at most one active (non-archived) edge. This is enforced as a uniqueness constraint over `(volume_id, to_id)` and guarantees each placed node has exactly one active parent within its volume.
- **PI-2** : Every node that is part of a volume and is not that volume's root must have exactly one active incoming edge; a volume root has none.
- **PI-3** : A node with no incoming edge that is not a volume root is in a volatile (orphaned) state: addressable by id, excluded from tree traversal, and eligible for later pruning.
- **PI-4** : The uniqueness constraint of PI-1 prevents any cycle formed by giving an already-placed node an additional parent.
- **PI-5** : A cycle formed by relocation (i.e. placing a container beneath one of its own descendants) is not caught by PI-1 and must be prevented by an ancestor check at reparent time. The proposed new parent must be neither the node being moved nor any descendant of it.
- **PI-6** : An edge's `volume_id` must equal the volume of its `from_id`; placements never span volumes. Moving a subtree between volumes re-stamps `volume_id` across the entire moved subtree.
- **PI-7** : Because archived edges are retained, a subtree detached in error remains recoverable by walking archived edges. The eager prevention of PI-5 is the primary defense; this recoverability is the backstop.

## Content and I/O

- **IO-1** : An Entry's content (payload) is stored separately from node metadata, keyed by node, so that namespace traversal and path resolution touch only metadata.
- **IO-2** : Reading or writing the full content of an Entry is an atomic operation.
    + **IO-2r1**: Reading or writing the full content of an Entry is atomic. Where content is chunked, a write completes by atomically advancing the Entry's committed content version to a fully-recorded new version; partial writes leave the committed version unchanged.
- **IO-3** : The system provides a streaming, file-descriptor-like access path for incremental reading and writing of content without materializing the entire payload.
- **IO-4** : Streaming access must not hold a long-lived write transaction open for the duration of the stream.
- **IO-5** : Path resolution walks active edges from a node toward its volume root. The same walk surfaces an archived or orphaned ancestor and serves as the detection backstop for cycles.
- **IO-6** : A Container may have associated content, but its meaning is undefined in this iteration and no behavior depends on it.

## Access

- **ACC-1a** : A mount is a durable immutable access point bound to an explicit node in exactly one volume. All access to a volume is brokered through a mount; access is never ambient.
- **ACC-2** : A mount has a mount point: a reference to a node within its volume that anchors the session for resolution and scoping.
- **ACC-3** : A mount carries a ttl. The mounts table is part of the schema, but its contents are not held to the same persistence expectations as the rest of the model.
- **ACC-4** : Mount state is actively managed across a lifecycle (open → active → unmount); mount and unmount are operations. Unmount marks the mount invalid; reclamation of what it held is deferred to pruning rather than performed eagerly.
- **ACC-5** : If a mount point node is removed entirely, the mount becomes invalid. If the mount point node is merely archived, the mount is flagged as anchored on an archived node, and reads through the mount may still be permitted.
- **ACC-6** : A lock is scoped to exactly one mount and may not be shared across mounts. The locking facility is advisory, distinct from and layered above SQLite's own transactional concurrency.
- **ACC-7** : Locks are exclusive in this iteration.
- **ACC-8** : A lock carries validity comprising `read_count`, `write_count`, and `ttl`. The counts are recorded but not yet enforced against; they forward-provision a future multi-reader policy without committing to one now.
- **ACC-9** : A lock is valid only while its mount exists and its ttl has not elapsed, and a lock must not outlive its mount. A long-lived streaming lock is an ordinary mount-scoped lock with a generous ttl, not a distinct category.
- **ACC-10** : Mounts are the only actively-managed access state. Lock invalidity follows from mount disappearance or ttl expiry rather than from active invalidation, and invalid locks are reclaimed lazily by pruning. This is the same sweep named in PI-3 for volatile nodes, applied here to the lock side.

## Operations

- **OP-1** : All access and mutation is brokered through the Mount API; nothing bypasses it. The Mount API is the single site for invariant enforcement, guards, and reserved hooks.
- **OP-2** : Create introduces a new node (and its content, if any) and places it by creating an edge under a target container.
- **OP-3** : Move is expressed as archiving or removing a node's active edge and creating a new edge under the target container. The node's identity, name, and `created_at` are unchanged. A move is subject to PI-5 and PI-6.
- **OP-4** : Copy produces a new node with a fresh uuid7 whose `created_at` is preserved from the source, with content duplicated. Copying a container into its own subtree copies the source as it existed at the start of the operation.
    + **OP-4r1** : Copy produces a new node with a fresh uuid7 whose created_at and metadata are preserved from the source, with content duplicated. Copying a container into its own subtree copies the source as it existed at the start of the operation.
- **OP-5** : Delete removes a node's active placement. Deleting a container detaches its children, which become volatile per PI-3 unless re-homed. Hard removal of a node and its content is a separate, explicit step.
- **OP-6** : Pack consolidates a subtree into a portable serialized form (MsgPack), realized as an Entry whose payload is that serialized form, and supersedes the original placement.
- **OP-7** : Unpack is the inverse of Pack: it restores a packed subtree from its serialized form and supersedes the packed Entry.
- **OP-8** : Mount and Unmount are operations whose structure and lifecycle are defined under Access (ACC-1 through ACC-5).

## Transactional Guarantees

- **TX-1** : Every operation exposed by the Mount API is atomic: it completes fully or has no effect.
- **TX-2** : Pack and Unpack each execute within a single transaction. For Pack, the original subtree is removed only in the transaction that durably records its packed form; for Unpack, the packed form is removed only once the restored subtree is durably recorded. Neither consolidation nor restoration can lose data.
- **TX-3** : Recoverability of erroneously detached subtrees is provided by retained archived edges (per PI-7), not by transactional rollback alone.

## Content and Versioning
- **CV-1** : An Entry's content may be stored as an ordered sequence of chunks drawn from a per-volume content-addressed pool. Chunk size is a property of the volume, fixed at volume creation and immutable thereafter.
- **CV-2** : A chunk's identity is the hash of its bytes together with its byte length. Chunks are immutable: once written they are never modified, only referenced, shared across Entries and versions, and reclaimed when unreferenced. No operation may mutate a chunk in place.
- **CV-3** : An Entry's content has a monotonic version counter, advanced only under the Entry's write lock. The committed version recorded on the content row is the sole definition of the Entry's current bytes; manifest rows at other versions are superseded history or incomplete writes and are not visible content.
- **CV-4** : The chunk sequence of each version is recorded as an explicit ordered manifest (one reference per chunk position), keyed such that position within a version is unique and reassembly order is determined by the manifest, not by chunk identity.
- **CV-5** : Streaming writes record chunks as independently-committed insertions into the immutable pool; only the final version-pointer advance is transactional. No streaming operation holds a write transaction open for the duration of the stream. *(This is the explicit discharge of IO-4 under chunking.)*
- **CV-6** : An Entry carries a single content-retention policy governing which superseded versions are retained (e.g. last N versions, versions within a time window, or a history byte budget). The committed version is always retained. Retention is enforced only by pruning, never by the write path.
- **CV-7** : Reclamation of unreferenced content — manifest rows beyond an Entry's retention policy, and pool chunks referenced by no retained version of any node — is performed lazily by a content prune, distinct from but parallel to the node/lock sweep of PI-3 and ACC-10. A chunk is reclaimable only after retained-version resolution; retained versions are resolved before any chunk is collected.
## Extensibility and Deferred Scope

- **EXT-1** : The per-volume single-parent uniqueness of PI-1 is the sole constraint enforcing a tree. The design must permit relaxing it to admit multiple placements per node (e.g. links and a general graph layout) without redesign.
- **EXT-2** : The Mount API (OP-1) must reserve a hook allowing Merkle hash recomputation over affected ancestors to be added without changing operation semantics.
- **EXT-3** : Child ordering is reserved to be defined as edge id, then node uuid7, within a container. Nothing in the present design may foreclose this ordering or its path-aware and Merkle implications.
- **EXT-4** : An edge must remain able to carry an optional per-placement alias that overrides a node's display name in that location, without altering name-on-node identity semantics.