# Real-VM integration test

> **Guest boot remains unverified.** This opt-in test has not been run against
> the v2 image. Collection and a skipped test are not evidence of successful VM boot,
> systemd startup, Kubernetes readiness, or live cleanup.

This opt-in test boots one server and one agent from a supplied v2 golden image.
It uses `ConnectionManager`, `NodeManager`, seed ISO upload, and `Observer` without mocks.
There are no fake join-readiness responses or guest provisioning scripts injected through QGA.
The test also runs a read-only `kubectl get nodes` command through the QEMU guest agent (QGA) to verify Kubernetes `Ready`.
It then exports kubeconfig through the CLI and uses host-side `kubectl` to list the expected nodes.
This checks guest-agent retrieval, API reachability, TLS verification, and exported credentials without mocks.
Each guest's installed wrapper must match the working tree's SHA-256 digest.
The test then runs a non-root probe pod on each node and checks CoreDNS availability.
The probes read the hosts and resolver files, create and remove a temporary file, and resolve local and cluster names.
Both pods must exit successfully, and both containerd processes must report umask `0022`.

## Requirements

- Use a disposable local KVM test host with access to `qemu:///system`, `qemu-img`, and `xorriso`.
- Install `kubectl` on the host's `PATH`. The host must reach the guests' API port, TCP 6443.
- Supply an image built from this revision's `scripts/build-image.sh` at a new path, outside the test pool directory.
- Supply a digest-pinned Python 3 probe image through `K3S_DEMO_TEST_PROBE_IMAGE`.
- The golden image must be standalone qcow2, with no backing file of its own.
- The image's virtual size must not exceed 20 GiB. Each test VM uses two virtual CPUs and 2 GiB of memory.
- Supply an existing active libvirt network dedicated to testing, with DHCP and guest-to-guest connectivity.
- Supply an existing empty directory reserved for this run. It must not overlap any registered storage pool or existing domain disk reference.
- Provision filesystem permissions and host security labels for libvirt/QEMU before running. The test does not change host security policy.
- Stop other operators from attaching disks or using the reserved directory during the run.

The directory must be an absolute path without symlinks, for example `/var/lib/k3s-demo-vm-tests`.
Do not use `/var/lib/libvirt/images` or a subdirectory if an existing pool already covers that path.
The test does not create, stop, redefine, or delete the supplied network.
Legacy domain metadata blocks the test just as it blocks the dashboard. The test never resets legacy guests.

The builder defaults to `/var/lib/libvirt/images/k3s-base-v2.qcow2`.
It publishes the completed image atomically and refuses to overwrite an existing output.
Use another unused `OUTPUT` path for each rebuild. The selected output directory must already exist and be writable.
The image contains an empty `/etc/machine-id`, with the D-Bus machine ID and random seed removed.
PID 1 generates the clone's identity without interactive first-boot setup or first-boot presets.

## Probe image

Use an image with `/usr/local/bin/python3`, the Python standard library, and a writable `/tmp` for UID/GID `10001`.
An official `python:3.13-slim` image, resolved to an immutable digest for the guest architecture, meets these requirements.
Set `K3S_DEMO_TEST_PROBE_IMAGE` to its full reference, such as `registry/repository@sha256:<64 hexadecimal characters>`.
Tags alone are rejected. The test does not install packages or change permissions inside the probe container.

The pod starts Python through its absolute path with all capabilities dropped and privilege escalation disabled.
It uses `socket.getaddrinfo` for both `localhost` and `kubernetes.default.svc.cluster.local`.
The cluster name must resolve to the Kubernetes API Service's actual ClusterIP.
Filesystem checks use the container image's writable layer, without an extra volume masking `/tmp` permissions.

Online runs need registry access from both guests. The pods use `imagePullPolicy: IfNotPresent`.
Offline runs require the same digest-pinned image imported into K3s containerd on both guests before the probes start.
For unattended offline runs, include a K3s air-gap image archive in the test golden image before creating its clones.
System workload images must also be available. The builder does not guarantee that container images are cached.
Image-pull errors are test setup failures, not evidence of the umask regression.

## Running the test

Run these commands from the repository root. Both image and pool environment variables require absolute paths.
Collection does not open a hypervisor connection:

```bash
uv run pytest tests/integration --collect-only
```

Without explicit opt-in, the test is skipped:

```bash
uv run pytest tests/integration
```

After preparing the host, set the probe image and the four VM variables.
The following paths and network name are examples, not resources the test creates:

