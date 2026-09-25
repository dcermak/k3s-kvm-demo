# Development

## Native setup

Install Python build tools and libvirt dependencies:

```bash
# Fedora / RHEL
sudo dnf install libvirt libvirt-devel gcc pkgconf-pkg-config python3-devel qemu-img xorriso
# openSUSE
sudo zypper install libvirt libvirt-devel gcc pkg-config python3-devel qemu-tools xorriso
# mandatory everywhere
uv sync
```

Authorize your account to manage system libvirt, commonly through the `libvirt` group.
Copy `config.example.toml` to `config.toml` and choose a private token.
Set `vm.base_image` to an absolute path to an extracted or [built guest image](../image/Readme.md).
Set `kubeconfig_export.path` inside an existing directory owned by your account with mode `0700`.
Keep the image outside the dedicated demo pool.

```bash
uv run k3s-demo init-pool
uv run k3s-demo check
uv run k3s-demo serve
```

Stop any other dashboard instance first. CLI configuration lookup is documented in [operations](operations.md#configuration-lookup).

## Testing

Run the default suite:

```bash
uv run pytest
```

It uses libvirt's `test:///default` driver for volumes, domains, metadata, and lease addresses.
Guest-agent responses and seed uploads are substituted where a running VM is required. It does not access `qemu:///system`.
The [VM integration test](../tests/integration/Readme.md) requires explicit opt-in and a disposable KVM host.

After building the container, check its installed package, assets, image, and seed tooling:

```bash
sudo podman run --rm -i --network=none --read-only --cap-drop=all \
  --security-opt=no-new-privileges --security-opt=label=disable \
  --tmpfs /tmp:rw,mode=1777 --entrypoint python \
  localhost/k3s-kvm-demo:latest - < tests/container_smoke.py
```

This check needs no host libvirt access. Verify guest boot and host permissions separately on the target host.


### Provisioning state

Durable states are `creating`, `booting`, `configuring`, `configured`, `failed`, and `deleting`.
Power, current service status, and Kubernetes readiness are separate observations.

The guest's `prepare` command mounts the seed read-only, installs configuration, synchronizes files, and commits a durable `prepared` marker.
Repeated preparation validates existing state. `run` starts k3s; `mark-started` records success after k3s signals systemd readiness.
`status` reads startup history and current unit states without changing configuration.

The dashboard does not provision through the QEMU guest agent (QGA). Guest preparation continues without it.
`configured` records successful startup history even if k3s later fails or the guest shuts off.
Stale QGA evidence makes service status unknown and blocks new joins through that server, without erasing history or reprovisioning.
Polling and freshness settings are in [config.example.toml](../config.example.toml).

### Storage ownership and deletion

The pool contains an ownership marker, seed disks, and overlays. Startup refuses a missing marker, unrecognized volumes, or incompatible metadata.
Each node claims two volumes, named by domain UUID. Ownership is scoped by pool UUID and node-name prefix.
Kill and Reset delete claimed volumes; unclaimed volumes are reported for inspection rather than reaped automatically.

Deletion checks unrelated guests' live and persistent definitions as well as this deployment's nodes.
Visible references to either claimed volume block deletion, including references in incomplete backing chains.
Only managed peers require complete backing-chain evidence:

- Known raw disks and read-only CD-ROM devices are accepted.
- Qcow2 chains require explicit formats and a terminating `<backingStore/>`.
- The v2 domain XML records the standalone golden image and explicitly terminates each overlay's chain.

An unrelated source-only qcow2 disk does not block cleanup solely because its chain is incomplete.
The dashboard does not read foreign image files. Unreadable domain XML, unsupported disk sources, and incompatible metadata still block deletion.

The pool must remain exclusive to this deployment. Hidden foreign backing dependencies are not detected and can be damaged during cleanup.
Do not attach or rebase images onto demo volumes during deletion.
Add a backing-chain terminator only when the image is verified to have no further backing file.

### Adding metrics

`stats.enrich` is currently a no-op. Extend the existing `getAllDomainStats` call for CPU and memory metrics; templates accept optional metric fields.
Use Kubernetes API evidence for `Ready`. Keep metrics separate from provisioning history and current systemd status.
