# k3s KVM demo

A kiosk dashboard for a project booth. Every k3s node is a libvirt guest shown
as a card; visitors deploy a node or kill one and watch the cluster heal.

![dashboard](dashboard.png)

Deploy a control plane node, deploy some workers, then kill one and watch the
workload move. Kill a control plane node from a three-server cluster and watch
`kubectl` keep answering. Kill a second and watch it stop — that one is worth
showing too.

- Backend: FastAPI + `libvirt-python`, managed with `uv`.
- Frontend: server-rendered HTML and htmx. No build step, no CDN.
- **No database.** libvirt's own domain metadata is the only store, so the
  dashboard rediscovers everything on restart.

> **This dashboard is not authenticated.** It destroys VMs and runs commands as
> root inside guests. It binds loopback only — a non-loopback `bind` is a
> startup error, not a warning — and rejects cross-origin requests. Run it on
> the booth machine behind a local kiosk browser and nowhere else.

## Requirements

On the booth machine:

```bash
# Fedora / RHEL
sudo dnf install libvirt libvirt-devel gcc pkgconf-pkg-config python3-devel qemu-img
# openSUSE
sudo zypper install libvirt libvirt-devel gcc pkg-config python3-devel qemu-tools

sudo usermod -aG libvirt "$USER"     # log out and back in
```

`libvirt-python` is published only as a source distribution, so it compiles
against your system libvirt at install time — hence the headers and compiler.
There is no version to pin: it generates its bindings from whatever libvirt is
installed.

```bash
uv sync
```

Do this **before** the event. Wheels for everything else come from the lock
file, but the libvirt binding still needs to build, and a booth network is not
where you want to discover that.

## The golden image

The dashboard boots a qcow2 you supply and never modifies it. It must have:

1. **`k3s` on `PATH`** — from the openSUSE RPM, the upstream installer, or
   anywhere else. The app never checks the version and has no opinion about it.
2. **`qemu-guest-agent` installed and enabled.** This is the only channel into
   the guest; without it a node never gets past `booting`.
3. **No enabled k3s unit.** `k3s.service`, `k3s-server.service` and
   `k3s-agent.service` must all be disabled — firstboot writes and enables its
   own `k3s-node.service`, and a packaged unit would race it. Firstboot checks
   and refuses.
4. **No `/var/lib/rancher/k3s`.** A pre-existing cluster data directory means
   the image was booted into a cluster once; firstboot refuses.
5. **No `/etc/machine-id`.** Otherwise every clone shares a DHCP client
   identity and they fight over leases. openSUSE's Minimal VM images already
   clear it.

`scripts/build-image.sh` produces one from openSUSE Tumbleweed Minimal VM
(the `-Cloud` flavour, which ships both `qemu-guest-agent` and `cloud-init`).
Run it off-site, on a machine with a real network.

Because k3s is baked in, provisioning a node at the booth touches the network
not at all.

## Setup

```bash
cp config.example.toml config.toml
$EDITOR config.toml                  # base_image, token

uv run k3s-demo init-pool            # creates a dedicated storage pool
uv run k3s-demo check                # validates config against the hypervisor
uv run k3s-demo serve
```

Then point a kiosk browser at `http://127.0.0.1:8000/`.

### Why a dedicated pool

The dashboard deletes disks, so it will not run against a pool it cannot prove
is its own. `init-pool` creates the pool and writes a marker volume
(`k3s-kvm-demo.marker`); startup refuses if the marker is missing, and refuses
again if the pool holds any volume it does not recognise. It never deletes to
resolve that — it stops and tells you.

Keep `base_image` **outside** the pool. Everything inside is deletable.

## Using it

| Button | What it does |
| --- | --- |
| **Deploy Node** | A worker. Refused until a control plane node is `configured`. |
| **Deploy Control Plane node** | The first one bootstraps with `cluster-init`; later ones join as embedded-etcd members. |
| **Kill node** | `destroy` then delete the overlay. Ungraceful on purpose — that is the demo. |
| **Prune Nodes** | Deletes Kubernetes `Node` objects for VMs that no longer exist. See the limitation below. |
| **Reset** | Destroys every demo VM and disk. The between-demos button, and the way back from a broken cluster. |

### Reading a card

Two independent things are shown, because conflating them loses information:

- **Provisioning state** — `creating`, `booting`, `configuring`, `configured`,
  `failed`. Durable: it survives a restart of the app, of libvirtd, and of the
  host. A failure keeps its reason.
- **Power and service** — `running` / `shut off`, and whether the k3s unit is
  active. Observed fresh each time, never written down.

