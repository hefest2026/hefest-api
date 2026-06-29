"""Unit tests for hefest.config settings validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hefest.config import WORKER_DB_POOL_HEADROOM, Settings


class TestWorkerPoolHeadroom:
    def test_defaults_satisfy_headroom(self) -> None:
        """The shipped defaults leave the required pool headroom."""
        settings = Settings()
        assert (
            settings.worker_db_pool_size
            >= settings.worker_send_concurrency + WORKER_DB_POOL_HEADROOM
        )

    def test_pool_smaller_than_concurrency_plus_headroom_is_rejected(self) -> None:
        """A pool too small for the configured concurrency fails fast."""
        with pytest.raises(ValidationError, match="worker_db_pool_size"):
            Settings(worker_send_concurrency=20, worker_db_pool_size=20)

    def test_pool_meeting_headroom_exactly_is_accepted(self) -> None:
        """Exactly concurrency + headroom is allowed (boundary)."""
        settings = Settings(
            worker_send_concurrency=20,
            worker_db_pool_size=20 + WORKER_DB_POOL_HEADROOM,
        )
        assert settings.worker_db_pool_size == 20 + WORKER_DB_POOL_HEADROOM
