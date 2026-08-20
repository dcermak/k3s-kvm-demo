#!/usr/bin/env bash
#
# Build the golden image the dashboard boots.
#
# Run this once, off-site, on a machine with a working network — not at the
# booth. The result is a qcow2 with k3s and qemu-guest-agent already installed
# and nothing enabled, so provisioning a node at the booth needs no internet at
# all.
#
# This is a known-good recipe, not a contract. Any image satisfying the
# requirements in the Readme will do, and the app never checks the k3s version.
#
# Requires: curl, qemu-img, and virt-customize (libguestfs-tools /
# guestfs-tools). virt-customize needs no root if libguestfs can use its
# appliance as your user.

set -euo pipefail

MIRROR="${MIRROR:-https://download.opensuse.org/tumbleweed/appliances}"
SOURCE_IMAGE="${SOURCE_IMAGE:-openSUSE-Tumbleweed-Minimal-VM.x86_64-Cloud.qcow2}"
OUTPUT="${OUTPUT:-/var/lib/libvirt/images/k3s-base.qcow2}"
DISK_SIZE="${DISK_SIZE:-20G}"
K3S_REPO="${K3S_REPO:-https://download.opensuse.org/repositories/devel:/kubic/openSUSE_Tumbleweed/devel:kubic.repo}"
ROOT_PASSWORD="${ROOT_PASSWORD:-}"

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

echo "==> downloading $SOURCE_IMAGE"
# The Cloud flavour is the one that ships qemu-guest-agent AND cloud-init; the
# plain kvm-and-xen flavour has the agent but no cloud-init.
curl -fL --progress-bar -o "$workdir/base.qcow2" "$MIRROR/$SOURCE_IMAGE"

echo "==> growing the image to $DISK_SIZE"
qemu-img resize "$workdir/base.qcow2" "$DISK_SIZE"

customise=(
  virt-customize -a "$workdir/base.qcow2"
  --run-command "zypper --non-interactive addrepo --refresh '$K3S_REPO' || true"
  --run-command "zypper --non-interactive --gpg-auto-import-keys refresh"
  --install k3s,qemu-guest-agent,util-linux

  # The guest agent is the only channel the dashboard has into the guest.
  --run-command "systemctl enable qemu-guest-agent.service"

  # firstboot writes and enables its own unit; the packaged ones must not race
  # it, and their presence is a hard error in the preflight.
  --run-command "systemctl disable k3s.service k3s-server.service k3s-agent.service 2>/dev/null || true"

  # cloud-init has no datasource here and only wastes boot time.
  --run-command "touch /etc/cloud/cloud-init.disabled"

  # A clean slate: no cluster data, and no machine-id, so every clone gets its
  # own and therefore its own DHCP lease.
  --run-command "rm -rf /var/lib/rancher/k3s /etc/rancher/k3s/config.yaml /var/lib/k3s-kvm-demo"
  --run-command "rm -f /etc/machine-id /var/lib/systemd/random-seed"
  --run-command "rm -rf /var/cache/zypp/*"
)

if [[ -n "$ROOT_PASSWORD" ]]; then
  # Only useful for poking at a wedged node on the serial console.
  customise+=(--root-password "password:$ROOT_PASSWORD")
fi

echo "==> customising"
"${customise[@]}"

echo "==> writing $OUTPUT"
install -D -m 0644 "$workdir/base.qcow2" "$OUTPUT"

cat <<EOF

Done: $OUTPUT

Check it before the event:
  qemu-img info $OUTPUT
  virt-cat -a $OUTPUT /etc/machine-id      # should not exist
  virt-ls  -a $OUTPUT /var/lib/rancher     # should not exist

Then point vm.base_image at it and run:
  uv run k3s-demo check
EOF
