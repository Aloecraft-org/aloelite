# Aloelite CSI Driver — Specification

## Purpose

A Kubernetes CSI driver that provisions Aloelite volumes on demand in response
to PVC creation and exposes them as pod-mountable directories. All volume
lifecycle operations are delegated to the Aloelite Volume Manager API. The CSI
driver is a thin protocol translation layer: Kubernetes CSI gRPC ↔ Manager HTTP.

This document assumes the Aloelite Volume Manager is already running and
accessible on each node.

---

## Architecture

```
Kubernetes control plane
  └─ PVC created with storageClassName: aloelite
       └─ external-provisioner sidecar calls CreateVolume (Controller)
            └─ CSI Controller → POST /volumes (Manager API)

Pod scheduled on node
  └─ kubelet calls NodeStageVolume / NodePublishVolume (Node)
       └─ CSI Node → POST /volumes/<id>/mount (Manager API)
            └─ bind mount /mnt/aloelite/<id> → pod volume path
```

### Process model

The CSI driver is a single Python process exposing a Unix domain socket at
`/var/lib/kubelet/plugins/aloelite.csi/csi.sock`. It implements all three
CSI services:

- **Identity** — driver name, capabilities, health probe
- **Controller** — CreateVolume, DeleteVolume, ListVolumes
- **Node** — NodeStageVolume, NodePublishVolume, NodeUnpublishVolume,
  NodeUnstageVolume, NodeGetCapabilities

Kubernetes CSI sidecars (standard containers, unmodified) handle the
Kubernetes-side protocol:

| Sidecar | Responsibility |
|---|---|
| `external-provisioner` | Watches PVCs, calls Controller CreateVolume/DeleteVolume |
| `node-driver-registrar` | Registers the socket with kubelet |
| `livenessprobe` | Probes the Identity service health endpoint |

The driver and sidecars run together in a DaemonSet pod on every node.

---

## Source Dependencies

- Aloelite Volume Manager (running, reachable via HTTP on the same node)
- CSI spec proto files (`csi.proto` from `github.com/container-storage-interface/spec`)
- `grpcio` + `grpcio-tools` for the gRPC server
- `requests` (or `httpx`) for Manager API calls
- Standard Kubernetes manifests (no SDK required for the driver itself)

---

## File Layout

```
aloelite-py/
  csi/
    __init__.py
    driver.py        # gRPC server entry point, wires up all three servicers
    identity.py      # CSI Identity service
    controller.py    # CSI Controller service
    node.py          # CSI Node service (bind mounts)
    manager.py       # Manager API client (thin requests wrapper)
    config.py        # Config from env vars / StorageClass parameters
    proto/           # Generated from csi.proto; committed, not generated at build time
      csi_pb2.py
      csi_pb2_grpc.py

  deploy/
    csi-driver.yaml          # CSIDriver object
    daemonset.yaml           # DaemonSet: driver + sidecars
    storageclass.yaml        # StorageClass: aloelite
    rbac.yaml                # ClusterRole / ClusterRoleBinding for sidecars
```

---

## Configuration

All configuration is injected via environment variables in the DaemonSet.

| Variable | Default | Description |
|---|---|---|
| `MANAGER_URL` | `http://localhost:8080` | Manager API base URL |
| `CSI_SOCKET` | `/var/lib/kubelet/plugins/aloelite.csi/csi.sock` | Unix socket path |
| `DRIVER_NAME` | `aloelite.csi` | Must match CSIDriver object and StorageClass |
| `NODE_ID` | (required) | Injected from `spec.nodeName` via downward API |
| `MAX_VOLUMES_PER_NODE` | `0` (unlimited) | Enforced in NodeGetCapabilities |
| `DEFAULT_ENCRYPTED` | `false` | Default for volumes without explicit parameter |

### StorageClass parameters

Parameters are passed through from the StorageClass (and overridable per PVC
via annotations if desired):

| Parameter | Values | Description |
|---|---|---|
| `encrypted` | `"true"` / `"false"` | Whether to create an encrypted volume |
| `pinSecretName` | secret name | Kubernetes Secret in the PVC namespace holding the PIN |
| `pinSecretKey` | key in secret | Key within that secret; defaults to `"pin"` |

PIN resolution: the Controller fetches the named Secret from the Kubernetes API
at CreateVolume time, extracts the PIN, and passes it to the Manager. The PIN
is never stored in the driver or in volume metadata.

---

## CSI Identity Service

