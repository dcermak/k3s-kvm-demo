# k3s KVM demo

A kiosk dashboard for a project booth. Every k3s node is a libvirt guest shown
as a card; visitors deploy a node or kill one and watch the cluster heal.

![dashboard](dashboard.png)

Deploy a control plane node, deploy some workers, then kill one and watch the
workload move. Kill a control plane node from a three-server cluster and watch
`kubectl` keep answering. Kill a second to demonstrate quorum loss.

- Backend: FastAPI + `libvirt-python`, managed with `uv`.
- Frontend: server-rendered HTML and htmx

> **This dashboard is not authenticated.** It destroys VMs and runs commands as
> root inside guests. It binds loopback only. A non-loopback `bind` is a
> startup error, and it rejects cross-origin requests. Run it on
> the booth machine behind a local kiosk browser and nowhere else.

## Requirements

The container deployment targets x86_64 MicroOS with rootful Podman and Quadlet.
The host needs working KVM, system libvirt, and an active libvirt network with DHCP.
The example uses `qemu:///system` and the `default` network.
On modular libvirt installations, activate `virtqemud.socket`, `virtstoraged.socket`, and `virtnetworkd.socket`.
For monolithic libvirt, use `libvirtd.socket` and adjust the Quadlet dependencies.

The container includes Python, the libvirt client, `qemu-img`, and `xorriso`.
Host libvirt runs the VMs and manages their disks and network.
The dashboard builds seed ISOs inside the container and uploads them through libvirt.
The kiosk browser runs on the host at `http://127.0.0.1:8000/`.

## The golden image

The dashboard boots a standalone qcow2 image you supply and never modifies it.
The golden image must have no backing file of its own. A node overlay is not a
valid golden image. The v2 guest-image contract requires:

1. Install `k3s` on the system `PATH`. The dashboard does not select or validate
   its version.
2. Install and enable `qemu-guest-agent.service`. The QEMU guest agent (QGA)
   reports guest status.
3. Install `image/k3s-demo-guest` as `/usr/local/libexec/k3s-demo-guest`,
   owned by root, with mode `0755`.
4. Install `image/k3s-demo-prepare.service` and `image/k3s-node.service`
   in `/etc/systemd/system`, with mode `0644`.
5. Enable `k3s-node.service`. Its dependency starts `k3s-demo-prepare.service`,
   which does not need separate enablement.
6. Disable and mask packaged `k3s.service`, `k3s-server.service`, and
   `k3s-agent.service` to prevent competing services.
7. Remove `/var/lib/rancher/k3s`, `/etc/rancher/k3s/config.yaml`, and
   `/var/lib/k3s-kvm-demo` from the image.
8. Arrange for each clone to generate its own machine ID. The KIWI hook writes
   `uninitialized` to `/etc/machine-id` and enables `systemd-firstboot`.
   Remove `/var/lib/systemd/random-seed` before cloning.
9. Provide `/bin/sh`, systemd, coreutils, and util-linux tools, including
   `flock`, `mount`, and `timeout`.
10. `qemu-guest-agent` must be configured to allow remote command execution,
    which is disallowed by default on openSUSE. Ensure that
    `guest-exec,guest-exec-status` are not filtered qemu-guest agent RPC
    arguments, e.g. by providing an `/etc/sysconfig/qemu-ga` with
    `FILTER_RPC_ARGS=""`


### Building a v2 image

Build `image/k3s-image.kiwi` with KIWI's boxbuild plugin before the event.
Run this command from `image/` on a machine with Internet access:

```bash
kiwi-ng system boxbuild --box=tumbleweed -- --description . --target-dir /var/tmp/kiwi/
```

Copy the completed qcow2 from `/var/tmp/kiwi/` to `k3s-image.qcow2` in the repository root.
The Containerfile bundles that file unchanged.

**Never overwrite a golden image that backs existing overlays.**
Build and extract replacements at unused paths, outside the demo pool.
Do not boot the golden image itself into a cluster.
Offline operation also requires the Kubernetes workload images to be available inside the guests.

### Guest-owned provisioning

Each VM has two owned volumes: a writable qcow2 overlay and a seed ISO.
The dashboard uses `xorriso` to build the ISO with the label `K3SDEMO` and attaches it as a read-only virtio disk.
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

## Setup

### Deploying on MicroOS

On a host with libvirt, KVM, Podman, and the `default` DHCP network ready, run
from the repository root:

```bash
cp config.example.toml config.toml
chmod 0600 config.toml
"${EDITOR:-vi}" config.toml
./scripts/deploy.sh
```

Set `cluster.token` to a private token and adjust the VM resources as needed.

