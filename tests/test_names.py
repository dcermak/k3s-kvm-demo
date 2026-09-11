from __future__ import annotations

import pytest

from k3s_kvm_demo import names


def test_prefix_must_be_a_dns_label():
    for bad in ("K3s", "-lead", "trail-", "under_score", "spa ce"):
        with pytest.raises(names.InvalidName):
            names.validate_prefix(bad)
    assert names.validate_prefix("k3s-node") == "k3s-node"


def test_prefix_must_leave_room_for_the_identifier():
    longest = "a" * names.MAX_PREFIX_LEN
    assert names.validate_prefix(longest) == longest
    assert len(names.node_name(longest, "0" * 32)) == names.MAX_LABEL_LEN

    with pytest.raises(names.InvalidName) as excinfo:
        names.validate_prefix("a" * (names.MAX_PREFIX_LEN + 1))
    assert str(names.MAX_PREFIX_LEN) in str(excinfo.value)


def test_identifier_is_128_bits():
    """A 16-bit suffix would make historical reuse real, and reuse is what
    trips etcd's 'duplicate node name found'."""
    name, identifier = names.allocate("k3s-node", set())
    suffix = name.removeprefix("k3s-node-")
    assert len(suffix) == 32
    assert int(suffix, 16) >= 0
    assert identifier.replace("-", "") == suffix


def test_allocate_rerolls_on_collision():
    first, _ = names.allocate("k3s-node", set())
    second, _ = names.allocate("k3s-node", {first})
    assert second != first


def test_patterns_only_match_our_own_names():
    pattern = names.name_pattern("k3s-node")
    good = names.node_name("k3s-node", "a" * 32)
    assert pattern.match(good)
    for bad in (
        "k3s-node",
        "k3s-node-01",
        f"{good}-extra",
        f"prefix-{good}",
        names.node_name("other", "a" * 32),
        names.node_name("k3s-node", "A" * 32),
    ):
        assert not pattern.match(bad), bad

    volumes = names.volume_pattern("k3s-node")
    assert volumes.match(names.volume_name(good))
    assert volumes.match(names.seed_volume_name(good))
    assert not volumes.match(names.volume_name(good) + "\n")
    assert not pattern.match(good + "\n")
    assert not volumes.match(good)
    assert not volumes.match("k3s-kvm-demo.marker")


def test_prefix_is_escaped_in_patterns():
    # A prefix cannot contain regex metacharacters today, but the pattern must
    # not depend on that remaining true.
    pattern = names.name_pattern("a.b")
    assert not pattern.match("axb-" + "0" * 32)