So "configured · shut off" is a node that provisioned fine and is currently
powered off — after a host reboot, for instance. It is kept, not deleted.

`configured` means firstboot enabled the unit and it came up. It does **not**
mean Kubernetes considers the node `Ready`; check that with `kubectl`.

## Known limitations

**Killed control plane nodes leave etcd members behind.** k3s does not remove
them, and deleting the Kubernetes `Node` does not reliably remove them either —
etcd member names carry their own random suffix. So they accumulate and count
toward quorum, `(n/2)+1`. Prune Nodes tidies the Kubernetes side only and says
so; **Reset** is the way back from a cluster that has lost quorum. If you want
to clean up members by hand on a cluster that still *has* quorum, from a
running server:

```bash
etcdctl --cert /var/lib/rancher/k3s/server/tls/etcd/client.crt \
        --key  /var/lib/rancher/k3s/server/tls/etcd/client.key \
        --cacert /var/lib/rancher/k3s/server/tls/etcd/server-ca.crt \
        --endpoints https://127.0.0.1:2379 member list
etcdctl ... member remove <ID>
```

**The quorum banner is advisory.** It counts the VMs managed here, which cannot
see those stale members, so it can understate the real member count. Nothing is
ever refused on the strength of it: breaking quorum is a demo people ask for.

**Agents can strand during rolling server outages** ([k3s #11349]). Agents
persist the apiserver list and normally survive losing the server they joined
through, but an address pruned during an outage is not restored. The remedy is
to delete `/var/lib/rancher/k3s/agent/etc/k3s-agent-load-balancer.json` and
restart k3s on the affected node.

**A timed-out firstboot may leave a process running in the guest.** The guest
agent has no kill primitive; the app makes a best-effort `pkill` and then marks
the node `failed`. Kill the node.

**One process only.** The app holds a whole-process lock; a second instance
refuses to start. Run `uvicorn` with a single worker (`k3s-demo serve` does).

[k3s #11349]: https://github.com/k3s-io/k3s/issues/11349

## Running as a service

`contrib/k3s-demo.service` is a starting point. The service account needs write
access to `qemu:///system`, which usually means the `libvirt` group.

`GET /healthz` reports the connection, node count, in-flight work, whether the
pool marker is present, and any unclassified or orphaned volumes.

## Development

```bash
uv run pytest
```

The suite runs libvirt for real through its built-in `test:///default` driver —
volumes with backing stores, define/start/destroy, metadata, lease addresses.
Only the QEMU guest agent is faked, because it cannot exist without a VM. No
test touches `qemu:///system`.

Where things live:

| File | Responsibility |
| --- | --- |
| `config.py` | TOML loading and validation; every error names its key |
| `meta.py` | Durable state in domain metadata; the only caller of `setMetadata` |
| `domxml.py` | Domain and volume XML, built with ElementTree |
| `conn.py` | The singleton lock and leased libvirt connections |
| `pool.py` | Pool ownership, volume classification, orphan eligibility |
| `libvirtctl.py` | The create saga, idempotent deletion, the state × power table |
| `guestexec.py` | The guest-agent protocol and its real implementation |
| `k3sconf.py` | The two files firstboot writes, and rendering the script |
| `provision.py` | The provisioning state machine and reconciliation |
| `cluster.py` | Kubernetes-side housekeeping |
| `stats.py` | Where live CPU/RAM and real kubelet status go next |

### Adding metrics

`stats.enrich` is called on every listing and currently attaches nothing. Its
docstring names exactly what goes in: `getAllDomainStats` for CPU and memory,
a kubeconfig-backed `/api/v1/nodes` read for kubelet status. The card template
already renders each field only when it is not `None`, so both land as pure
additions.

## Before the event

Some things only a real hypervisor and a real image can tell you. Work through
these once with the actual golden image:

1. Deploy a control plane node and two workers; `kubectl get nodes` shows three
   `Ready`.
2. Kill a worker: the card disappears, the domain and its overlay are gone, and
   its workload reschedules.
3. Three control plane nodes; kill one — `kubectl` still answers. Kill a second
   — it stops. Reset and rebuild.
4. Restart `virtqemud` with a node mid-`configuring`: the state survives.
5. `kill -9` the dashboard mid-`configuring` and restart it: the node resolves
   to `configured` or `failed`, never a stuck card.
6. Reboot the host: nodes come back "configured · shut off". Nothing is
   destroyed.
7. Kill and replace control plane nodes several times: no `duplicate node name`
   errors. Etcd members accumulate — confirm Reset clears them.
8. Unplug the network and deploy a node: the UI and the k3s join both still
   work.