The script uses `Image=` from `contrib/k3s-demo.container`. The script installs
the configuration and tmpfiles rule, extracts the golden image if absent,
initializes the pool, runs `check`, and installs and starts the container via
quadlet.

### Building the container

Run from the repository root after placing the KIWI output at `k3s-image.qcow2`:

```bash
sudo podman build -t localhost/k3s-kvm-demo:latest -f Containerfile .
```

The build uses `opensuse/tumbleweed` and installs production Python dependencies from `uv.lock`.
The running container uses the installed environment directly and does not install packages at startup.
The build context excludes local configuration, credentials, caches, and image-build output, except for the explicitly bundled qcow2.

To distribute the container, use ordinary Podman commands with your registry reference:

```bash
sudo podman tag localhost/k3s-kvm-demo:latest REGISTRY/PROJECT/k3s-kvm-demo:latest
sudo podman push REGISTRY/PROJECT/k3s-kvm-demo:latest
# On the booth host:
sudo podman pull REGISTRY/PROJECT/k3s-kvm-demo:latest
```

Use that reference for `IMAGE` below and for `Image=` in the Quadlet.

### GitHub Actions builds

The workflow in `.github/workflows/build.yml` builds the guest with KIWI boxbuild, then bundles it using the existing Containerfile.
It runs on `ubuntu-latest` for pushes to `main`, pull requests targeting `main`, and every Monday at 04:23 UTC.
Each run installs current KIWI and boxed-plugin versions.

In `dcermak/k3s-kvm-demo`, main pushes and scheduled runs publish these container tags:

- `ghcr.io/dcermak/k3s-kvm-demo:latest`
- `ghcr.io/dcermak/k3s-kvm-demo:build-<run-id>-<attempt>`

Pull requests build without publishing. Publication uses the repository's `GITHUB_TOKEN` with `packages: write` permission.
After the first push, set the GHCR package visibility to public in its package settings.

The deployment script and Quadlet use the published container. For the manual
commands below, set `IMAGE` and the Quadlet's `Image=` to the same reference.
Podman downloads a missing image when creating or running the container.

The guest image retains its fixed demo credentials and enables SSH. Use it only for demos, not production.

### Extracting the golden image

Host libvirt cannot read the qcow2 inside the container's private filesystem.
Extract it once into host storage, outside the demo pool.
These commands use the example base path; choose a new filename if it already exists.

```bash
IMAGE=ghcr.io/dcermak/k3s-kvm-demo:latest
BASE=/var/lib/libvirt/images/k3s-base-v2.qcow2
sudo podman create --name k3s-image-extract "$IMAGE"
sudo podman cp k3s-image-extract:/usr/share/k3s-kvm-demo/k3s-image.qcow2 "$BASE"
sudo podman rm k3s-image-extract
sudo chmod 0644 "$BASE"
```

The parent directory must exist. `podman cp` can overwrite files and can leave a partial file if interrupted.
After a failed copy, remove only the newly chosen partial output and retry.
Ensure host QEMU can traverse the parent directories and read the file under the host's security policy.
Container `label=disable` does not grant host QEMU access. Restore the new file's expected host label where required.
Do not recursively relabel existing libvirt storage.

### Configuring the host

Install the configuration and kubeconfig export directory rule from the repository:

```bash
sudo install -d -m 0750 /etc/k3s-kvm-demo
sudo install -m 0600 config.example.toml /etc/k3s-kvm-demo/config.toml
sudoedit /etc/k3s-kvm-demo/config.toml
sudo install -m 0644 contrib/k3s-demo-tmpfiles.conf /etc/tmpfiles.d/k3s-demo.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/k3s-demo.conf
```

Set `vm.base_image` to `BASE` and choose a private cluster token.
Use canonical absolute paths. The base-image mount must use the same path on the host and inside the container.
Keep `server.bind = "127.0.0.1"`; host networking makes that listener accessible to the kiosk browser.

The tmpfiles rule creates `/run/k3s-kvm-demo` at boot with mode `0700` for private kubeconfig export.
The container shares this directory with the host so host tools can read the exported kubeconfig.

`init-pool` only creates the libvirt storage pool. It does not create the kubeconfig export directory.
`check` and `serve` require the parent of `kubeconfig_export.path` to exist and be writable by the account that runs them.

### Initializing and checking

The following Bash array keeps the shared options visible when running the existing CLI commands:

```bash
IMAGE=ghcr.io/dcermak/k3s-kvm-demo:latest
BASE=/var/lib/libvirt/images/k3s-base-v2.qcow2
POOL=/var/lib/libvirt/images/k3s-demo
options=(
  --rm --network=host --read-only --cap-drop=all
  --security-opt=no-new-privileges --security-opt=label=disable
  --tmpfs /tmp:rw,mode=1777
  -v /run/libvirt:/run/libvirt:ro
  -v /run/k3s-kvm-demo:/run/k3s-kvm-demo:rw
  -v /etc/k3s-kvm-demo/config.toml:/etc/k3s-kvm-demo/config.toml:ro
  -v "$BASE:$BASE:ro"
)

# Use an empty directory dedicated to this deployment.
sudo install -d "$POOL"
sudo podman run "${options[@]}" -v "$POOL:$POOL:ro" "$IMAGE" init-pool --path "$POOL"
sudo podman run "${options[@]}" "$IMAGE" check
```

Match `POOL` to the configured pool's host target. The extra setup mount lets `init-pool` inspect the actual host directory.
Libvirt writes the pool through its API, even though this mount is read-only.
Normal serving needs no pool mount. Existing pools must already have the intended target.

For a foreground session, run `sudo podman run "${options[@]}" "$IMAGE" serve`.
For boot-time startup, install the [Quadlet](#running-as-a-service).
The libvirt socket mount permits VM mutations even with `:ro`; treat the container as a trusted host-management process.

The CLI selects configuration in this order: `--config`, `K3S_DEMO_CONFIG`, then `config.toml` in the current directory.
Place the option before the subcommand. The container sets `K3S_DEMO_CONFIG=/etc/k3s-kvm-demo/config.toml` by default.
A relative `vm.base_image` resolves from the process's working directory, not the configuration file's directory.
Use an absolute image path so service and manual launches select the same file.
Use an absolute directory with `init-pool --path` if overriding the default pool location.

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

### Exporting kubeconfig

While the dashboard runs, it exports kubeconfig to `/run/k3s-kvm-demo/kubeconfig.yaml` roughly every second, without an open browser:

```bash
sudo kubectl --kubeconfig /run/k3s-kvm-demo/kubeconfig.yaml get nodes
```

This works for native launches and the example container, which already shares the runtime directory with the host.
The file has permissions `0600` and belongs to the dashboard account, normally root.
Updates replace the file atomically when its contents change.
The dashboard removes it when export fails or during normal shutdown, and recreates it when retrieval succeeds.
Slow guest-agent calls can delay refreshes and failure detection.
A forced termination can leave the file behind until the next dashboard startup.

To change the destination or interval, add this section to your configuration:

```toml
[kubeconfig_export]
path = "/run/k3s-kvm-demo/kubeconfig.yaml"
interval_s = 1
```

Use a dedicated absolute file path whose parent directory exists and is writable by the dashboard account.
The dashboard replaces and removes this file, so do not point it at a kubeconfig you maintain yourself.
For native non-root launches, use a private directory owned by that account.
For a custom container destination, mount its parent directory read-write, rather than mounting the individual file.
Remove any old export after changing the configured path.

#### Manual exports

To use host-side `kubectl`, export credentials from a running, configured control plane node with an IP address:

```bash
sudo podman exec k3s-demo k3s-demo export --output /tmp/k3s-demo.yaml
sudo podman cp k3s-demo:/tmp/k3s-demo.yaml ./k3s-demo.yaml
sudo podman exec k3s-demo rm /tmp/k3s-demo.yaml
sudo chown "$(id -u):$(id -g)" ./k3s-demo.yaml
chmod 0600 ./k3s-demo.yaml
kubectl --kubeconfig ./k3s-demo.yaml get nodes
```

Without `--output`, the command writes kubeconfig to standard output.
File output uses permissions `0600` and refuses an existing path.
Choose an unused local destination: `podman cp` can overwrite an existing file.
The example uses the running Quadlet container, named `k3s-demo`.
For a stopped dashboard, run `export` with the setup section's `podman run` options and an output-directory mount.
Native development launches can use `uv run k3s-demo export --output ./k3s-demo.yaml`.
None of these commands changes `~/.kube/config`.

All exports grant administrator access to the demo cluster. Do not publish them.
They point to one VM's IP address on the local libvirt network.
Manual exports are snapshots. Export again when you need a fresh configuration.

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

Run one dashboard instance through your chosen launcher, with a single worker (`k3s-demo serve` does this).
Stop the dashboard before switching launch methods.

[k3s #11349]: https://github.com/k3s-io/k3s/issues/11349

## Running as a service

Install the rootful Quadlet after completing Setup:

```bash
sudo install -d /etc/containers/systemd
sudo install -m 0644 contrib/k3s-demo.container /etc/containers/systemd/k3s-demo.container
sudoedit /etc/containers/systemd/k3s-demo.container
sudo systemctl daemon-reload
sudo systemctl start k3s-demo.service
```

Set `Image=` to your local or registry image reference. Match the base-image `Volume=` line to `vm.base_image` on both sides.
The Quadlet uses host networking and root execution, with a read-only root filesystem and all capabilities dropped.
`SecurityLabelDisable=true` exempts this container from SELinux label separation; host SELinux and QEMU confinement remain enabled.
The mounts deliberately omit `:z` and `:Z` to preserve host virtualization labels.

Quadlet generates `k3s-demo.service`. Its `[Install]` section enables boot-time startup; do not run `systemctl enable` on the generated service.
Use `systemctl stop`, `systemctl restart`, and `journalctl -u k3s-demo.service` for normal operation.
The application runs one worker. Stopping the dashboard leaves its VMs running.
Container resource limits apply to the dashboard, not to the VMs managed by host libvirt.

When migrating from the old native service, stop and disable it first.
Back up its unit and configuration, then remove the installed native unit before reloading systemd.

For application updates, build or pull the selected container image and restart the service.
If changing the golden image too, extract it to a new path and update both the configuration and Quadlet mount.
Existing overlays retain their original backing image, so keep old base files until their VMs are removed.
To roll back, stop the service and restore a compatible previous container image and configuration without deleting VM storage.

## Development

For native development, install Python build and runtime tools on your development host:

```bash
# Fedora / RHEL
sudo dnf install libvirt libvirt-devel gcc pkgconf-pkg-config python3-devel qemu-img xorriso
# openSUSE
sudo zypper install libvirt libvirt-devel gcc pkg-config python3-devel qemu-tools xorriso
uv sync
```

Native hypervisor access requires authorization to manage system libvirt, often through the `libvirt` group.
For native development, create a private directory owned by your account and set `kubeconfig_export.path` to an absolute file path inside it.
Use mode `0700` for the directory before running `check` or `serve`.

Run the unit tests:

```bash
uv run pytest
```

After building the container, check its installed package, assets, bundled image, and seed tooling without accessing host libvirt:

```bash
sudo podman run --rm -i --network=none --read-only --cap-drop=all \
  --security-opt=no-new-privileges --security-opt=label=disable \
  --tmpfs /tmp:rw,mode=1777 --entrypoint python \
  localhost/k3s-kvm-demo:latest - < tests/container_smoke.py
```

This check runs against the installed image with no source mount or mocked Podman calls.
It does not verify guest boot or host permissions. Validate those on the booth host using the event checks below.
To inspect Quadlet generation before installation:

```bash
QUADLET_UNIT_DIRS="$PWD/contrib" /usr/lib/systemd/system-generators/podman-system-generator --dryrun
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
| `conn.py` | Leased libvirt connections |
| `pool.py` | Pool ownership, volume classification, disk-reference checks |
| `libvirtctl.py` | Two-volume creation, scoped ownership, idempotent deletion |
| `guestexec.py` | The guest-agent protocol and QGA implementation |
| `k3sconf.py` | Per-node k3s configuration |
| `seed.py` | Seed payload, ISO creation, and libvirt upload |
| `observer.py` | Bounded guest status polling, durable startup history, fresh join evidence |
| `image/` | KIWI description, configuration hook, guest command, and systemd units |
| `Containerfile` | Installed dashboard runtime and bundled golden image |
| `contrib/k3s-demo.container` | Rootful Podman Quadlet for the booth host |
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
12. Confirm unattended boot from the KIWI image's initial machine-ID state. Compare two clones' generated IDs and DHCP leases for distinct values.
13. Inspect both guest units with `systemd-analyze verify` in the built image. Confirm readiness notification precedes the `started` marker.
14. Test interrupted creation and deletion on a disposable host. Confirm recovery deletes only volumes with matching ownership claims.
15. Check legacy metadata on a disposable host. Confirm v2 refuses startup without changing domains or automatically resetting anything.
16. Check deletion with multiple demo nodes sharing the standalone base. Confirm an unrelated source-only qcow2 definition does not block cleanup.
17. On a disposable host, confirm visible foreign references and incomplete backing evidence in managed peers block volume deletion.
18. With the Quadlet, verify loopback-only HTTP access, container restart, host reboot, and reconnect after libvirt socket recreation.
19. Confirm stopping the service removes the exported kubeconfig and leaves no dashboard container running.
20. With host SELinux enforcing, deploy two guests sharing the extracted base, then Reset. Confirm host QEMU can read the base throughout.
