# k3s KVM demo

A kiosk dashboard for a project booth. Every k3s node is a libvirt guest shown
as a card; visitors deploy a node or kill one and watch the cluster heal.

![dashboard](dashboard.png)

Deploy a control plane node, deploy some workers, then kill one and watch the
workload move. Kill a control plane node from a three-server cluster and watch
`kubectl` keep answering. Kill a second to demonstrate quorum loss.

- Backend: FastAPI + `libvirt-python`, managed with `uv`.
- Frontend: server-rendered HTML and htmx
- **No database.** libvirt's own domain metadata is the only store, so the
  dashboard rediscovers everything on restart.

> **This dashboard is not authenticated.** It destroys VMs and runs commands as
> root inside guests. It binds loopback only. A non-loopback `bind` is a
> startup error, and it rejects cross-origin requests. Run it on
> the booth machine behind a local kiosk browser and nowhere else.

## Requirements

On the booth machine:

```bash
# Fedora / RHEL
sudo dnf install libvirt libvirt-devel gcc pkgconf-pkg-config python3-devel qemu-img xorriso
# openSUSE
sudo zypper install libvirt libvirt-devel gcc pkg-config python3-devel qemu-tools xorriso

sudo usermod -aG libvirt "$USER"     # log out and back in

uv sync
```

The host also needs an active libvirt network with DHCP and working KVM.
`xorriso` creates each node's seed ISO on the host before the VM starts.

## The golden image

> **Guest boot remains unverified.** The opt-in VM test has not been run against
> the v2 image. Collection and mocked tests do not verify guest boot or systemd behavior.

The dashboard boots a standalone qcow2 image you supply and never modifies it.
The golden image must have no backing file of its own. A node overlay is not a
valid golden image. The v2 guest-image contract requires:

1. Install `k3s` on the system `PATH`. The dashboard does not select or validate
   its version.
2. Install and enable `qemu-guest-agent.service`. The QEMU guest agent (QGA)
   reports guest status.
3. Install `firstboot/k3s-demo-guest` as `/usr/local/libexec/k3s-demo-guest`,
   owned by root, with mode `0755`.
4. Install `firstboot/k3s-demo-prepare.service` and `firstboot/k3s-node.service`
   in `/etc/systemd/system`, with mode `0644`.
5. Enable `k3s-node.service`. Its dependency starts `k3s-demo-prepare.service`,
   which does not need separate enablement.
6. Disable and mask packaged `k3s.service`, `k3s-server.service`, and
   `k3s-agent.service` to prevent competing services.
7. Remove `/var/lib/rancher/k3s`, `/etc/rancher/k3s/config.yaml`, and
   `/var/lib/k3s-kvm-demo` from the image.
8. Leave `/etc/machine-id` empty. Remove `/var/lib/dbus/machine-id` and
   `/var/lib/systemd/random-seed` before cloning.
9. Provide `/bin/sh`, systemd, coreutils, and util-linux tools, including
   `flock`, `mount`, and `timeout`.
10. `qemu-guest-agent` must be configured to allow remote command execution,
    which is disallowed by default on openSUSE. Ensure that
    `guest-exec,guest-exec-status` are not filtered qemu-guest agent RPC
    arguments, e.g. by providing an `/etc/sysconfig/qemu-ga` with
    `FILTER_RPC_ARGS=""`

An empty `/etc/machine-id` is accepted and preferred over a missing file.
Ordinary systemd startup, through PID 1, generates a fresh machine ID for each clone.
The empty file avoids `ConditionFirstBoot=yes`, interactive first-boot setup, and first-boot presets that could change the enabled units.
Guest preparation does not generate the machine ID or invoke `systemd-firstboot`.
Preparation does not derive IDs from hashes or install custom udev rules.
Disable cloud-init if the image includes it: this deployment supplies no cloud-init datasource.

### Building a v2 image

