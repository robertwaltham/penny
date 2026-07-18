"""Deterministic iOS APNs notification policy and batching."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from penny.channels.ios.apns import ApnsError
from penny.database.models import IosOutboxItem

if TYPE_CHECKING:
    from penny.channels.ios.apns import ApnsClient
    from penny.database import Database

logger = logging.getLogger("penny.channels.ios.channel")


class IosNotificationCoordinator:
    name = "ios_notification_dispatch"

    def __init__(self, db: Database, apns: ApnsClient | None = None) -> None:
        self._db = db
        self._apns = apns
        self._channel = None

    def set_channel(self, channel) -> None:
        self._channel = channel

    async def deliver_new_item(
        self, item: IosOutboxItem, *, connected: bool, force_push: bool = False
    ) -> None:
        if self._apns is None or item.id is None:
            return
        if connected and not force_push:
            return
        registration = self._db.ios.get_registration(item.device_id)
        if registration is None or not registration.push_enabled or not registration.apns_token:
            return
        category = item.notification_category
        if force_push or category in {"chat", "test_push"}:
            await self._send(item, registration, notification_kind="preview")
            return
        if not self._db.ios_notifications.category_enabled(category):
            await self._send(
                item,
                registration,
                notification_kind="badge",
                alert=False,
                sound=None,
                collapse_id=f"penny-badge-{item.device_id}",
            )
            return
        interval = self._db.ios_notifications.effective_interval(category)
        if interval <= 0:
            await self._send(item, registration, notification_kind="preview")
            return
        item_id = item.id
        assert item_id is not None
        batch_id = self._db.ios_notifications.attach(item_id, item.device_id, category)
        if batch_id is not None:
            return
        if await self._send(item, registration, notification_kind="preview"):
            self._db.ios_notifications.open_batch(item.device_id, category, datetime.now(UTC))

    async def execute(self) -> bool:
        if self._apns is None:
            return False
        now = datetime.now(UTC).replace(tzinfo=None)
        did_work = bool(
            self._db.ios_notifications.release_expired_leases(now)
            or self._db.ios_notifications.cancel_empty()
        )
        for batch in self._db.ios_notifications.due_batches(now):
            if batch.id is None:
                continue
            if not self._db.ios_notifications.claim_due(batch.id, now):
                continue
            if self._channel is not None and self._channel.is_connected_device(batch.device_id):
                self._db.ios_notifications.mark_batch(batch.id, "sent", 0)
                did_work = True
                continue
            count = self._db.ios_notifications.unread_count(batch.id)
            if count == 0:
                self._db.ios_notifications.mark_batch(batch.id, "cancelled")
                did_work = True
                continue
            registration = self._db.ios.get_registration(batch.device_id)
            if registration is None or not registration.push_enabled or not registration.apns_token:
                continue
            try:
                await self._apns.send_preview(
                    device_token=registration.apns_token,
                    title="Hi from Penny",
                    body=_summary(batch.category, count),
                    badge=self._db.ios.pending_count(batch.device_id),
                    outbox_id=None,
                    source_type="collector",
                    source_name=batch.category,
                    thread_id=f"penny-{batch.category}",
                    environment=registration.apns_environment,
                    notification_kind="batch_summary",
                    batch_id=batch.id,
                    category=batch.category,
                    count=count,
                    collapse_id=f"penny-batch-{batch.id}",
                )
            except ApnsError as error:
                self._db.ios_notifications.mark_batch(batch.id, "open", count, error.reason)
                if error.invalid_token:
                    self._db.ios.disable_push(batch.device_id)
                continue
            self._db.ios_notifications.mark_batch(batch.id, "sent", count)
            did_work = True
        return did_work

    async def _send(self, item: IosOutboxItem, registration, **kwargs) -> bool:
        assert self._apns is not None
        try:
            logger.info(
                "Sending iOS preview notification to APNs "
                "(device_id=%s, outbox_id=%s, source_type=%s, source_name=%s)",
                item.device_id,
                item.id,
                item.source_type,
                item.source_name,
            )
            await self._apns.send_preview(
                device_token=registration.apns_token,
                title=item.push_title,
                body=item.push_summary,
                badge=self._db.ios.pending_count(item.device_id),
                outbox_id=item.id,
                source_type=item.source_type,
                source_name=item.source_name,
                thread_id=f"penny-{item.notification_category}",
                environment=registration.apns_environment,
                category=item.notification_category,
                **kwargs,
            )
            item_id = item.id
            assert item_id is not None
            self._db.ios.mark_push_sent(item_id)
            return True
        except ApnsError as error:
            item_id = item.id
            assert item_id is not None
            self._db.ios.mark_push_error(item_id, error.reason)
            if error.invalid_token:
                logger.warning(
                    "APNs rejected iOS device token; disabling push "
                    "(device_id=%s, outbox_id=%s, reason=%s)",
                    item.device_id,
                    item.id,
                    error.reason,
                )
                self._db.ios.disable_push(item.device_id)
            else:
                logger.warning(
                    "APNs rejected preview notification "
                    "(device_id=%s, outbox_id=%s, status=%s, reason=%s)",
                    item.device_id,
                    item.id,
                    error.status_code,
                    error.reason,
                )
            return False
        except Exception as error:
            item_id = item.id
            assert item_id is not None
            self._db.ios.mark_push_error(item_id, str(error))
            logger.warning("iOS notification delivery failed: %s", error)
            return False


def _summary(category: str, count: int) -> str:
    label = {"thoughts": "Thought", "collector": "Collector update"}.get(
        category, category.replace("_", " ").title()
    )
    plural = "s" if count != 1 else ""
    verb = "are" if count != 1 else "is"
    return f"{count} more {label} message{plural} {verb} available"
