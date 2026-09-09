"""Shared fixtures.

Two principles here:

* Tests run migrations rather than ``create_all``, so the migration chain is
  exercised on every run. A schema that only exists in the models is a schema
  that will diverge from what is actually deployed.
* The fake Home Assistant and the mock vision provider mean the whole pipeline is
  exercisable with no network, no credentials, and no real hardware.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from hermes_home.clients.home_assistant import EntityState
from hermes_home.config import CamerasConfig, HomeConfig, Settings
from hermes_home.core.time import now_utc
from hermes_home.spatial import seed_home
from hermes_home.storage.engine import create_engine, create_session_factory, session_scope

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Tests read this instead of the operator's real config/, which is gitignored
#: and would make the suite unrunnable on a fresh clone -- and would couple test
#: assertions to whatever the actual house looks like today.
FIXTURE_CONFIG = Path(__file__).resolve().parent / "fixtures"


def _png(width: int, height: int, *, tag: bytes = b"") -> bytes:
    """Build a genuinely valid PNG.

    Real bytes rather than a stub, so the header parser that records image
    dimensions is exercised against something a decoder would actually accept.
    ``tag`` lands in a trailing comment chunk, which changes the content hash
    without changing the pixels -- handy for dedupe tests.
    """
    import struct
    import zlib

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
    )
    if tag:
        png += chunk(b"tEXt", b"note\x00" + tag)
    return png + chunk(b"IEND", b"")


#: Two distinct valid images: same dimensions, different bytes, so content
#: dedupe is tested on a real hash difference rather than a contrived one.
TINY_IMAGE = _png(4, 3)
OTHER_IMAGE = _png(4, 3, tag=b"second")
TINY_IMAGE_WIDTH, TINY_IMAGE_HEIGHT = 4, 3

#: Minimal valid JPEG *header* (SOI + APP0 + SOF0), enough to exercise the JPEG
#: branch of the dimension parser without embedding a whole encoded image.
JPEG_HEADER_640x480 = (
    b"\xff\xd8"
    b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xc0\x00\x11\x08\x01\xe0\x02\x80\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
)


@pytest.fixture(scope="session")
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        # _env_file=None so a developer's real .env can never leak into a test.
        _env_file=None,
        home_assistant_url="http://ha.test",
        home_assistant_token="test-token",
        webhook_secret="test-secret",
        vision_provider="mock",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        config_dir=FIXTURE_CONFIG,
        ingest_poll_seconds=0.01,
        freshness_poll_interval_seconds=0.001,
        dedupe_window_seconds=15,
        log_level="WARNING",
    )


@pytest.fixture
def home_config() -> HomeConfig:
    from hermes_home.config import load_home_config

    return load_home_config(FIXTURE_CONFIG)


@pytest.fixture
def cameras_config() -> CamerasConfig:
    from hermes_home.config import load_cameras_config

    return load_cameras_config(FIXTURE_CONFIG)


@pytest.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    """A migrated database.

    Uses the real Alembic chain rather than metadata.create_all, so a broken
    migration fails the suite instead of hiding until deployment.
    """
    engine = create_engine(settings.database_url)

    from alembic import command
    from alembic.config import Config

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    await asyncio.to_thread(command.upgrade, config, "head")

    yield engine
    await engine.dispose()


@pytest.fixture
async def session_factory(
    engine: AsyncEngine, home_config: HomeConfig, cameras_config: CamerasConfig
) -> async_sessionmaker:
    factory = create_session_factory(engine)
    async with session_scope(factory) as session:
        await seed_home(session, home_config, cameras_config)
    return factory


class FakeHomeAssistant:
    """A scriptable stand-in for the Home Assistant REST API.

    ``image_state_ts`` is what the freshness gate reads; advancing it simulates
    the camera integration publishing a new event still.
    """

    def __init__(
        self,
        *,
        image: bytes = TINY_IMAGE,
        image_state_ts: datetime | None = None,
        media_type: str = "image/png",
    ) -> None:
        self.image = image
        self.image_state_ts = image_state_ts or now_utc()
        #: Override the reported state, e.g. "unknown" after an HA reload.
        self.state_value: str | None = None
        self.media_type = media_type
        self.state_calls = 0
        self.image_calls = 0
        self.raise_on_state: Exception | None = None
        #: Publish the new image only from this poll onwards, reproducing the
        #: real camera's upload delay (measured ~3.7s on Eufy hardware).
        self.publish_on_call: int | None = None
        self.pending_image: bytes | None = None
        self.pending_state_ts: datetime | None = None
        self.raise_on_image: Exception | None = None

    async def get_entity_state(self, entity_id: str) -> EntityState:
        self.state_calls += 1
        if self.raise_on_state:
            raise self.raise_on_state
        if self.publish_on_call is not None and self.state_calls >= self.publish_on_call:
            # The camera has finished uploading: the entity now reports a real
            # timestamp and the proxy serves the new frame.
            if self.pending_image is not None:
                self.image = self.pending_image
            if self.pending_state_ts is not None:
                self.image_state_ts = self.pending_state_ts
            self.state_value = None
        return EntityState(
            entity_id=entity_id,
            state=(
                self.state_value
                if self.state_value is not None
                else self.image_state_ts.isoformat().replace("+00:00", "Z")
            ),
            attributes={},
            last_changed=self.image_state_ts,
            last_updated=self.image_state_ts,
        )

    async def get_image_entity_image(self, entity_id: str) -> tuple[bytes, str]:
        self.image_calls += 1
        if self.raise_on_image:
            raise self.raise_on_image
        return self.image, self.media_type

    async def get_camera_image(self, entity_id: str) -> tuple[bytes, str]:
        self.image_calls += 1
        return self.image, self.media_type

    async def aclose(self) -> None:
        return None


@pytest.fixture
def fake_ha() -> FakeHomeAssistant:
    return FakeHomeAssistant()