`scripts/build-image.sh` builds from openSUSE Tumbleweed Minimal VM, using the
`-Cloud` flavor. Run it before the event on a machine with Internet access.
The build requires `curl`, `qemu-img`, and `virt-customize` from the distribution's libguestfs tools package.

**Choose a new output path. Never overwrite an image that backs existing overlays.**
The builder defaults to `/var/lib/libvirt/images/k3s-base-v2.qcow2`.
It rejects an existing output, including a symlink, and atomically publishes the completed image without replacing an existing path.
For later rebuilds, set `OUTPUT` to another unused path.
Run this example from the repository root:

```bash
OUTPUT=/var/lib/libvirt/images/k3s-base-v2.qcow2 bash scripts/build-image.sh
```

The output directory must already exist and be writable and searchable by the build account.
The builder does not create it. Publication requires a filesystem that supports hard links.
Inspect the result with `qemu-img info`: the format must be `qcow2`, with no backing file.
`virt-cat -a /var/lib/libvirt/images/k3s-base-v2.qcow2 /etc/machine-id` must produce empty output.
Set `vm.base_image` to the new path after the build succeeds.
Do not boot the golden image into a cluster before cloning it.
Provisioning needs the local VM network, but no Internet access to install k3s.
Workloads also need locally available container images for an offline demonstration.

### Guest-owned provisioning

Each VM has two owned volumes: a writable qcow2 overlay and a seed ISO.
The host uses `xorriso` to build the ISO with the label `K3SDEMO` and attaches it as a read-only virtio disk.
The seed contains node identity, role, hostname, and k3s configuration, including the join token.
Treat both volumes and their backups as sensitive. Do not publish seed files or use production credentials for this demo.

The two unit files are installed in the image and are not rendered per node.
`k3s-demo-guest prepare` mounts the seed read-only, installs the configuration,
and commits a durable `prepared` marker after synchronizing the files.
Repeated preparation validates the installed state rather than replacing it.
`k3s-demo-guest run` starts k3s with the installed role and configuration.
After k3s sends its systemd readiness notification, `mark-started` records durable startup success.
`status` reports that history and the current systemd states without changing guest configuration.

The dashboard uses QGA for bounded status commands, kubeconfig export, and explicit **Prune Nodes** operations.
It does not inject provisioning scripts or start k3s through QGA.
Guest preparation continues if the dashboard or QGA becomes unavailable.

### Upgrading from the old image

Legacy or unsupported domain metadata blocks startup and mutations. The dashboard never resets or converts it automatically.
There is no live adoption of old guests into v2.

1. Decide whether to discard the old demo cluster. Preserve required data before choosing Reset.
2. If you choose to discard it, use the old dashboard version and configuration to Reset before upgrading.
3. Stop the old dashboard, then build the v2 image at a new path as described above.
4. Update the application and configuration together. Replace `[firstboot]` with `[observation]` from `config.example.toml`.
5. Remove obsolete maintenance settings. Keep `shutdown_grace_s`, and add `server.lock_path` from the example.
6. Validate the configuration and deploy new guests.

If you keep legacy guests, stop here: v2 remains blocked while their metadata exists on the same hypervisor connection.
Use a separate test host for v2 rather than changing their metadata by hand.
Once v2 domains exist, binary-only rollback to the old dashboard is unsupported.
Returning to the old version requires an explicit teardown with the v2 tools and a separate rebuild, not metadata editing.
Keep the old backing image unchanged for as long as any overlay references it.

## Setup

Run the following commands from the repository root:

```bash
cp config.example.toml config.toml
$EDITOR config.toml                  # base_image, token

# For manual operation only, provision the runtime directory for this account.
# Do not run this against a directory used by a running service account.
sudo install -d -m 0750 -o "$USER" -g "$(id -gn)" /run/k3s-kvm-demo

uv run k3s-demo init-pool            # creates a dedicated storage pool
uv run k3s-demo check                # validates config against the hypervisor
uv run k3s-demo serve
```

