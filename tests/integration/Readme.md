# VM integration test

This opt-in test boots one server and one worker, verifies Kubernetes readiness and workload behavior, and removes its resources.
A skipped run does not verify guest behavior.

## Requirements

- A disposable local KVM host with access to `qemu:///system`, `qemu-img`, and `xorriso`.
- Host `kubectl` and connectivity to the guests' API port, TCP 6443.
- A [guest image built from this checkout](../../image/Readme.md#building-the-guest), at an absolute path outside the test pool.
  It must be standalone qcow2, at most 24 GiB virtual size. The installed guest wrapper must match the checkout's SHA-256 digest.
- Capacity for two VMs, each with four virtual CPUs and 4 GiB RAM.
- An existing dedicated, active libvirt network with DHCP and guest-to-guest connectivity.
- An existing empty directory reserved for this run, with an absolute, symlink-free path.
  It must not overlap any registered storage pool or existing domain disk reference.
- Host permissions and security labels allowing QEMU access. The test does not change host security policy.
- A digest-pinned Python 3 probe image as described below.

For example, reserve `/var/lib/k3s-demo-vm-tests`. Avoid `/var/lib/libvirt/images` if an existing pool covers it.
The supplied network is left unchanged. Legacy application metadata blocks the test without resetting existing guests.

### Probe image

Use an image with `/usr/local/bin/python3`, the standard library, and `/tmp` writable by UID/GID `10001`.
An official `python:3.13-slim` image resolved to a digest for the guest architecture meets these requirements.
Set `PROBE_IMAGE_BY_DIGEST` to its full `registry/repository@sha256:<64 hexadecimal characters>` reference. Tags alone are rejected.

Both guests need registry access. Offline runs require the pinned probe image and k3s system images cached before the probes start.
For unattended offline runs, include a k3s air-gap image archive in the golden image.
Image-pull failures indicate setup problems.

## Running

From the repository root, substitute your image path, reserved directory, and existing network:

```bash
K3S_DEMO_VM_TESTS=1 \
K3S_DEMO_TEST_IMAGE=/var/lib/libvirt/images/k3s-base-v2.qcow2 \
K3S_DEMO_TEST_POOL_PATH=/var/lib/k3s-demo-vm-tests \
K3S_DEMO_TEST_NETWORK=k3s-demo-test \
K3S_DEMO_TEST_PROBE_IMAGE="$PROBE_IMAGE_BY_DIGEST" \
uv run pytest tests/integration -s
```

The test is skipped without `K3S_DEMO_VM_TESTS=1`.


## Results and diagnostics

A successful run verifies:

- Guest boot, read-only virtio seed attachment, systemd startup, and server/worker join.
- Kubernetes `Ready` and host API access using CLI-exported credentials, including TLS verification.
- Wrapper hashes, containerd umask `0022`, and CoreDNS availability.
- Non-root filesystem access and local/cluster DNS resolution on both nodes.
- Reset deleting both VMs and all four node volumes.

Probes run without privilege escalation or extra volumes masking `/tmp` permissions.
The test allows 600 s per guest for configured/active status, then 600 s for Kubernetes readiness.
Probe pods have a 240 s deadline; the host waits up to 300 s. DNS and CoreDNS checks each have a 60 s budget.

On probe failure, output includes pod status, events, available logs, and CoreDNS diagnostics.
Wrapper hashes and umasks are printed; credentials are not collected.
For old-versus-corrected image comparisons, follow [regression verification](../../docs/development.md#verifying-the-guest-umask-regression).
Complete the [release checks](../../docs/development.md#release-checks) separately for interruption and recovery behavior.

## Cleanup

Each run creates a unique `k3sit-` pool and node prefix, generates a disposable token, and ignores the user's `config.toml`.
A directory lock prevents concurrent test runs. The fixture prints pool and domain identities and cleans only its own scope.
After removing nodes and volumes, it removes its marker and undefines the pool, leaving the supplied directory in place.

Cleanup runs after ordinary assertion failures. Unexpected volumes, ownership changes, or blocking disk references preserve resources for inspection.
See [deletion rules](../../docs/development.md#storage-ownership-and-deletion) for the checks.
Hidden foreign backing dependencies are not detected: never attach test volumes to other guests or use them as backing images.

A process kill, host failure, or lost libvirt reply can leave resources behind.
Use the printed identities and libvirt metadata to inspect them before manual removal. Never use wildcard domain deletion or default-pool cleanup.