```
GetPluginInfo     → { name: DRIVER_NAME, vendor_version: "0.1.0" }
GetPluginCapabilities → CONTROLLER_SERVICE
Probe             → GET /volumes (Manager API); ready if 200
```

---

## CSI Controller Service

### CreateVolume

Called by `external-provisioner` when a PVC is created.

1. Extract `name`, `capacity_range`, `parameters` from the request.
2. Resolve PIN if `encrypted=true` (fetch Secret from Kubernetes API).
3. `POST /volumes` → Manager API with `{ name, encrypted, pin }`.
4. Store volume ID in response `volume_id` (used in all subsequent calls).
5. Return `CreateVolumeResponse` with `volume_id` and `volume_context`
   (passes `encrypted` flag through to Node for informational purposes).

Capacity: Aloelite volumes are not pre-sized (SQLite grows on demand).
Return the requested capacity as nominal; actual usage is uncapped unless
the node enforces quotas (out of scope for initial implementation).

### DeleteVolume

1. `DELETE /volumes/<id>` → Manager API (unmounts first if needed).
2. Return success. Idempotent: 404 from Manager is treated as success.

### ListVolumes

1. `GET /volumes` → Manager API.
2. Map each record to a `ListVolumesResponse.Entry`.

### Controller capabilities declared:

`CREATE_DELETE_VOLUME`, `LIST_VOLUMES`

---

## CSI Node Service

The Node service runs on every node and performs the actual mount/unmount
into pod paths. It assumes the Manager is running on the same node.

### NodeStageVolume

"Stage" in CSI means preparing a volume at a node-global path before
bind-mounting into individual pods. For Aloelite this maps to mounting
the volume via the Manager.

1. `POST /volumes/<id>/mount` → Manager API (with PIN if encrypted;
   PIN is passed through `volume_context` from CreateVolume, resolved
   at stage time from the Secret if needed).
2. Wait for Manager to confirm readiness (Manager handles this internally;
   201/200 response means ready).
3. The staging path (`staging_target_path`) is `/mnt/aloelite/<id>` —
   already managed by the Manager. No additional action needed.

### NodePublishVolume

Bind-mounts the staged path into the pod's volume path.

```python
import subprocess
os.makedirs(target_path, exist_ok=True)
subprocess.run(
    ["mount", "--bind", staging_target_path, target_path],
    check=True
)
```

If `read_only` is requested, add `-o remount,ro` after the bind mount.

### NodeUnpublishVolume

```python
subprocess.run(["umount", target_path], check=True)
```

Idempotent: ignore `EINVAL` / "not mounted" errors.

### NodeUnstageVolume

`DELETE /volumes/<id>/mount` → Manager API. Idempotent: 404 is success.

### Node capabilities declared:

`STAGE_UNSTAGE_VOLUME`

---

## Manager API Client (`manager.py`)

A thin wrapper around `requests.Session` with:

- Base URL from config
- Retry with backoff on connection errors (3 attempts, exponential)
- Timeout: 30s for mount operations, 5s for all others
- Raises typed exceptions (`ManagerError`, `VolumeNotFound`, `MountFailed`)
  that the Controller and Node servicers catch and map to gRPC status codes

```python
class ManagerClient:
    def create_volume(self, name, encrypted, pin=None) -> dict: ...
    def delete_volume(self, volume_id) -> None: ...
    def list_volumes(self) -> list[dict]: ...
    def mount_volume(self, volume_id, pin=None) -> dict: ...
    def unmount_volume(self, volume_id) -> None: ...
    def stat_volume(self, volume_id) -> dict: ...
```

---

## gRPC Error Mapping

| Manager response | gRPC status code |
|---|---|
| 404 | `NOT_FOUND` |
| 409 (already mounted) | `ALREADY_EXISTS` |
| 400 (bad PIN) | `INVALID_ARGUMENT` |
| 503 (mount timeout) | `UNAVAILABLE` |
| 500 | `INTERNAL` |
| Connection error | `UNAVAILABLE` |

---

## Kubernetes Manifests

### CSIDriver object

```yaml
apiVersion: storage.k8s.io/v1
kind: CSIDriver
metadata:
  name: aloelite.csi
spec:
  attachRequired: false
  podInfoOnMount: false
  volumeLifecycleModes:
    - Persistent
```

`attachRequired: false` — no ControllerPublishVolume/UnpublishVolume needed
since the Manager handles mount state directly.

