"""Keep the ComfyUI job ID across scheduled report delivery attempts.

Type: schema
"""

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(send_queue)")}
    if columns and "image_prompt_id" not in columns:
        conn.execute("ALTER TABLE send_queue ADD COLUMN image_prompt_id VARCHAR")
