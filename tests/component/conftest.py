"""Fixtures for Codex Voice component tests."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> None:
    """Enable custom integrations for every component test."""
    assert enable_custom_integrations is None
