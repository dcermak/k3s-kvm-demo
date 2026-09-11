"""Keep real-VM tests independent of the test-driver cleanup fixture."""

import pytest


@pytest.fixture(autouse=True)
def libvirt_clean():
    """Override tests/conftest.py: never open or wipe the default test pool."""
    yield
