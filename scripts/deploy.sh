#!/bin/bash
set -euo pipefail

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
image=$(sed -n 's/^Image=//p' "$repo/contrib/k3s-demo.container")
base=/var/lib/libvirt/images/k3s-base-v2.qcow2
pool=/var/lib/libvirt/images/k3s-demo

install -d -m 0750 /etc/k3s-kvm-demo
install -m 0600 "$repo/config.toml" /etc/k3s-kvm-demo/config.toml
install -m 0644 "$repo/contrib/k3s-demo-tmpfiles.conf" /etc/tmpfiles.d/k3s-demo.conf
systemd-tmpfiles --create /etc/tmpfiles.d/k3s-demo.conf

if [[ ! -e "$base" && ! -L "$base" ]]; then
    staging=$(mktemp -d "${base}.XXXXXX")
    container=
    cleanup() {
        if [[ -n "$container" ]]; then
            podman rm "$container" || true
        fi
        rm -f "$staging/base.qcow2"
        rmdir "$staging"
    }
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM

    container=$(podman create "$image")
    podman cp "$container:/usr/share/k3s-kvm-demo/k3s-image.qcow2" "$staging/base.qcow2"
    chmod 0644 "$staging/base.qcow2"
    # Publish the completed copy without overwriting an existing backing image.
    ln -T "$staging/base.qcow2" "$base"
    restorecon "$base"
    cleanup
    trap - EXIT INT TERM
fi

options=(
    --rm --network=host --read-only --cap-drop=all
    --security-opt=no-new-privileges --security-opt=label=disable
    --tmpfs '/tmp:rw,mode=1777'
    -v /run/libvirt:/run/libvirt:ro
    -v /run/k3s-kvm-demo:/run/k3s-kvm-demo:rw
    -v /etc/k3s-kvm-demo/config.toml:/etc/k3s-kvm-demo/config.toml:ro
    -v "$base:$base:ro"
)

install -d "$pool"
podman run "${options[@]}" -v "$pool:$pool:ro" "$image" init-pool --path "$pool"
podman run "${options[@]}" "$image" check

install -d /etc/containers/systemd
install -m 0644 "$repo/contrib/k3s-demo.container" /etc/containers/systemd/k3s-demo.container
systemctl daemon-reload
systemctl start k3s-demo.service
