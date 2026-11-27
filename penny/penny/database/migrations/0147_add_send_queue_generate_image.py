"""Store a scheduled collection's image request on its queued report.

Type: schema
"""

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(send_queue)")}
    if columns and "generate_image" not in columns:
        conn.execute("ALTER TABLE send_queue ADD COLUMN generate_image BOOLEAN NOT NULL DEFAULT 0")
