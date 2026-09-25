# Building images

The [default deployment](../Readme.md#deploying) uses a published container containing the guest image.
Build locally to change the guest or dashboard. Use an x86_64 build machine with Internet access, KIWI, its boxbuild plugin, and Podman.

## Building the guest

From `image/`, run:

```bash
kiwi-ng system boxbuild --box=tumbleweed -- kiwi --description . --target-dir /var/tmp/kiwi/
```

Copy the completed qcow2 from `/var/tmp/kiwi/` to `k3s-image.qcow2` in the repository root.

The image enables SSH and contains fixed demo credentials. Use it only in an isolated demo environment.
Offline operation requires k3s system images and demo workload images cached in the guest, for example through a k3s air-gap image archive.
The build does not guarantee a populated container image cache.

## Building the container

From the repository root:

```bash
sudo podman build -t localhost/k3s-kvm-demo:latest -f Containerfile .
```

The Containerfile bundles the qcow2 unchanged and installs production Python dependencies from `uv.lock`.
The runtime includes the libvirt client, `qemu-img`, and `xorriso`; host libvirt runs the VMs.

## Custom guest-image requirements

The dashboard requires an image with the following requirements:

1. Install `k3s` on `PATH`. The dashboard does not select or validate its version.
2. Install and enable `qemu-guest-agent.service`. Allow the `guest-exec` and `guest-exec-status` RPCs.
   The supplied [qemu-ga](qemu-ga) sets `FILTER_RPC_ARGS=""` in `/etc/sysconfig/qemu-ga` to enable the otherwise filtered calls on openSUSE.
3. Install [k3s-demo-guest](k3s-demo-guest) at `/usr/local/libexec/k3s-demo-guest`, owned by root with mode `0755`.
4. Install [k3s-demo-prepare.service](k3s-demo-prepare.service) and [k3s-node.service](k3s-node.service) in `/etc/systemd/system` with mode `0644`.
5. Enable `k3s-node.service`; its dependency starts preparation. Disable and mask competing packaged k3s units, including `k3s.service` if present.
6. Remove `/var/lib/rancher/k3s`, `/etc/rancher/k3s/config.yaml`, and `/var/lib/k3s-kvm-demo` before cloning.
7. Arrange unique machine IDs for clones. The KIWI hook writes `uninitialized` to `/etc/machine-id` and enables `systemd-firstboot`.
   Remove `/var/lib/systemd/random-seed`.
8. Provide `/bin/sh`, systemd, coreutils, and util-linux, including `flock`, `mount`, and `timeout`.

## Guest provisioning

Each VM receives a writable qcow2 overlay and a read-only seed ISO labeled `K3SDEMO`, attached as a virtio disk.
The seed includes identity, role, hostname, and k3s configuration with the join token. Treat both volumes and backups as sensitive.

The guest prepares and starts k3s through installed systemd units. The dashboard uses the QEMU guest agent for status, kubeconfig export, and **Prune Nodes**.
See [provisioning state](../docs/development.md#provisioning-state) for recovery behavior.
