"""Persistence for account-wide iOS notification settings and batch windows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlmodel import Session, select

from penny.database.models import (
    IosNotificationBatch,
    IosNotificationPolicy,
    IosNotificationPreference,
    IosOutboxItem,
)

CATEGORIES = ("chat", "collector", "thoughts", "startup", "test_push")
PRESETS = (0, 300, 900, 1800, 3600)


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


class IosNotificationStore:
    def __init__(self, engine):
        self.engine = engine

    def _session(self) -> Session:
        return Session(self.engine)

    def settings(self) -> dict:
        with self._session() as session:
            policy = session.get(IosNotificationPolicy, 1)
            if policy is None:
                policy = IosNotificationPolicy(id=1)
                session.add(policy)
                for category in CATEGORIES:
                    session.add(IosNotificationPreference(category=category))
                session.commit()
            rows = {
                row.category: row for row in session.exec(select(IosNotificationPreference)).all()
            }
            return {
                "global_interval_seconds": policy.global_interval_seconds,
                "categories": [
                    {
                        "id": category,
                        "enabled": rows.get(
                            category, IosNotificationPreference(category=category)
                        ).enabled,
                        "override_seconds": rows.get(category).interval_seconds
                        if category in rows
                        else None,
                        "effective_interval_seconds": (
                            rows[category].interval_seconds
                            if category in rows and rows[category].interval_seconds is not None
                            else policy.global_interval_seconds
                            if category != "chat"
                            else 0
                        ),
                    }
                    for category in CATEGORIES
                ],
            }

    def update(self, global_interval_seconds: int | None, categories: list[dict]) -> dict:
        if global_interval_seconds not in PRESETS:
            raise ValueError("unsupported global notification interval")
        incoming = {item.get("id"): item for item in categories}
        if set(incoming) != set(CATEGORIES):
            raise ValueError("all notification categories are required")
        for category, item in incoming.items():
            override = item.get("override_seconds")
            if override is not None and override not in PRESETS:
                raise ValueError(f"unsupported interval for {category}")
        now = _utc_now()
        with self._session() as session:
            policy = session.get(IosNotificationPolicy, 1) or IosNotificationPolicy(id=1)
            policy.global_interval_seconds = global_interval_seconds
            policy.updated_at = now
            session.add(policy)
            for category in CATEGORIES:
                row = session.get(IosNotificationPreference, category) or IosNotificationPreference(
                    category=category
                )
                row.enabled = bool(incoming[category].get("enabled", True))
                row.interval_seconds = incoming[category].get("override_seconds")
                row.updated_at = now
                session.add(row)
            session.commit()
        return self.settings()

    def category_enabled(self, category: str) -> bool:
        with self._session() as session:
            row = session.get(IosNotificationPreference, category)
            return row is None or row.enabled

    def effective_interval(self, category: str) -> int:
        if category == "chat":
            return 0
        with self._session() as session:
            policy = session.get(IosNotificationPolicy, 1)
            row = session.get(IosNotificationPreference, category)
            return (
                row.interval_seconds
                if row and row.interval_seconds is not None
                else policy.global_interval_seconds
                if policy
                else 900
            )

    def open_batch(
        self, device_id: int, category: str, now: datetime
    ) -> IosNotificationBatch | None:
        now = _naive_utc(now)
        with self._session() as session:
            existing = session.exec(
                select(IosNotificationBatch).where(
                    IosNotificationBatch.device_id == device_id,
                    IosNotificationBatch.category == category,
                    IosNotificationBatch.state == "open",
                )
            ).first()
            if existing:
                return existing
            batch = IosNotificationBatch(
                device_id=device_id,
                category=category,
                started_at=now,
                due_at=now + timedelta(seconds=self.effective_interval(category)),
            )
            session.add(batch)
            session.commit()
            session.refresh(batch)
            return batch

    def attach(self, item_id: int, device_id: int, category: str) -> int | None:
        with self._session() as session:
            batch = session.exec(
                select(IosNotificationBatch).where(
                    IosNotificationBatch.device_id == device_id,
                    IosNotificationBatch.category == category,
                    IosNotificationBatch.state == "open",
                )
            ).first()
            if batch is None or batch.id is None:
                return None
            item = session.get(IosOutboxItem, item_id)
            if item is None:
                return None
            item.notification_batch_id = batch.id
            session.add(item)
            session.commit()
            return batch.id

    def due_batches(self, now: datetime, limit: int = 20) -> list[IosNotificationBatch]:
        with self._session() as session:
            return list(
                session.exec(
                    select(IosNotificationBatch)
                    .where(
                        IosNotificationBatch.state == "open",
                        IosNotificationBatch.due_at <= now,
                    )
                    .order_by(cast(Any, IosNotificationBatch.due_at))
                    .limit(limit)
                ).all()
            )

    def claim_due(self, batch_id: int, now: datetime, lease_seconds: int = 120) -> bool:
        now = _naive_utc(now)
        with self._session() as session:
            batch = session.get(IosNotificationBatch, batch_id)
            if batch is None or batch.state != "open" or batch.due_at > now:
                return False
            batch.state = "sending"
            batch.lease_until = now + timedelta(seconds=lease_seconds)
            session.add(batch)
            session.commit()
            return True

    def release_expired_leases(self, now: datetime) -> int:
        now = _naive_utc(now)
        with self._session() as session:
            rows = session.exec(
                select(IosNotificationBatch).where(
                    IosNotificationBatch.state == "sending",
                    cast(Any, IosNotificationBatch.lease_until) < now,
                )
            ).all()
            for batch in rows:
                batch.state = "open"
                batch.lease_until = None
                session.add(batch)
            session.commit()
            return len(rows)

    def unread_count(self, batch_id: int) -> int:
        with self._session() as session:
            return len(
                session.exec(
                    select(IosOutboxItem).where(
                        IosOutboxItem.notification_batch_id == batch_id,
                        cast(Any, IosOutboxItem.acked_at).is_(None),
                    )
                ).all()
            )

    def mark_batch(
        self, batch_id: int, state: str, count: int = 0, error: str | None = None
    ) -> None:
        with self._session() as session:
            batch = session.get(IosNotificationBatch, batch_id)
            if batch is None:
                return
            batch.state = state
            batch.lease_until = None
            batch.summary_count = count
            batch.last_error = error
            batch.summary_sent_at = _utc_now() if state == "sent" else batch.summary_sent_at
            session.add(batch)
            session.commit()

    def cancel_empty(self) -> int:
        count = 0
        with self._session() as session:
            batches = session.exec(
                select(IosNotificationBatch).where(IosNotificationBatch.state == "open")
            ).all()
            for batch in batches:
                unread = session.exec(
                    select(IosOutboxItem).where(
                        IosOutboxItem.notification_batch_id == batch.id,
                        cast(Any, IosOutboxItem.acked_at).is_(None),
                    )
                ).first()
                if unread is None:
                    batch.state = "cancelled"
                    session.add(batch)
                    count += 1
            session.commit()
        return count