Then point a kiosk browser at `http://127.0.0.1:8000/`.

The CLI selects configuration in this order: `--config`, `K3S_DEMO_CONFIG`, then `config.toml` in the current directory.
Place the option before the subcommand, for example `uv run k3s-demo --config /etc/k3s-kvm-demo/config.toml check`.
A relative `vm.base_image` resolves from the process's working directory, not the configuration file's directory.
Use an absolute image path so service and manual launches select the same file.
`server.lock_path` must be absolute. Use an absolute directory with `init-pool --path` if overriding the default pool location.

### Why a dedicated pool

The dashboard deletes disks, so it will not run against a pool it cannot prove
is its own. `init-pool` creates the pool and writes a marker volume
(`k3s-kvm-demo.marker`); startup refuses if the marker is missing, and refuses
again if the pool contains any volume it does not recognize. It never deletes to
resolve that. It stops and reports the problem.

Keep `base_image` **outside** the pool. The pool contains the ownership marker,
seed disks, and overlays. Kill and Reset delete both volumes claimed by each node.
Ownership is scoped by pool UUID and name prefix, with domain UUID-based volume names.
Unclaimed volumes are reported for operator inspection, not automatically reaped.
An unclaimed-volume report is diagnostic only. Hidden backing references can still make deletion unsafe.

Deletion checks disk references across the hypervisor connection, including unrelated guests' live and persistent definitions.
Visible references to either claimed volume block deletion, including references in incomplete backing chains.
Complete backing-chain evidence is required only for other nodes managed by this deployment, identified through metadata, pool UUID, and name prefix.
Accepted evidence includes known raw disks, read-only CD-ROM devices, or explicit qcow2 chains with a terminating `<backingStore/>`.
The v2 domain XML contract records each overlay's standalone qcow2 base and explicitly terminates that backing chain.
This lets multiple demo nodes share the golden image while retaining separate owned overlays and seed disks.

An unrelated qcow2 disk with only a source path does not block Kill, Reset, or recovery cleanup solely because its backing chain is incomplete.
The dashboard does not read foreign image files directly or require permission to read them.
Unreadable domain XML, unsupported disk-source syntax, and incompatible application metadata still block deletion.

The demo pool is exclusively for this deployment.
Other guests and externally managed images must not attach its volumes or use them as backing images.
Hidden foreign backing dependencies are not detected, so violating this requirement can damage those guests during cleanup.
Administrators must not attach or rebase images onto demo volumes while deletion runs.
Do not add an empty backing-chain terminator unless the image is verified to have no further backing file.

## Using it

| Button | What it does |
| --- | --- |
| **Deploy Node** | A worker. Requires a configured, running server with an IP address and fresh successful guest status. |
| **Deploy Control Plane node** | The first one bootstraps with `cluster-init`; later ones join as embedded-etcd members. |
| **Kill node** | Destroys the VM, deletes its seed disk and overlay, then removes its definition. Intentionally ungraceful. |
| **Prune Nodes** | Deletes Kubernetes `Node` objects for VMs that no longer exist. See the limitation below. |
| **Reset** | Destroys demo VMs and both owned volumes in this pool/prefix scope. Use between demos or after quorum loss. |
| **Copy kubeconfig** | Opens a panel with the current export and a **Copy to clipboard** button. |
| **Download kubeconfig** | Downloads the current export as `k3s-demo.yaml`. |

### Exporting kubeconfig

To use host-side `kubectl`, export credentials from a running, configured control plane node with an IP address:

```bash
uv run k3s-demo export --output ./k3s-demo.yaml
kubectl --kubeconfig ./k3s-demo.yaml get nodes
```

Without `--output`, the command writes kubeconfig to standard output.
File output uses permissions `0600` and refuses an existing path.
The command works with the dashboard running or stopped and does not change `~/.kube/config`.
Use the same configuration as the dashboard, with `--config` before `export` if needed.

