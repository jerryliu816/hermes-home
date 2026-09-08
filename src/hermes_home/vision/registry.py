"""Provider selection.

The pipeline depends on the ``VisionProvider`` protocol, never on a concrete
provider. Adding a local model later means adding a module here and one value to
the ``VISION_PROVIDER`` enum -- ingest and storage do not change.
"""

from __future__ import annotations

from hermes_home.config import Settings
from hermes_home.vision.base import VisionProvider
from hermes_home.vision.mock import MockVisionProvider


def build_provider(settings: Settings) -> VisionProvider:
    if settings.vision_provider == "mock":
        return MockVisionProvider()
    if settings.vision_provider == "anthropic":
        # Imported lazily so the anthropic extra stays optional.
        from hermes_home.vision.anthropic import AnthropicVisionProvider

        return AnthropicVisionProvider(
            api_key=settings.anthropic_api_key, model=settings.vision_model
        )
    raise ValueError(f"unknown VISION_PROVIDER: {settings.vision_provider!r}")
