"""Node naming.

Names are ``<prefix>-<32 hex>`` and the hex is the domain UUID.  Names are
never recycled: etcd member names carry their own random suffix, so rebuilding
a server on a previously used hostname is rejected with ``duplicate node name
found`` while the dead member lingers.  A 128-bit identifier makes reuse
negligibly probable (2**-128) rather than merely unlikely.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Container

#: RFC 1123 label, as Kubernetes requires for node names.
DNS_1123_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
MAX_LABEL_LEN = 63

#: ``-`` plus 32 hex characters are appended to the configured prefix.
SUFFIX_LEN = 33
MAX_PREFIX_LEN = MAX_LABEL_LEN - SUFFIX_LEN

VOLUME_SUFFIX = ".qcow2"


class InvalidName(ValueError):
    """A name or prefix is not usable as a DNS-1123 label."""


def validate_prefix(prefix: str) -> str:
    """Return *prefix* if a node name built from it fits in a DNS-1123 label."""
    if not DNS_1123_LABEL.match(prefix):
        raise InvalidName(
            f"{prefix!r} is not a DNS-1123 label "
            "(lowercase alphanumerics and '-', not starting or ending with '-')"
        )
    if len(prefix) > MAX_PREFIX_LEN:
        raise InvalidName(
            f"{prefix!r} is {len(prefix)} characters; at most {MAX_PREFIX_LEN} "
            f"are allowed so that '<prefix>-<32 hex>' fits in {MAX_LABEL_LEN}"
        )
    return prefix


def name_pattern(prefix: str) -> re.Pattern[str]:
    """Match exactly the node names this deployment may own."""
    return re.compile(rf"^{re.escape(prefix)}-[0-9a-f]{{32}}$")


def volume_pattern(prefix: str) -> re.Pattern[str]:
    """Match exactly the overlay volume names this deployment may own."""
    return re.compile(rf"^{re.escape(prefix)}-[0-9a-f]{{32}}\{VOLUME_SUFFIX}$")


def node_name(prefix: str, uuid_hex: str) -> str:
    return f"{prefix}-{uuid_hex}"


def volume_name(name: str) -> str:
    return f"{name}{VOLUME_SUFFIX}"


def allocate(prefix: str, taken: Container[str]) -> tuple[str, str]:
    """Return an unused ``(name, uuid)`` pair.

    *taken* holds the names already present.  A collision is astronomically
    unlikely but re-rolling is one line, so we do not rely on that.
    """
    while True:
        identifier = uuid.uuid4()
        name = node_name(prefix, identifier.hex)
        if name not in taken:
            return name, str(identifier)
