# VM integration test

This opt-in test boots one server and one worker, verifies Kubernetes readiness and workload behavior, and removes its resources.
A skipped run does not verify guest behavior.

## Running

From the repository root:

```bash
K3S_DEMO_VM_TESTS=1 uv run pytest tests/integration -s
```

The test is skipped without `K3S_DEMO_VM_TESTS=1`.
It uses these defaults. Set an environment variable to override a value:

| Environment variable | Default | Override requirements |
| --- | --- | --- |
| `K3S_DEMO_TEST_IMAGE` | `k3s-image.qcow2` in the repository root | Existing absolute path to a guest image built from this checkout |
| `K3S_DEMO_TEST_NETWORK` | `default` | Existing active libvirt network |
| `K3S_DEMO_TEST_POOL_PATH` | Unique directory created under `/var/tmp`, named `k3sit-<random>` | Existing empty directory with an absolute, symlink-free path |
| `K3S_DEMO_TEST_PROBE_IMAGE` | `registry.opensuse.org/opensuse/bci/python:3.13` | Python 3 image reference, using a tag or digest |

## Requirements

- A disposable local KVM host with access to `qemu:///system`, `qemu-img`, `xorriso`, and GNU `cp`.
- Host `kubectl` and connectivity to the guests' API port, TCP 6443.
- A [guest image built from this checkout](../../image/Readme.md#building-the-guest), at an absolute path outside the test pool.
  It must be standalone qcow2, at most 24 GiB virtual size. The installed guest wrapper must match the checkout's SHA-256 digest.
  The source must be readable by the test user.
- Capacity for two VMs, each with four virtual CPUs and 4 GiB RAM.
- An existing active libvirt network with DHCP and guest-to-guest connectivity.
- Write access to `/var/tmp` for staging the guest image and creating the automatic pool directory.
  Image staging also uses `/var/tmp` when `K3S_DEMO_TEST_POOL_PATH` supplies a custom pool directory.
  Both directories must not overlap any registered storage pool or existing domain disk reference.
  Allow space for one guest-image copy when the filesystem cannot use a reflink.
- Host permissions and security labels allowing QEMU access. The test does not change host security policy.
- On SELinux hosts, permission to label automatic directories using the context of `/var/lib/libvirt/images`.
- Registry access from both guests, or cached probe and k3s system images as described below.

### SELinux storage labels

The test labels new directories using the SELinux context of `/var/lib/libvirt/images`, in both enforcing and permissive mode.
Setup fails if this label cannot be applied. Explicitly supplied pool directories must already have suitable labels.

### Probe image

The probe runs `python3` through the container's `PATH` and uses only the Python standard library.
The image must support user and group IDs `10001`, including write access to `/tmp`.

For a reproducible image selection, set `K3S_DEMO_TEST_PROBE_IMAGE` to a `registry/repository@sha256:<digest>` reference for the guest architecture.
The test prints the requested reference and each successful probe pod's `imageID`.
It uses `imagePullPolicy: IfNotPresent`, so a cached image can satisfy a tagged reference.

Offline runs require the selected probe image and k3s system images cached in both guests before the probes start.
For unattended offline runs, include a k3s air-gap image archive in the golden image.
Image-pull failures indicate setup problems.

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

## Cleanup

Each run creates a unique `k3sit-` pool and node prefix, generates a disposable token, and ignores the user's `config.toml`.
The pool-directory lock prevents sharing that directory between runs. Runs with separate directories can execute concurrently.
Cleanup removes test nodes, volumes, the pool, the staged image, and automatically created directories. An explicitly supplied pool directory remains.

Unexpected volumes, ownership changes, blocking disk references, or uncertain pool operations preserve resources for inspection.
The staged image is removed only after confirmed teardown. Directories are removed with `rmdir`, without recursive deletion.
See [deletion rules](../../docs/development.md#storage-ownership-and-deletion) for the checks.
Hidden foreign backing dependencies are not detected: never attach test volumes to other guests or use them as backing images.

A process kill, host failure, or lost libvirt reply can leave resources behind.
Use the printed identities and libvirt metadata to inspect them before manual removal. Never use wildcard domain deletion or default-pool cleanup.
