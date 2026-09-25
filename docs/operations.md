# Operations

Use the [scripted deployment](../Readme.md#deploying) for a fresh installation with default paths.
Run one dashboard instance. Stop it before switching launch methods; stopping it leaves VMs running.

## Manual installation

This procedure replaces the deployment script. Run from the repository root after meeting the [host requirements](../Readme.md#requirements).

### Extracting the golden image

Host libvirt needs a host copy of the bundled image. Choose an unused filename outside the demo pool:

```bash
IMAGE=ghcr.io/dcermak/k3s-kvm-demo:latest
BASE=/var/lib/libvirt/images/k3s-base-v2.qcow2
POOL=/var/lib/libvirt/images/k3s-demo
sudo podman create --name k3s-image-extract "$IMAGE"
sudo podman cp k3s-image-extract:/usr/share/k3s-kvm-demo/k3s-image.qcow2 "$BASE"
sudo podman rm k3s-image-extract
sudo chmod 0644 "$BASE"
```

Or build the image yourself from (see [image](../image/Readme.md)).

### Configuring the host

Install the configuration and private kubeconfig directory rule.
In the editor, set `vm.base_image` to the absolute path in `BASE`, choose a private `cluster.token`, and keep `server.bind = "127.0.0.1"`:

```bash
sudo install -d -m 0750 /etc/k3s-kvm-demo
sudo install -m 0600 config.example.toml /etc/k3s-kvm-demo/config.toml
sudoedit /etc/k3s-kvm-demo/config.toml
sudo install -m 0644 contrib/k3s-demo-tmpfiles.conf /etc/tmpfiles.d/k3s-demo.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/k3s-demo.conf
```

The tmpfiles rule creates `/run/k3s-kvm-demo` with mode `0700` at boot.
For a custom export directory, update the rule and the container mount too.

### Initializing the pool

Use an empty, dedicated directory. Keep the golden image outside it, and never attach demo volumes to other guests or use them as backing images.

```bash
options=(
  --rm --network=host --read-only --cap-drop=all
  --security-opt=no-new-privileges --security-opt=label=disable
  --tmpfs /tmp:rw,mode=1777
  -v /run/libvirt:/run/libvirt:ro
  -v /run/k3s-kvm-demo:/run/k3s-kvm-demo:rw
  -v /etc/k3s-kvm-demo/config.toml:/etc/k3s-kvm-demo/config.toml:ro
  -v "$BASE:$BASE:ro"
)
sudo install -d "$POOL"
sudo podman run "${options[@]}" -v "$POOL:$POOL:ro" "$IMAGE" init-pool --path "$POOL"
sudo podman run "${options[@]}" "$IMAGE" check
```

Match `POOL` to the configured libvirt pool's target. The setup mount lets `init-pool` inspect that directory; libvirt writes through its API.
Normal serving needs no pool mount. The read-only socket mount still permits VM mutations.
The container is a trusted host-management process.

`init-pool` creates the pool and ownership marker, not the kubeconfig directory.
Both `check` and `serve` require the export directory to exist and be writable.

### Installing the service

In the editor, set `Image=` to the reference in `IMAGE`.
Match the base-image `Volume=` to `vm.base_image`, using the same absolute path on both sides:

```bash
sudo install -d /etc/containers/systemd
sudo install -m 0644 contrib/k3s-demo.container /etc/containers/systemd/k3s-demo.container
sudoedit /etc/containers/systemd/k3s-demo.container
sudo systemctl daemon-reload
sudo systemctl start k3s-demo.service
```

The Quadlet's `[Install]` section provides boot startup; do not enable the generated service with `systemctl enable`.

`SecurityLabelDisable=true` disables container label separation. It does not grant host QEMU access to files.
Keep `:z` and `:Z` off virtualization mounts to preserve their host labels.
Container resource limits apply to the dashboard, not its VMs.

For a foreground session instead of the service, use `sudo podman run "${options[@]}" "$IMAGE" serve`.

## Managing the service

```bash
sudo systemctl status k3s-demo.service
sudo journalctl -u k3s-demo.service -f
sudo systemctl restart k3s-demo.service
sudo systemctl stop k3s-demo.service
```

Stopping leaves VMs running and removes the automatic kubeconfig export on normal shutdown.

### Launch recovery

Each dashboard launch cleans up interrupted creation and deletion operations, then starts eligible stopped VMs.
Autostart defaults to enabled, including for existing configurations that omit the setting.
To keep stopped VMs stopped across dashboard launches, set:

```toml
[vm]
autostart_on_launch = false
```

Autostart applies to shut-off nodes in `booting`, `configuring`, or `configured` state.
Failed, paused, suspended, crashed, and unknown-state nodes require operator attention.
Cleanup of interrupted operations also runs when autostart is disabled.

Recovery runs once per dashboard launch. A VM stopped afterward stays stopped until the next launch.
A failed start is logged, and recovery continues with other eligible VMs.
Check `journalctl -u k3s-demo.service` for node-specific errors and the recovery summary.
After correcting a start failure, restart the dashboard or start the affected VM directly:

```bash
sudo virsh -c qemu:///system start FULL_VM_NAME
```

Use the configured libvirt URI and the VM's full name.
Host-boot recovery requires the dashboard service, active libvirt network, and original VM storage, including backing images.

## Updating

For an application-only update, pull or build the chosen image, then restart:

```bash
sudo podman pull ghcr.io/dcermak/k3s-kvm-demo:latest
sudo systemctl restart k3s-demo.service
```

Use the reference from your Quadlet if different. If `Image=` changes, run `systemctl daemon-reload` before restarting.

For a new golden image:

1. Stop the service.
2. Extract the replacement to an unused path using the manual procedure above.
3. Update `vm.base_image` and the Quadlet's matching `Volume=` line.
4. Run `check` with the new configuration and mount, then reload systemd and start the service.


## Kubeconfig

Automatic export defaults to `/run/k3s-kvm-demo/kubeconfig.yaml`, refreshed about every second without an open browser.
The file grants cluster administrator access and has mode `0600`. Do not publish it.

Updates replace the file atomically. Export failures and normal shutdown remove it.

Change `[kubeconfig_export]` in the [configuration](../config.example.toml) to set the destination or interval.
Use a dedicated absolute filename in an existing private directory. Mount the parent directory read-write in containers.
The dashboard replaces and removes this file; never point it at a kubeconfig you maintain yourself.

### Manual export

With a running service and configured control plane node, choose an unused local filename:

```bash
sudo podman exec k3s-demo k3s-demo export --output /tmp/k3s-demo.yaml
sudo podman cp k3s-demo:/tmp/k3s-demo.yaml ./k3s-demo.yaml
sudo podman exec k3s-demo rm /tmp/k3s-demo.yaml
sudo chown "$(id -u):$(id -g)" ./k3s-demo.yaml
chmod 0600 ./k3s-demo.yaml
kubectl --kubeconfig ./k3s-demo.yaml get nodes
```

`export --output` refuses an existing file, but `podman cp` can overwrite the local destination.
Without `--output`, export writes to standard output. Native launches can use `uv run k3s-demo export --output ./k3s-demo.yaml`.
For a stopped container, use the manual installation's `podman run` options with an output-directory mount.

Manual exports are snapshots targeting one VM. Export again if that server disappears.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Dashboard unavailable | Check `systemctl status k3s-demo.service`, its journal, and the required libvirt sockets. |
| Retained VM stays shut off after dashboard launch | Check `vm.autostart_on_launch`, the node's provisioning state, and launch recovery errors in the service journal. |
| Unknown guest status or joins blocked | Inspect the guest console and `journalctl -u k3s-demo-prepare.service -u k3s-node.service`. Check the QEMU guest agent (QGA). |
| `configured` but not Kubernetes `Ready` | Check current k3s service status and Kubernetes. `configured` records past startup success. |
| Export missing | Check for a running, configured server with an IP address, working QGA, and a writable export directory. |
| Cleanup refused | Inspect the reported ownership or disk-reference problem. Never bypass it with wildcard deletion. |
| Quorum lost | Use **Reset**. **Prune Nodes** only removes Kubernetes objects. |

Guest preparation continues without the dashboard or QGA. Unknown status does not trigger reprovisioning or a provisioning timeout.
Kill and redeploy a failed demo guest when appropriate.

### Stale etcd members

Killed control plane nodes remain etcd members and count toward quorum. The dashboard banner counts managed VMs and can understate membership.
On a cluster that still has quorum, use `etcdctl` on a running server to inspect and remove a stale member:

```bash
etcd=(etcdctl
  --cert /var/lib/rancher/k3s/server/tls/etcd/client.crt
  --key /var/lib/rancher/k3s/server/tls/etcd/client.key
  --cacert /var/lib/rancher/k3s/server/tls/etcd/server-ca.crt
  --endpoints https://127.0.0.1:2379
)
"${etcd[@]}" member list
"${etcd[@]}" member remove MEMBER_ID
```

Replace `MEMBER_ID` with the stale member's ID. Member names include a random suffix.


## Configuration lookup

The CLI uses `--config`, then `K3S_DEMO_CONFIG`, then `config.toml` in the working directory.
Place `--config` before the subcommand. Containers default to `/etc/k3s-kvm-demo/config.toml`.
Relative image paths resolve from the process's working directory; use absolute paths for deployment.
