FROM opensuse/tumbleweed AS runtime

RUN zypper --non-interactive install --no-recommends \
        python313 libvirt-client qemu-tools xorriso \
    && zypper clean --all

FROM runtime AS build

RUN zypper --non-interactive install --no-recommends \
        python313-devel libvirt-devel gcc pkg-config uv \
    && zypper clean --all

WORKDIR /build
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/k3s-kvm-demo/venv
COPY pyproject.toml uv.lock Readme.md ./
RUN uv sync --frozen --no-dev --no-install-project --python /usr/bin/python3.13
COPY src/ ./src/
RUN uv sync --frozen --no-dev --no-editable --python /usr/bin/python3.13

FROM runtime

COPY --from=build /opt/k3s-kvm-demo/venv /opt/k3s-kvm-demo/venv
ENV PATH=/opt/k3s-kvm-demo/venv/bin:$PATH \
    K3S_DEMO_CONFIG=/etc/k3s-kvm-demo/config.toml \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /opt/k3s-kvm-demo
RUN python -c 'import libvirt, libvirt_qemu, k3s_kvm_demo.app' \
    && k3s-demo --help

# Host libvirt uses an extracted copy, mounted at its absolute host path.
COPY k3s-image.qcow2 /usr/share/k3s-kvm-demo/k3s-image.qcow2

USER 0
ENTRYPOINT ["k3s-demo"]
CMD ["serve"]
