"""Provider selection.

The pipeline depends on the ``VisionProvider`` protocol, never on a concrete
provider. Adding a local model later means adding a module here and one value to
the ``VISION_PROVIDER`` enum -- ingest and storage do not change.

Exactly one provider is built. There is no fallback chain: a failing provider
records failed analyses, it does not hand the frame to another one.
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
    if settings.vision_provider == "ollama":
        from hermes_home.vision.ollama import OllamaVisionProvider

        settings.validate_ollama_selection()
        return OllamaVisionProvider(
            base_url=settings.ollama_base_url,
            model=settings.vision_model,
            num_ctx=settings.ollama_num_ctx,
            num_predict=settings.ollama_num_predict,
            think=settings.ollama_think,
            keep_alive=settings.ollama_keep_alive,
        )
    raise ValueError(f"unknown VISION_PROVIDER: {settings.vision_provider!r}")
