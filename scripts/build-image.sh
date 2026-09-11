#!/usr/bin/env bash
#
# Build the golden image the dashboard boots.
#
# Run this once, off-site, on a machine with a working network — not at the
# booth. The result is a qcow2 with k3s and qemu-guest-agent already installed
# and the repo-owned guest service enabled. Provisioning needs no internet.
#
# The image must implement the guest provisioning protocol in the Readme.
# This recipe installs the repo-owned script and units that implement it.
#
# Requires: curl, qemu-img, and virt-customize (libguestfs-tools /
# guestfs-tools). virt-customize needs no root if libguestfs can use its
# appliance as your user.

set -euo pipefail

MIRROR="${MIRROR:-https://download.opensuse.org/tumbleweed/appliances}"
SOURCE_IMAGE="${SOURCE_IMAGE:-openSUSE-Tumbleweed-Minimal-VM.x86_64-Cloud.qcow2}"
OUTPUT="${OUTPUT:-/var/lib/libvirt/images/k3s-base-v2.qcow2}"
DISK_SIZE="${DISK_SIZE:-20G}"
K3S_REPO="${K3S_REPO:-https://download.opensuse.org/repositories/devel:/kubic/openSUSE_Tumbleweed/devel:kubic.repo}"
ROOT_PASSWORD="${ROOT_PASSWORD:-}"
REPO_ROOT="$(dirname "$(dirname "$(readlink -f "$0")")")"

if [[ -e "$OUTPUT" || -L "$OUTPUT" ]]; then
  printf 'Refusing to overwrite existing output: %s\n' "$OUTPUT" >&2
  exit 1
fi
output_dir="$(dirname -- "$OUTPUT")"
if [[ ! -d "$output_dir" || ! -w "$output_dir" || ! -x "$output_dir" ]]; then
  printf 'Output directory must exist and be writable: %s\n' "$output_dir" >&2
  exit 1
fi

workdir="$(mktemp -d)"
staged=
cleanup() {
  if [[ -n "$staged" ]]; then rm -f -- "$staged"; fi
  rm -rf -- "$workdir"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

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
  --install "k3s,qemu-guest-agent,util-linux"

  # The guest agent reports status; the seed and guest units own provisioning.
  --run-command "systemctl enable qemu-guest-agent.service"

  # Packaged units must never race the repo-owned service.
  --run-command "systemctl disable k3s.service k3s-server.service k3s-agent.service 2>/dev/null || true"
  --run-command "systemctl mask k3s.service k3s-server.service k3s-agent.service"
  --mkdir /usr/local/libexec
  --upload "$REPO_ROOT/firstboot/k3s-demo-guest:/usr/local/libexec/k3s-demo-guest"
  --chmod 0755:/usr/local/libexec/k3s-demo-guest
  --upload "$REPO_ROOT/firstboot/k3s-demo-prepare.service:/etc/systemd/system/k3s-demo-prepare.service"
  --upload "$REPO_ROOT/firstboot/k3s-node.service:/etc/systemd/system/k3s-node.service"
  --chmod 0644:/etc/systemd/system/k3s-demo-prepare.service
  --chmod 0644:/etc/systemd/system/k3s-node.service
  --run-command "systemctl enable k3s-node.service"

  # cloud-init has no datasource here and only wastes boot time.
  --run-command "mkdir -p /etc/cloud; touch /etc/cloud/cloud-init.disabled"

  # A clean slate: no cluster data or shared machine identity.
  --run-command "rm -rf /var/lib/rancher/k3s /etc/rancher/k3s/config.yaml /var/lib/k3s-kvm-demo"
  # Unlink the D-Bus alias as well as regular stale IDs, without following it.
  # An empty file lets PID 1 generate an ID without ConditionFirstBoot=yes,
  # avoiding first-boot setup and presets that could change our enabled units.
  --run-command "rm -f /var/lib/dbus/machine-id /etc/machine-id /var/lib/systemd/random-seed && install -m 0644 /dev/null /etc/machine-id"
  --run-command "rm -rf /var/cache/zypp/*"
)

if [[ -n "$ROOT_PASSWORD" ]]; then
  # Only useful for poking at a wedged node on the serial console.
  customise+=(--root-password "password:$ROOT_PASSWORD")
fi

echo "==> customising"
"${customise[@]}"

echo "==> writing $OUTPUT"
staged="$(mktemp "$output_dir/.k3s-demo-image.XXXXXX")"
install -m 0644 -- "$workdir/base.qcow2" "$staged"
# Same-filesystem hard linking publishes the complete image atomically. -T also
# refuses a directory or symlink at OUTPUT, including one created after preflight.
ln -T -- "$staged" "$OUTPUT"

cat <<EOF

Done: $OUTPUT

Check it before the event:
  qemu-img info $OUTPUT
  virt-cat -a $OUTPUT /etc/machine-id      # should be empty
  virt-ls  -a $OUTPUT /var/lib/rancher     # should not exist

Then point vm.base_image at it and run:
  uv run k3s-demo check
EOF