The dashboard offers **Download kubeconfig** and **Copy kubeconfig**.
After opening the copy panel, click **Copy to clipboard**.
If clipboard access is unavailable, copy the selected text manually or use the download button.
Browser downloads use browser-managed file permissions. Keep the downloaded file private.

The export grants administrator access to the demo cluster. Do not publish it.
It points to one VM's IP address on the local libvirt network and does not update automatically.
Export again when you need a fresh configuration.

### Reading a card

Provisioning state is durable: `creating`, `booting`, `configuring`, `configured`,
`failed`, or `deleting`. It survives dashboard and host restarts.
Power state and current k3s service state are separate observations.

`configured` means the guest recorded that k3s started successfully at least once.
It remains configured if k3s later stops or fails. It does **not** mean the
service is currently active or Kubernetes considers the node `Ready`.
Check Kubernetes readiness with `kubectl`.

A configured, shut-off guest is retained, for example after a host reboot.
Missing or stale QGA evidence makes service status unknown and prevents new joins through that server.
It does not erase startup history, rerun provisioning, or delete the guest.
Observation defaults are a 2 s interval, a 2 s QGA call timeout, and a 30 s freshness limit.
All three values must be positive integers, and `stale_after_s` must be greater than `interval_s`.

## Known limitations

**Killed control plane nodes leave etcd members behind.** k3s does not remove
them, and deleting the Kubernetes `Node` does not reliably remove them either.
etcd member names include a random suffix. Stale members accumulate and count
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
see those stale members, so it can understate the member count. Nothing is
ever refused on the strength of it: breaking quorum is a demo people ask for.

