"""Preserve the image wait deadline and distinguish reservation from acceptance.

Type: schema
"""

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(send_queue)")}
    if not columns:
        return
    if "image_prompt_acknowledged" not in columns:
        conn.execute(
            "ALTER TABLE send_queue ADD COLUMN image_prompt_acknowledged BOOLEAN NOT NULL DEFAULT 0"
        )
    if "image_deadline" not in columns:
        conn.execute("ALTER TABLE send_queue ADD COLUMN image_deadline FLOAT")
    if "delivery_run_id" not in columns:
        conn.execute("ALTER TABLE send_queue ADD COLUMN delivery_run_id VARCHAR")
