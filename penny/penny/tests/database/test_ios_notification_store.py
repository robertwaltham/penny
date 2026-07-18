"""Tests for durable iOS notification settings and batching state."""

from datetime import UTC, datetime, timedelta

import pytest

from penny.database import Database


def _db(tmp_path) -> Database:
    db = Database(str(tmp_path / "test.db"))
    db.create_tables()
    return db


def test_defaults_and_atomic_update(tmp_path):
    db = _db(tmp_path)
    settings = db.ios_notifications.settings()
    assert settings["global_interval_seconds"] == 900
    assert {item["id"] for item in settings["categories"]} == {
        "chat",
        "collector",
        "thoughts",
        "startup",
        "test_push",
    }

    updated = db.ios_notifications.update(
        1800,
        [
            {"id": item["id"], "enabled": item["id"] != "thoughts", "override_seconds": None}
            for item in settings["categories"]
        ],
    )
    assert updated["global_interval_seconds"] == 1800
    assert db.ios_notifications.category_enabled("thoughts") is False


def test_invalid_update_does_not_partially_change_state(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(ValueError):
        db.ios_notifications.update(17, [])
    assert db.ios_notifications.settings()["global_interval_seconds"] == 900


def test_open_and_attach_batch(tmp_path):
    db = _db(tmp_path)
    device = db.devices.register("ios", "device", "iPhone", is_default=True)
    assert device.id is not None
    now = datetime.now(UTC)
    batch = db.ios_notifications.open_batch(device.id, "thoughts", now)
    assert batch is not None
    due_at = batch.due_at.replace(tzinfo=UTC) if batch.due_at.tzinfo is None else batch.due_at
    assert due_at >= now + timedelta(seconds=900)
    reopened = db.ios_notifications.open_batch(device.id, "thoughts", now)
    assert reopened is not None and reopened.id == batch.id