**Agents can strand during rolling server outages** ([k3s #11349]). Agents
persist the apiserver list and normally survive losing the server they joined
through, but an address pruned during an outage is not restored. The remedy is
to delete `/var/lib/rancher/k3s/agent/etc/k3s-agent-load-balancer.json` and
restart k3s on the affected node.

**Unknown guest status is not a provisioning timeout.** Inspect the guest console
and `journalctl -u k3s-demo-prepare.service -u k3s-node.service` when status does not recover.
The dashboard does not inject retries or kill guest provisioning processes.
Kill and redeploy a failed demo guest when appropriate.

**One process only.** The app holds a whole-process lock; a second instance
refuses to start. Run `uvicorn` with a single worker (`k3s-demo serve` does).

[k3s #11349]: https://github.com/k3s-io/k3s/issues/11349

## Running as a service

`contrib/k3s-demo.service` uses the `k3sdemo` account and an installation at `/opt/k3s-kvm-demo`.
The account needs write access to `qemu:///system`, usually through the `libvirt` group.
Install dependencies there with `uv sync --frozen` before starting the service.
The service executes `/opt/k3s-kvm-demo/.venv/bin/k3s-demo` directly.
It does not download packages or depend on a user-specific uv cache at startup.

Install the configuration at `/etc/k3s-kvm-demo/config.toml`, readable by the service account but not other users.
Keep `server.lock_path = "/run/k3s-kvm-demo/dashboard.lock"` for both service and manual launches.
The lock path is absolute and does not depend on the current directory or `XDG_RUNTIME_DIR`.
Do not run simultaneous instances with different lock paths.

`RuntimeDirectory=k3s-kvm-demo` creates the service's runtime directory with the service account's ownership.
Manual launches need a precreated writable directory, as shown in Setup, or an administrator-managed `systemd-tmpfiles` rule.
Because `/run` is temporary, manual directory setup must survive or be repeated after a reboot.
The application does not change directory ownership or host security policy automatically.
No lock-path environment variable is needed.

The unit retains `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`,
and `NoNewPrivileges=yes`. Keep images outside home directories and provision
libvirt access and host security labels explicitly. Do not disable these protections to work around setup errors.

`GET /healthz` reports the connection, node count, in-flight work, whether the
pool marker is present, and any unclassified or orphaned volumes.

## Development

```bash
uv run pytest
```

The default suite exercises libvirt through its built-in `test:///default` driver:
volumes with backing stores, define/start/destroy, metadata, lease addresses.
Guest-agent responses and seed upload are substituted where a running VM is required.
The default suite does not touch `qemu:///system`.
The separately enabled tests in [tests/integration](tests/integration/Readme.md)
boot a server and an agent using a supplied v2 image and a dedicated empty pool directory.
They are skipped unless `K3S_DEMO_VM_TESTS=1` is set. Read their resource and cleanup requirements before enabling them.

Where things live:

| File | Responsibility |
| --- | --- |
| `config.py` | TOML loading and validation; every error names its key |
| `meta.py` | Durable state in domain metadata; the only caller of `setMetadata` |
| `domxml.py` | Domain and volume XML, built with ElementTree |
| `conn.py` | The singleton lock and leased libvirt connections |
| `pool.py` | Pool ownership, volume classification, disk-reference checks |
| `libvirtctl.py` | Two-volume creation, scoped ownership, idempotent deletion |
| `guestexec.py` | The guest-agent protocol and QGA implementation |
| `k3sconf.py` | Per-node k3s configuration |
| `seed.py` | Seed payload, ISO creation, and libvirt upload |
| `observer.py` | Bounded guest status polling, durable startup history, fresh join evidence |
| `firstboot/` | Guest preparation command and the two installed systemd units |
| `cluster.py` | Kubernetes-side housekeeping and kubeconfig export |
| `stats.py` | Extension point for resource metrics and Kubernetes readiness |

### Adding metrics

Keep resource metrics separate from durable provisioning history and current guest service status.
Kubernetes `Ready` needs Kubernetes API evidence. Do not infer it from an active systemd unit.
`stats.enrich` is currently a no-op. Extend the existing `getAllDomainStats` call
for CPU and memory metrics, and use a Kubernetes API read for node readiness.
The card template renders optional metric fields when they are available.

## Before the event

Complete these checks on the booth hypervisor with the golden image intended for the event:

1. Deploy a control plane node and two workers; `kubectl get nodes` shows three
   `Ready`.
2. Kill a worker: the card disappears, the domain, seed disk, and overlay are gone, and
   its workload reschedules.
3. Deploy three control plane nodes. Kill one: `kubectl` still answers. Kill a second: it stops. Reset and rebuild.
4. Restart `virtqemud` with a node mid-`configuring`: the state survives.
5. Terminate the dashboard mid-`configuring`. Verify guest preparation completes without it, then restart and confirm status recovery without reprovisioning.
6. Reboot the host: nodes come back "configured · shut off". Nothing is
   destroyed.
7. Kill and replace control plane nodes several times: no `duplicate node name`
   errors. Etcd members accumulate. Confirm Reset clears them.
8. Disconnect external networking but retain the libvirt network. Deploy a node and confirm the UI and k3s join still work.
9. Stop k3s inside a configured guest. Confirm startup history remains configured while current service status changes.
10. Stop QGA temporarily. Confirm service evidence becomes unknown and new joins are blocked without deleting or restarting existing guests.
11. Reboot a guest. Confirm its hostname and machine ID remain stable, and that preparation preserves the installed configuration.
12. Confirm unattended boot from an empty machine ID file. Compare two clones' generated IDs and DHCP leases for distinct values.
13. Inspect both guest units with `systemd-analyze verify` in the built image. Confirm readiness notification precedes the `started` marker.
14. Test interrupted creation and deletion on a disposable host. Confirm recovery deletes only volumes with matching ownership claims.
15. Check legacy metadata on a disposable host. Confirm v2 refuses startup without changing domains or automatically resetting anything.
16. Check deletion with multiple demo nodes sharing the standalone base. Confirm an unrelated source-only qcow2 definition does not block cleanup.
17. On a disposable host, confirm visible foreign references and incomplete backing evidence in managed peers block volume deletion.
