from __future__ import annotations

import copy

import pytest

from k3s_kvm_demo import config as configmod


def load(values: dict) -> configmod.Config:
    return configmod.from_mapping(values)


def expect_error(values: dict, key: str) -> configmod.ConfigError:
    with pytest.raises(configmod.ConfigError) as excinfo:
        load(values)
    assert excinfo.value.key == key, f"expected {key}, got {excinfo.value.key}"
    return excinfo.value


def test_defaults_survive_slots(config_values):
    """``slots=True`` hides dataclass defaults behind descriptors; the loader
    must read them from the field metadata, not the class attribute."""
    minimal = {
        "libvirt": config_values["libvirt"],
        "vm": {"base_image": config_values["vm"]["base_image"], "disk_gb": 4},
        "cluster": {"token": "t"},
        "firstboot": {"script": config_values["firstboot"]["script"]},
    }
    cfg = load(minimal)
    assert cfg.vm.name_prefix == "k3s-node"
    assert cfg.vm.max_nodes == 8
    assert cfg.server.port == 8000
    assert cfg.firstboot.retry_backoff_s == (2, 5, 10, 30)
    assert cfg.maintenance.reap_orphans is False


def test_bind_must_be_loopback(config_values):
    values = copy.deepcopy(config_values)
    values["server"]["bind"] = "0.0.0.0"
    error = expect_error(values, "server.bind")
    assert "loopback" in str(error)

    for good in ("127.0.0.1", "::1", "localhost"):
        values["server"]["bind"] = good
        assert load(values).server.bind == good


def test_disk_must_hold_the_base_image(config_values):
    values = copy.deepcopy(config_values)
    values["vm"]["disk_gb"] = 1  # the fixture image has a 2 GiB virtual size
    error = expect_error(values, "vm.disk_gb")
    assert "virtual size" in str(error)


def test_base_image_must_exist_and_be_qcow2(config_values, tmp_path):
    values = copy.deepcopy(config_values)
    values["vm"]["base_image"] = str(tmp_path / "missing.qcow2")
    expect_error(values, "vm.base_image")

    raw = tmp_path / "raw.img"
    raw.write_bytes(b"\0" * 1024)
    values["vm"]["base_image"] = str(raw)
    error = expect_error(values, "vm.base_image")
    assert "qcow2" in str(error)


def test_token_and_script_are_required(config_values):
    values = copy.deepcopy(config_values)
    del values["cluster"]["token"]
    expect_error(values, "cluster.token")

    values = copy.deepcopy(config_values)
    values["cluster"]["token"] = "   "
    expect_error(values, "cluster.token")

    values = copy.deepcopy(config_values)
    values["firstboot"]["script"] = "/nonexistent/firstboot.sh.j2"
    expect_error(values, "firstboot.script")


def test_numeric_bounds_name_their_key(config_values):
    cases = {
        "vm.max_nodes": ("vm", "max_nodes", 0),
        "vm.vcpus": ("vm", "vcpus", 0),
        "vm.memory_mb": ("vm", "memory_mb", 128),
        "firstboot.boot_timeout_s": ("firstboot", "boot_timeout_s", 0),
        "firstboot.qga_timeout_s": ("firstboot", "qga_timeout_s", -1),
        "maintenance.shutdown_grace_s": ("maintenance", "shutdown_grace_s", 0),
        "maintenance.orphan_min_age_s": ("maintenance", "orphan_min_age_s", -1),
    }
    for key, (table, field, bad) in cases.items():
        values = copy.deepcopy(config_values)
        values[table][field] = bad
        expect_error(values, key)


def test_bad_name_prefix_names_its_key(config_values):
    values = copy.deepcopy(config_values)
    values["vm"]["name_prefix"] = "Not_A_Label"
    expect_error(values, "vm.name_prefix")

    values["vm"]["name_prefix"] = "a" * 40
    error = expect_error(values, "vm.name_prefix")
    assert "63" in str(error)


def test_retry_backoff_must_be_positive_integers(config_values):
    values = copy.deepcopy(config_values)
    values["firstboot"]["retry_backoff_s"] = []
    expect_error(values, "firstboot.retry_backoff_s")

    values["firstboot"]["retry_backoff_s"] = [1, 0]
    expect_error(values, "firstboot.retry_backoff_s")


def test_load_reports_missing_and_malformed_files(tmp_path):
    with pytest.raises(configmod.ConfigError):
        configmod.load(tmp_path / "nope.toml")

    broken = tmp_path / "broken.toml"
    broken.write_text("this is not = = toml")
    with pytest.raises(configmod.ConfigError):
        configmod.load(broken)


def test_script_path_is_relative_to_the_config_file(tmp_path, config_values):
    (tmp_path / "firstboot").mkdir()
    script = tmp_path / "firstboot" / "firstboot.sh.j2"
    script.write_text("#!/bin/sh\n")
    toml = tmp_path / "config.toml"
    toml.write_text(
        "\n".join(
            [
                "[libvirt]",
                'uri = "test:///default"',
                'pool = "default-pool"',
                "[vm]",
                f'base_image = "{config_values["vm"]["base_image"]}"',
                "disk_gb = 4",
                "[cluster]",
                'token = "t"',
                "[firstboot]",
                'script = "firstboot/firstboot.sh.j2"',
            ]
        )
    )
    cfg = configmod.load(toml)
    assert cfg.firstboot.script == script