```bash
K3S_DEMO_VM_TESTS=1 \
K3S_DEMO_TEST_IMAGE=/var/lib/libvirt/images/k3s-base-v2.qcow2 \
K3S_DEMO_TEST_POOL_PATH=/var/lib/k3s-demo-vm-tests \
K3S_DEMO_TEST_NETWORK=k3s-demo-test \
K3S_DEMO_TEST_PROBE_IMAGE="$PROBE_IMAGE_BY_DIGEST" \
uv run pytest tests/integration -s
```

The test waits up to 600 s for each guest's configured and active status,
then up to 600 s for both Kubernetes nodes to become `Ready`.
Fresh `Observer` evidence authorizes the agent deployment.
The test verifies read-only virtio seed attachment and checks that Reset removes both nodes and all four node volumes.
Probe pods have a 240 s active deadline, and the host waits up to 300 s for both to succeed.
DNS retries have a 60 s budget, bounded externally by the pod deadline if a resolver call stalls.
CoreDNS rollout verification has a separate 60 s timeout. Historical system-pod restarts do not automatically fail the test.

On probe failure, the test collects pod status, events, available probe logs, and CoreDNS diagnostics before cleanup.
Wrapper hashes and containerd umasks appear in test output. Credentials and token contents are not collected.
The test deletes its uniquely named probe namespace, then the fixture tears down its owned VMs and storage.

## Regression verification

Run the same workload checks against fresh images with the old and corrected wrappers.
For the negative control, use a separate source copy containing the old wrapper and build the image from that copy.
Keep the integration test changes in both copies so each installed wrapper matches its corresponding source digest.
The old-wrapper run must fail on workload startup, filesystem access, or DNS, rather than image pulling or a stale-image check.
The corrected-wrapper run must pass on both server and agent nodes.

A skipped test does not verify the fix. Record both image identities and test results when a VM host is available.
Existing guests can retain incorrectly permissioned snapshots and sandbox files after a wrapper update.
Do not use a simple service restart as the positive control, and do not change permissions recursively across K3s data.

## Resource ownership

Each run generates a unique pool name and node prefix, beginning with `k3sit-`.
It generates a disposable cluster token in memory and never reads the user's `config.toml`.
A directory lock prevents concurrent test runs from using the same directory.
The integration fixture overrides the parent suite's automatic test-driver cleanup.
It never uses `default-pool` or runs the parent cleanup against QEMU.

The test prints its pool name, pool UUID, directory, and each successfully created domain UUID.
Cleanup uses `manager.reset()` in that unique pool UUID/prefix scope.
Deletion resolves exact domain UUIDs and verifies both volume claims.
After node cleanup, the fixture removes its marker and undefines its own pool.
It leaves the supplied directory in place and never recursively removes files.

Cleanup also runs after ordinary assertion failures. Unexpected volumes, ownership changes,
or disk references cause cleanup to stop and preserve resources for inspection.
Deletion requires complete backing evidence from other nodes in the test's pool UUID/prefix scope.
Known raw disks and read-only CD-ROM devices are accepted. Qcow2 chains need explicit formats and a terminating `<backingStore/>`.
The v2 domain XML contract includes the standalone base and terminator for each demo overlay.
Visible references from any guest's live or persistent definition block deletion, even when its backing chain is incomplete.
An unrelated source-only qcow2 definition does not block cleanup solely because it omits backing evidence.
Foreign image files do not need to be readable by the test process.
Unreadable domain XML, unsupported disk-source syntax, and incompatible application metadata still block cleanup.

The test pool is exclusively for its deployment.
Other guests and externally managed images must not attach its volumes or use them as backing images.
Hidden foreign backing dependencies are not detected and can be damaged by cleanup.
Administrators must not attach or rebase images onto test volumes while cleanup runs.

A process kill, host failure, or lost libvirt reply can leave test resources behind.
Use the printed identities and libvirt metadata to inspect those resources before removing them manually.
Never use a wildcard domain deletion or a default-pool cleanup command.

## Release checklist

A successful run checks boot, seed attachment, systemd startup, cluster join, Kubernetes readiness, and host access using exported credentials.
It also checks non-root filesystem access, local and cluster name resolution on both nodes, and normal two-volume cleanup.
It does not prove crash consistency or all systemd failure and recovery behavior.
Before a release, also complete the main README's **Before the event** checklist on a disposable host.
That includes dashboard termination during preparation, QGA outages, guest and host reboots,
machine ID uniqueness, current service failure after historical success, and legacy metadata refusal.
Confirm unattended PID 1 startup from the empty machine ID file, without interactive setup or presets changing the guest units.
Check multi-node deletion alongside an unrelated source-only qcow2 guest.
On a disposable host, check refusal for visible foreign references and incomplete backing evidence in managed peers.
Run `systemd-analyze verify` for both guest units inside the built image, where their executable paths and dependencies exist.
