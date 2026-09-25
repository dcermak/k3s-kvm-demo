from __future__ import annotations

import copy
import subprocess
from dataclasses import fields

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
    }
    cfg = load(minimal)
    assert cfg.vm.name_prefix == "k3s-node"
    assert cfg.vm.max_nodes == 8
    assert cfg.server.port == configmod.ServerConfig().port
    assert cfg.observation == configmod.ObservationConfig(
        interval_s=2, qga_timeout_s=2, stale_after_s=30
    )
    assert cfg.maintenance.shutdown_grace_s == 30
    assert [field.name for field in fields(cfg.maintenance)] == ["shutdown_grace_s"]
    assert not hasattr(cfg, "firstboot")


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


def test_token_and_base_image_are_required(config_values):
    values = copy.deepcopy(config_values)
    del values["cluster"]["token"]
    expect_error(values, "cluster.token")

    values = copy.deepcopy(config_values)
    values["cluster"]["token"] = "   "
    expect_error(values, "cluster.token")

    values = copy.deepcopy(config_values)
    del values["vm"]["base_image"]
    expect_error(values, "vm.base_image")


def test_numeric_bounds_name_their_key(config_values):
    cases = {
        "vm.max_nodes": ("vm", "max_nodes", 0),
        "vm.vcpus": ("vm", "vcpus", 0),
        "vm.memory_mb": ("vm", "memory_mb", 128),
        "observation.interval_s": ("observation", "interval_s", 0),
        "observation.qga_timeout_s": ("observation", "qga_timeout_s", -1),
        "observation.stale_after_s": ("observation", "stale_after_s", 0),
        "maintenance.shutdown_grace_s": ("maintenance", "shutdown_grace_s", 0),
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


@pytest.mark.parametrize("section", [{}, {"script": "/old/firstboot.sh.j2"}])
def test_firstboot_requires_migration(config_values, section):
    values = copy.deepcopy(config_values)
    values["firstboot"] = section
    error = expect_error(values, "firstboot")
    assert "guest" in str(error).lower()
    assert "remov" in str(error).lower()


def test_automatic_orphan_reaping_is_rejected(config_values):
    values = copy.deepcopy(config_values)
    values["maintenance"]["reap_orphans"] = True
    expect_error(values, "maintenance.reap_orphans")


def test_disabled_legacy_reaping_can_be_removed(config_values):
    values = copy.deepcopy(config_values)
    values["maintenance"]["reap_orphans"] = False
    assert load(values) == load(config_values)


@pytest.mark.parametrize("key", ["interval_s", "qga_timeout_s", "stale_after_s"])
@pytest.mark.parametrize("value", [True, "2", 1.5])
def test_observation_requires_integers(config_values, key, value):
    values = copy.deepcopy(config_values)
    values["observation"][key] = value
    expect_error(values, f"observation.{key}")


def test_observation_settings_are_loaded(config_values):
    values = copy.deepcopy(config_values)
    values["observation"] = {"interval_s": 3, "qga_timeout_s": 1, "stale_after_s": 45}
    assert load(values).observation == configmod.ObservationConfig(3, 1, 45)


def test_load_reports_missing_and_malformed_files(tmp_path):
    with pytest.raises(configmod.ConfigError):
        configmod.load(tmp_path / "nope.toml")

    broken = tmp_path / "broken.toml"
    broken.write_text("this is not = = toml")
    with pytest.raises(configmod.ConfigError):
        configmod.load(broken)


def test_load_records_source_and_observation_settings(tmp_path, config_values):
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
                "[observation]",
                "interval_s = 5",
            ]
        )
    )
    cfg = configmod.load(toml)
    assert cfg.source == toml
    assert cfg.observation.interval_s == 5


def test_base_image_must_have_no_backing_chain(config_values, tmp_path):
    overlay = tmp_path / "backed.qcow2"
    subprocess.run(
        [
            "qemu-img",
            "create",
            "-f",
            "qcow2",
            "-F",
            "qcow2",
            "-b",
            config_values["vm"]["base_image"],
            str(overlay),
        ],
        check=True,
        capture_output=True,
    )
    config_values["vm"]["base_image"] = str(overlay)
    assert "standalone" in str(expect_error(config_values, "vm.base_image"))


@pytest.mark.parametrize("interval,stale", [(30, 30), (31, 30)])
def test_observation_must_allow_time_to_reap(config_values, interval, stale):
    config_values["observation"].update(interval_s=interval, stale_after_s=stale)
    expect_error(config_values, "observation.stale_after_s")


def test_qga_calls_have_small_upper_bound(config_values):
    config_values["observation"]["qga_timeout_s"] = 3
    expect_error(config_values, "observation.qga_timeout_s")
