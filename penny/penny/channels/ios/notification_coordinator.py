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

    async def send_test_push(
        self,
        device_id: int,
        *,
        title: str,
        body: str,
        source_type: str,
        source_name: str,
    ) -> bool:
        """Send a diagnostic APNs notification without persisting an outbox item."""
        if self._apns is None:
            logger.info("Skipping iOS test push: APNs client unavailable (device_id=%s)", device_id)
            return False
        registration = self._db.ios.get_registration(device_id)
        if registration is None:
            logger.warning("Skipping iOS test push: no push registration (device_id=%s)", device_id)
            return False
        if not registration.push_enabled or not registration.apns_token:
            logger.warning(
                "Skipping iOS test push: push unavailable "
                "(device_id=%s, push_enabled=%s, has_apns_token=%s)",
                device_id,
                registration.push_enabled,
                bool(registration.apns_token),
            )
            return False

        badge = self._db.ios.pending_count(device_id)
        try:
            logger.info(
                "Sending iOS test push to APNs (device_id=%s, source_type=%s, "
                "source_name=%s, environment=%s, badge=%s)",
                device_id,
                source_type,
                source_name,
                registration.apns_environment,
                badge,
            )
            await self._apns.send_preview(
                device_token=registration.apns_token,
                title=title,
                body=body,
                badge=badge,
                outbox_id=None,
                source_type=source_type,
                source_name=source_name,
                thread_id=f"penny-{source_name}",
                environment=registration.apns_environment,
                notification_kind="test_push",
                category="test_push",
            )
        except ApnsError as error:
            logger.warning(
                "APNs rejected iOS test push (device_id=%s, status=%s, reason=%s)",
                device_id,
                error.status_code,
                error.reason,
            )
            if error.invalid_token:
                self._db.ios.disable_push(device_id)
            return False
        except Exception as error:
            logger.warning(
                "iOS test push delivery failed (device_id=%s, error=%s)", device_id, error
            )
            return False

        logger.info("APNs test push sent successfully (device_id=%s)", device_id)
        return True

    async def deliver_new_item(
        self, item: IosOutboxItem, *, connected: bool, force_push: bool = False
    ) -> None:
        if self._apns is None or item.id is None:
            logger.info(
                "Skipping iOS APNs notification (device_id=%s, outbox_id=%s, apns_client=%s)",
                item.device_id,
                item.id,
                self._apns is not None,
            )
            return
        if connected and not force_push:
            logger.info(
                "Skipping iOS APNs notification: device connected (device_id=%s, outbox_id=%s)",
                item.device_id,
                item.id,
            )
            return
        registration = self._db.ios.get_registration(item.device_id)
        if registration is None:
            logger.warning(
                "Skipping iOS APNs notification: no push registration (device_id=%s, outbox_id=%s)",
                item.device_id,
                item.id,
            )
            return
        if not registration.push_enabled or not registration.apns_token:
            logger.warning(
                "Skipping iOS APNs notification: push unavailable "
                "(device_id=%s, outbox_id=%s, push_enabled=%s, has_apns_token=%s)",
                item.device_id,
                item.id,
                registration.push_enabled,
                bool(registration.apns_token),
            )
            return
        category = item.notification_category
        logger.info(
            "Dispatching iOS notification (device_id=%s, outbox_id=%s, category=%s, "
            "force_push=%s, environment=%s)",
            item.device_id,
            item.id,
            category,
            force_push,
            registration.apns_environment,
        )
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
            logger.info(
                "APNs preview sent successfully (device_id=%s, outbox_id=%s)",
                item.device_id,
                item_id,
            )
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
