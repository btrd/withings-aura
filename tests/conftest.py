"""Shared test fixtures."""

from __future__ import annotations

import pytest

from .fake_bridge import FakeBridge


@pytest.fixture
async def fake_bridge():
    """Run a fake Aura bridge."""
    bridge = FakeBridge()
    await bridge.start()
    try:
        yield bridge
    finally:
        await bridge.stop()