### StorageClass

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: aloelite
provisioner: aloelite.csi
reclaimPolicy: Delete
volumeBindingMode: WaitForFirstConsumer
parameters:
  encrypted: "false"
```

`WaitForFirstConsumer` ensures the volume is created on the node where the
pod will run, which is required since Manager and storage are node-local.

### DaemonSet (abbreviated)

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: aloelite-csi
  namespace: kube-system
spec:
  selector:
    matchLabels:
      app: aloelite-csi
  template:
    spec:
      hostNetwork: true
      containers:

        - name: csi-driver
          image: aloelite-csi:latest
          securityContext:
            privileged: true
          env:
            - name: MANAGER_URL
              value: "http://localhost:8080"
            - name: NODE_ID
              valueFrom:
                fieldRef:
                  fieldPath: spec.nodeName
          volumeMounts:
            - name: socket-dir
              mountPath: /var/lib/kubelet/plugins/aloelite.csi
            - name: mountpoint-dir
              mountPath: /var/lib/kubelet/pods
              mountPropagation: Bidirectional

        - name: node-driver-registrar
          image: registry.k8s.io/sig-storage/csi-node-driver-registrar:v2.10.0
          args:
            - --csi-address=/var/lib/kubelet/plugins/aloelite.csi/csi.sock
            - --kubelet-registration-path=/var/lib/kubelet/plugins/aloelite.csi/csi.sock

        - name: external-provisioner
          image: registry.k8s.io/sig-storage/csi-provisioner:v4.0.0
          args:
            - --csi-address=/var/lib/kubelet/plugins/aloelite.csi/csi.sock
            - --leader-election=false

        - name: livenessprobe
          image: registry.k8s.io/sig-storage/livenessprobe:v2.12.0
          args:
            - --csi-address=/var/lib/kubelet/plugins/aloelite.csi/csi.sock

      volumes:
        - name: socket-dir
          hostPath:
            path: /var/lib/kubelet/plugins/aloelite.csi
            type: DirectoryOrCreate
        - name: mountpoint-dir
          hostPath:
            path: /var/lib/kubelet/pods
```

`Bidirectional` mount propagation on the pod mountpoint directory is what
allows bind mounts created inside the CSI container to be visible to the
kubelet and to pods.

---

## Preflight / Startup Checks

On driver startup, before the gRPC server begins accepting requests:

| Check | How | Fatal? |
|---|---|---|
| Manager reachable | `GET /volumes`; expect 200 | Yes |
| Socket directory writable | `os.access(socket_dir, os.W_OK)` | Yes |
| `mount` binary present | `shutil.which("mount")` | Yes |
| `umount` binary present | `shutil.which("umount")` | Yes |
| `NODE_ID` set | env var present and non-empty | Yes |

---

## PIN Security Notes

- PINs are fetched from Kubernetes Secrets at CreateVolume / NodeStageVolume time.
- PINs are passed in-memory to the Manager API over localhost HTTP; they are
  never written to disk by the CSI driver.
- PINs are not stored in PV/PVC annotations or in CSI volume context passed
  to the Node (volume context carries only `encrypted: "true"` as a flag;
  the Node re-fetches the PIN from the Secret at stage time).
- The CSI driver requires `get` permission on Secrets in provisioned namespaces.

---

## RBAC

The CSI driver needs the following permissions (applied via ClusterRole):

```yaml
rules:
  - apiGroups: [""]
    resources: ["persistentvolumes"]
    verbs: ["get", "list", "watch", "create", "delete"]
  - apiGroups: [""]
    resources: ["persistentvolumeclaims"]
    verbs: ["get", "list", "watch", "update"]
  - apiGroups: ["storage.k8s.io"]
    resources: ["storageclasses"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["storage.k8s.io"]
    resources: ["csinodes"]
    verbs: ["get", "list", "watch"]
  - apiGroups: [""]
    resources: ["secrets"]
    verbs: ["get"]
  - apiGroups: [""]
    resources: ["nodes"]
    verbs: ["get", "list", "watch"]
```

---

## What This Specification Defines

- Driver architecture (single process, three CSI services, sidecar pattern)
- Complete Controller and Node service behaviour
- Manager API client interface
- PIN resolution via Kubernetes Secrets
- gRPC error mapping
- All required Kubernetes manifests (CSIDriver, StorageClass, DaemonSet, RBAC)
- Startup preflight checks
- File layout

This document is sufficient to drive implementation from a fresh context.
The Manager spec (`aloelite_volume_manager_spec.md`) is a prerequisite.