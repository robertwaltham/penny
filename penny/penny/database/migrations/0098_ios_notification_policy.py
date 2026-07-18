"""Add durable iOS notification preferences and batching state."""

from __future__ import annotations

import sqlite3

_CATEGORIES = ("chat", "collector", "thoughts", "startup", "test_push")


def up(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ios_notification_policy (
            id INTEGER PRIMARY KEY,
            global_interval_seconds INTEGER NOT NULL DEFAULT 900,
            updated_at TIMESTAMP NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ios_notification_preference (
            category TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1,
            interval_seconds INTEGER,
            updated_at TIMESTAMP NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ios_notification_batch (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id INTEGER NOT NULL REFERENCES device(id),
            category TEXT NOT NULL,
            started_at TIMESTAMP NOT NULL,
            due_at TIMESTAMP NOT NULL,
            state TEXT NOT NULL DEFAULT 'open',
            lease_until TIMESTAMP,
            summary_sent_at TIMESTAMP,
            summary_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_ios_notification_batch_due
            ON ios_notification_batch(device_id, category, state, due_at);
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(ios_outbox)")}
    if "notification_category" not in columns:
        conn.execute(
            "ALTER TABLE ios_outbox ADD COLUMN notification_category "
            "TEXT NOT NULL DEFAULT 'collector'"
        )
    if "notification_batch_id" not in columns:
        conn.execute("ALTER TABLE ios_outbox ADD COLUMN notification_batch_id INTEGER")
    conn.execute(
        "INSERT OR IGNORE INTO ios_notification_policy(id, global_interval_seconds, updated_at) "
        "VALUES (1, 900, CURRENT_TIMESTAMP)"
    )
    for category in _CATEGORIES:
        conn.execute(
            "INSERT OR IGNORE INTO ios_notification_preference "
            "(category, enabled, interval_seconds, updated_at) "
            "VALUES (?, 1, NULL, CURRENT_TIMESTAMP)",
            (category,),
        )
    conn.commit()
