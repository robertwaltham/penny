"""Tests for SendQueueDrainer — cooldown-honouring delivery of queued messages.

``send_message`` enqueues; this drainer delivers.  The cooldown logic is the
same gate ``send_message`` used to apply inline, just relocated so a cooldown
*delays* a message instead of dropping it:

- No prior Penny message (or the user has spoken since) → deliver immediately.
- A recent Penny message with no user reply since → hold until the flat
  ``SEND_COOLDOWN_SECONDS`` window elapses.

Delivery pops the oldest pending row (FIFO), one per tick, marks it sent, and
attributes it to the collection that queued it.

There are two delivery lanes (#1939), read off the row's own ``origin``: a cadence
cycle's message waits out the cooldown above, and a message from a cycle the user
pressed "run this now" on goes out on the next tick — they are sitting there waiting
for it.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from penny.agents.collector import Collector
from penny.agents.models import ControllerResponse
from penny.channels.signal.channel import SignalChannel
from penny.channels.turns import TurnRegistry
from penny.config_params import SCHEDULED_REPORT_IMAGE_EDITING, RuntimeParams
from penny.constants import (
    ChannelType,
    CycleTrigger,
    MutationAction,
    PennyConstants,
    QueueItemKind,
)
from penny.database import Database
from penny.database.memory import EntryInput
from penny.database.models import SendQueueItem
from penny.database.mutation_store import MutationDetail, render_mutation
from penny.llm.call_link import CallLink
from penny.llm.client import LlmClient
from penny.llm.comfyui_client import ComfyUIError
from penny.llm.models import LlmError, LlmMessage, LlmResponse
from penny.scheduler.base import BackgroundScheduler
from penny.scheduler.scheduled_attachment_editor import ScheduledAttachmentEditor
from penny.scheduler.schedules import PeriodicSchedule
from penny.scheduler.send_queue_drainer import SendQueueDrainer
from penny.tests.conftest import ONE_PX_PNG_B64, require_memory, wait_until
from penny.tests.schema_template import schema_only_db

_PENNY_LOG = PennyConstants.MEMORY_PENNY_MESSAGES_LOG
_USER_LOG = PennyConstants.MEMORY_USER_MESSAGES_LOG

_RECIPIENT = "+15551234567"  # the user's primary sender identity
# Penny's OWN Signal number (config.signal_number) — #2765's startup fix makes
# sure this is NEVER registered as a device.  Kept here only so tests can
# assert the drainer never resolves to it.
_PENNY_NUMBER = "+15559998888"
_IOS_IDENTIFIER = "ios-keychain-id"
_DISCORD_CHANNEL_ID = "9876543210"
_COLLECTION = "notified-thoughts"


def _make_db(tmp_path) -> Database:
    db = schema_only_db(str(tmp_path / "test.db"))
    # Marker rows so db.memory(...) dispatches the messagelog facades the
    # cooldown helpers read.
    db.memories.create_log(_PENNY_LOG, "outbound")
    db.memories.create_log(_USER_LOG, "inbound")
    db.users.save_info(
        sender=_RECIPIENT,
        name="user",
        location="New York",
        timezone="America/New_York",
        date_of_birth="1990-01-01",
    )
    # #2765: production now seeds the Signal default device as the USER's own
    # number — never Penny's own (config.signal_number, which is never
    # registered at all) — so the drainer's default-device read lands on the
    # user directly, with no special-casing.
    db.devices.register(ChannelType.SIGNAL, _RECIPIENT, "Signal", is_default=True)
    return db


def _make_config(cooldown_seconds: float = 600.0, *, db: Database | None = None):
    runtime = RuntimeParams(db=db, env_overrides={"SEND_COOLDOWN_SECONDS": cooldown_seconds})
    return type("Config", (), {"runtime": runtime})()


def _make_channel():
    return SimpleNamespace(send_response=AsyncMock(return_value=42))


class _IdleCollector:
    """A collector double exposing the one read the drainer makes off it (#3025):
    no job run in progress, always.  The tests that need a run ACTUALLY in
    progress drive a real ``Collector`` (below) — this only stands in for the
    ordinary-row tests, whose subject is the cooldown lanes, not the run gate."""

    def is_cycle_running(self) -> bool:
        return False


def _make_drainer(db, channel, cooldown_seconds: float = 600.0, *, collector=None):
    # The chat agent only matters to the compose path (#2656), which these
    # ordinary-row tests never reach — a mock stands in for it.
    drainer = SendQueueDrainer(
        db=db,
        config=_make_config(cooldown_seconds),
        chat_agent=MagicMock(),
        collector=collector if collector is not None else _IdleCollector(),
    )
    drainer.set_channel(channel)
    return drainer


def _penny_sent(db, content: str) -> None:
    db.messages.log_message(PennyConstants.MessageDirection.OUTGOING, "penny", content)


def _user_said(db, content: str) -> None:
    db.messages.log_message(PennyConstants.MessageDirection.INCOMING, _RECIPIENT, content)


@pytest.mark.asyncio
async def test_drain_delivers_when_no_prior_send(tmp_path):
    """Empty conversation → cooldown vacuously elapsed → deliver + mark sent."""
    db = _make_db(tmp_path)
    channel = _make_channel()
    drainer = _make_drainer(db, channel)
    db.send_queue.enqueue(content="hey there!", collection=_COLLECTION)

    did_work = await drainer.execute()

    assert did_work is True
    channel.send_response.assert_awaited_once()
    kwargs = channel.send_response.await_args.kwargs
    # Routes to the seeded Signal default device's identifier — the user's own
    # number (#2765) — never Penny's own, which is never registered at all.
    assert kwargs["recipient"] == _RECIPIENT
    assert kwargs["recipient"] != _PENNY_NUMBER
    assert kwargs["content"] == "hey there!"
    assert kwargs["author"] == _COLLECTION
    # Emission provenance (#1568): the queued row's collection is carried onto
    # send_response as the mechanism, so the delivered messagelog row names its
    # cause (NULL = direct reply).
    assert kwargs["mechanism"] == _COLLECTION
    # Row is stamped delivered — never re-sent.
    assert db.send_queue.next_pending() is None


@pytest.mark.asyncio
async def test_drain_holds_when_cooldown_not_elapsed(tmp_path):
    """Recent Penny send, no user reply since → hold the queued message."""
    db = _make_db(tmp_path)
    _penny_sent(db, "prior")  # count = 1, no user reply since
    channel = _make_channel()
    drainer = _make_drainer(db, channel, cooldown_seconds=3600.0)
    db.send_queue.enqueue(content="hey again!", collection=_COLLECTION)

    did_work = await drainer.execute()

    assert did_work is False
    channel.send_response.assert_not_awaited()
    # Still pending — delayed, not dropped.
    pending = db.send_queue.next_pending()
    assert pending is not None and pending.content == "hey again!"


@pytest.mark.asyncio
async def test_drain_delivers_when_user_replied_since_last_send(tmp_path):
    """User spoke since Penny's last send → conversational → deliver now."""
    db = _make_db(tmp_path)
    _penny_sent(db, "prior")
    _user_said(db, "actually, follow-up")
    channel = _make_channel()
    drainer = _make_drainer(db, channel, cooldown_seconds=3600.0)
    db.send_queue.enqueue(content="responding", collection=_COLLECTION)

    did_work = await drainer.execute()

    assert did_work is True
    channel.send_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_user_triggered_runs_message_skips_the_cooldown_a_cadence_one_waits(tmp_path):
    """The THREE lanes, in one world (#1939, #2964).

    The cooldown is anti-spam for messages nobody asked for.  A message from a cycle
    the user pressed "run this now" on, or a sandbox preview the user asked to run,
    is one they are sitting in front of waiting for, so both go out before the next
    tick — while the cadence message queued alongside them, older and otherwise
    first in line, keeps waiting.  Observed live: an on-demand run finished and its
    result then waited ~10.5 minutes because an unrelated chat reply seconds earlier
    had started the cooldown.
    """
    db = _make_db(tmp_path)
    _penny_sent(db, "prior")  # the cooldown is running: no user reply since
    channel = _make_channel()
    drainer = _make_drainer(db, channel, cooldown_seconds=3600.0)
    # The cadence message is OLDEST, so strict FIFO would hand it out first — asking
    # by lane is what stops it holding the other two's own results behind it.
    db.send_queue.enqueue(content="the daily digest", collection=_COLLECTION)
    db.send_queue.enqueue(
        content="your run finished",
        collection=_COLLECTION,
        origin=CycleTrigger.ON_DEMAND,
    )
    db.send_queue.enqueue(
        content="your preview finished",
        collection=_COLLECTION,
        origin=CycleTrigger.SANDBOX,
    )

    assert await drainer.execute() is True
    assert channel.send_response.await_args.kwargs["content"] == "your run finished"

    # The SANDBOX lane is tried next — also skips the cooldown, also ahead of the
    # still-older cadence message.
    channel.send_response.reset_mock()
    assert await drainer.execute() is True
    assert channel.send_response.await_args.kwargs["content"] == "your preview finished"

    # The cadence message is delayed, not dropped — and the next tick still holds it.
    channel.send_response.reset_mock()
    assert await drainer.execute() is False
    channel.send_response.assert_not_awaited()
    pending = db.send_queue.next_pending()
    assert pending is not None and pending.content == "the daily digest"


@pytest.mark.asyncio
async def test_drain_no_work_when_queue_empty(tmp_path):
    """Nothing queued → no work, no send."""
    db = _make_db(tmp_path)
    channel = _make_channel()
    drainer = _make_drainer(db, channel)

    did_work = await drainer.execute()

    assert did_work is False
    channel.send_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_drain_delivers_one_per_tick_in_fifo_order(tmp_path):
    """Two queued → a single execute() delivers only the oldest; the rest waits."""
    db = _make_db(tmp_path)
    channel = _make_channel()
    drainer = _make_drainer(db, channel)
    db.send_queue.enqueue(content="first", collection=_COLLECTION)
    db.send_queue.enqueue(content="second", collection=_COLLECTION)

    did_work = await drainer.execute()

    assert did_work is True
    channel.send_response.assert_awaited_once()
    assert channel.send_response.await_args.kwargs["content"] == "first"
    # Only the oldest is delivered this tick; the next stays pending.
    pending = db.send_queue.next_pending()
    assert pending is not None and pending.content == "second"


@pytest.mark.asyncio
async def test_drain_routes_to_ios_default_device_identifier(tmp_path):
    """iOS default device → the device identifier *is* the recipient.

    On iOS the registered device identifier is the delivery target (not the
    single primary sender), so the drainer must route drained messages there.
    """
    db = _make_db(tmp_path)
    # An iOS device registers as default — registration keeps a single default,
    # so it supersedes the seeded Signal device.
    db.devices.register(ChannelType.IOS, _IOS_IDENTIFIER, "iPhone", is_default=True)
    channel = _make_channel()
    drainer = _make_drainer(db, channel)
    db.send_queue.enqueue(content="ping", collection=_COLLECTION)

    did_work = await drainer.execute()

    assert did_work is True
    kwargs = channel.send_response.await_args.kwargs
    assert kwargs["recipient"] == _IOS_IDENTIFIER


@pytest.mark.asyncio
async def test_drainer_resolves_signal_default_to_the_users_number(tmp_path):
    """Signal's default device is the user's own number (#2765) — never
    Penny's own seeded endpoint, which is never registered at all."""
    db = _make_db(tmp_path)
    drainer = _make_drainer(db, _make_channel())

    recipient = drainer._resolve_recipient()

    assert recipient == _RECIPIENT
    assert recipient != _PENNY_NUMBER


@pytest.mark.asyncio
async def test_drainer_resolves_discord_default_to_the_channel_id(tmp_path):
    """Discord's default device identifier is the channel id — the only
    endpoint Discord's own ``_send_raw`` ever delivers to (it ignores its
    ``recipient`` argument and always posts to the configured channel), so
    reading the default device directly is correct there too, not merely
    tolerated: the primary sender it used to fall back to is a Discord USER
    id, which no device is ever registered under."""
    db = _make_db(tmp_path)
    db.devices.register(ChannelType.DISCORD, _DISCORD_CHANNEL_ID, "Discord", is_default=True)
    drainer = _make_drainer(db, _make_channel())

    recipient = drainer._resolve_recipient()

    assert recipient == _DISCORD_CHANNEL_ID
    assert recipient != _PENNY_NUMBER


@pytest.mark.asyncio
async def test_drainer_falls_back_to_primary_sender_with_no_default_device(tmp_path):
    """No device registered at all yet (a fresh Signal install before its
    first message ever arrives) — the drainer still has a recipient to try:
    the primary sender, the one case the simplified read can't cover."""
    db = schema_only_db(str(tmp_path / "test.db"))
    db.memories.create_log(_PENNY_LOG, "outbound")
    db.memories.create_log(_USER_LOG, "inbound")
    db.users.save_info(
        sender=_RECIPIENT,
        name="user",
        location="New York",
        timezone="America/New_York",
        date_of_birth="1990-01-01",
    )
    drainer = _make_drainer(db, _make_channel())

    assert drainer._resolve_recipient() == _RECIPIENT


@pytest.mark.asyncio
async def test_drain_no_channel_is_noop(tmp_path):
    """No channel wired → drainer reports no work rather than crashing."""
    db = _make_db(tmp_path)
    drainer = SendQueueDrainer(
        db=db, config=_make_config(), chat_agent=MagicMock(), collector=_IdleCollector()
    )
    db.send_queue.enqueue(content="queued", collection=_COLLECTION)

    assert await drainer.execute() is False
    # Message remains pending for when a channel is available.
    assert db.send_queue.next_pending() is not None


@pytest.mark.asyncio
async def test_deliver_composed_sends_without_queuing(tmp_path):
    """``deliver_composed`` (#2913) is the no-queue, no-idle-wait path a caller
    that already ran its own delivery turn uses — the startup changelog
    announcement. It records a send exactly like a drained row does: the same
    recipient resolution and the same messagelog stamp (mechanism, run id)."""
    db = _make_db(tmp_path)
    channel = _make_channel()
    drainer = _make_drainer(db, channel)

    delivered = await drainer.deliver_composed(
        "the announcement", collection=_COLLECTION, run_id="run-123"
    )

    assert delivered is True
    channel.send_response.assert_awaited_once()
    kwargs = channel.send_response.await_args.kwargs
    assert kwargs["content"] == "the announcement"
    assert kwargs["recipient"] == _RECIPIENT
    assert kwargs["mechanism"] == _COLLECTION
    assert kwargs["run_id"] == "run-123"
    # Nothing was ever written to the queue.
    assert db.send_queue.pending_items() == []


@pytest.mark.asyncio
async def test_deliver_composed_no_channel_or_recipient_reports_false(tmp_path):
    """No channel wired → ``deliver_composed`` reports failure rather than
    crashing, the same honesty ``execute()`` gives a queued row."""
    db = _make_db(tmp_path)
    drainer = SendQueueDrainer(
        db=db, config=_make_config(), chat_agent=MagicMock(), collector=_IdleCollector()
    )

    delivered = await drainer.deliver_composed(
        "the announcement", collection=_COLLECTION, run_id="run-123"
    )

    assert delivered is False


@pytest.mark.asyncio
async def test_archiving_collection_cancels_pending_sends(tmp_path):
    """Teardown means silence through the queue (#1634): a send queued while the
    collector was alive is cancelled when the collection is archived, so the
    drainer delivers NOTHING — even though the cooldown is vacuously elapsed and
    would otherwise let it through.  The row is stamped cancelled (a visible audit
    trail; ``sent_at`` stays NULL — it was never sent), and the archive's mutation
    event names the cancelled count."""
    db = _make_db(tmp_path)
    db.memories.create_collection("hedgehog-watch", "hedgehog sightings", created_by_run_id="run-c")
    db.send_queue.enqueue(content="a hedgehog!", collection="hedgehog-watch")

    db.memories.archive("hedgehog-watch", run_id="run-arch")

    channel = _make_channel()
    drainer = _make_drainer(db, channel)  # cooldown vacuously elapsed (no prior send)
    did_work = await drainer.execute()

    # Nothing delivered — the only queued row was cancelled by the archive.
    assert did_work is False
    channel.send_response.assert_not_awaited()
    assert db.send_queue.next_pending() is None
    assert db.send_queue.pending_items() == []

    # Visible cancellation: the row is kept (audit trail), stamped cancelled_at,
    # and sent_at stays NULL — the single-source-of-truth "was it sent" is clean.
    with Session(db.engine) as session:
        rows = session.exec(select(SendQueueItem)).all()
    assert len(rows) == 1
    assert rows[0].cancelled_at is not None
    assert rows[0].sent_at is None

    # The archive's mutation event names the count — the legible surface the
    # cancellation renders on (self-state activity, memory_get).
    events = db.mutations.history("hedgehog-watch", limit=10)
    archive_event = next(e for e in events if e.action == MutationAction.ARCHIVED.value)
    assert archive_event.detail is not None
    assert (
        MutationDetail.model_validate_json(archive_event.detail).note == "cancelled 1 pending send"
    )
    assert render_mutation(archive_event).endswith("— cancelled 1 pending send")


@pytest.mark.asyncio
async def test_archive_cancel_is_scoped_and_pluralizes(tmp_path):
    """Cancellation is scoped to the archived collection's rows: a sibling
    collection's pending send survives and is what the drainer delivers, while
    both of the archived collection's rows are cancelled and the count pluralizes
    (#1634)."""
    db = _make_db(tmp_path)
    db.memories.create_collection("hedgehog-watch", "hedgehog sightings")
    db.memories.create_collection("weather-watch", "weather")
    db.send_queue.enqueue(content="a hedgehog!", collection="hedgehog-watch")
    db.send_queue.enqueue(content="another hedgehog!", collection="hedgehog-watch")
    db.send_queue.enqueue(content="rain today", collection="weather-watch")

    db.memories.archive("hedgehog-watch", run_id="run-arch")

    # The sibling's send is untouched — the drainer delivers it.
    channel = _make_channel()
    drainer = _make_drainer(db, channel)
    assert await drainer.execute() is True
    assert channel.send_response.await_args.kwargs["content"] == "rain today"

    # Both of the archived collection's rows are cancelled (kept, sent_at NULL).
    with Session(db.engine) as session:
        hedgehog = session.exec(
            select(SendQueueItem).where(SendQueueItem.collection == "hedgehog-watch")
        ).all()
    assert len(hedgehog) == 2
    assert all(row.cancelled_at is not None and row.sent_at is None for row in hedgehog)

    events = db.mutations.history("hedgehog-watch", limit=10)
    archive_event = next(e for e in events if e.action == MutationAction.ARCHIVED.value)
    assert archive_event.detail is not None
    assert (
        MutationDetail.model_validate_json(archive_event.detail).note == "cancelled 2 pending sends"
    )


@pytest.mark.asyncio
async def test_unarchive_does_not_resurrect_cancelled_sends(tmp_path):
    """A cancelled send belongs to the dead epoch — unarchiving the collection
    does NOT bring it back (#1634): the row stays cancelled and the drainer never
    delivers it."""
    db = _make_db(tmp_path)
    db.memories.create_collection("hedgehog-watch", "hedgehog sightings")
    db.send_queue.enqueue(content="a hedgehog!", collection="hedgehog-watch")
    db.memories.archive("hedgehog-watch")

    db.memories.unarchive("hedgehog-watch")

    channel = _make_channel()
    drainer = _make_drainer(db, channel)
    assert await drainer.execute() is False
    channel.send_response.assert_not_awaited()
    assert db.send_queue.next_pending() is None
    # The row is still cancelled — unarchive is not a resurrection.
    with Session(db.engine) as session:
        rows = session.exec(select(SendQueueItem)).all()
    assert len(rows) == 1 and rows[0].cancelled_at is not None


# ── The run gate (#3025): a job run in progress holds queued reports back ──

_REPORT_COLLECTION = "indie-release-watch"


class _GatedLlm(LlmClient):
    """A model client that holds its first call open until the test releases it —
    the deterministic stand-in for a job run mid-cycle, no timers.  The test
    awaits the collector's own ``is_cycle_running()`` to see the run start, then
    sets ``release``; the canned text response ends the cycle, which settles and
    frees the cycle lock."""

    def __init__(self) -> None:
        super().__init__("http://localhost:9", "test-model", max_retries=1, retry_delay=0.0)
        self.release = asyncio.Event()

    async def chat(
        self,
        messages: list[LlmMessage],
        tools: list[dict] | None = None,
        format: dict | str | None = None,
        *,
        call_link: CallLink,
        agent_name: str | None = None,
        prompt_type: str | None = None,
        run_target: str | None = None,
        token_budget: int | None = None,
        thinking_enabled: bool = True,
        skip_cache_store: bool = False,
        chat_slot: bool = False,
    ) -> LlmResponse:
        await self.release.wait()
        return LlmResponse(
            message=LlmMessage(role="assistant", content="All caught up."),
            model=self.model,
        )


def _make_real_collector(db: Database, test_config, model_client) -> Collector:
    """A real Collector wired to the given model client — the genuine run path
    (``run_for`` → ``_execute_cycle`` → ``_run_locked_cycle`` under the cycle
    lock) is what holds the gate closed in the tests below.  ``run_for`` is the
    user-requested path that lives OUTSIDE the scheduler — the case the cooldown
    never kept apart from a queued report's delivery turn; the scheduled and
    sandbox paths hold the same lock, so the gate reads them the same way."""
    return Collector(
        model_client=model_client,
        db=db,
        config=test_config,
        embedding_model_client=LlmClient(
            "http://localhost:9", "test-model", max_retries=1, retry_delay=0.0
        ),
        process_started_at=datetime(2000, 1, 1, tzinfo=UTC),
    )


def _seed_report_row(
    db: Database,
    run_id: str,
    origin: CycleTrigger = CycleTrigger.ON_DEMAND,
) -> None:
    """A buildable composing report row: the collection plus the entry its run
    wrote, so once the gate opens the compose path can actually assemble the
    fact set instead of failing the row."""
    db.memories.create_collection(
        _REPORT_COLLECTION,
        "New indie releases",
        extraction_prompt="Check the store page for new releases.",
        notify=True,
    )
    require_memory(db, _REPORT_COLLECTION).write_rule_decided(
        [
            EntryInput(
                key="Astral Drift",
                content="Astral Drift — a new indie metroidvania, announced for spring.",
            )
        ],
        author="producer",
        run_id=run_id,
    )
    db.send_queue.enqueue_report(_REPORT_COLLECTION, origin, run_id, kind=QueueItemKind.REPORT)


@pytest.mark.parametrize(
    ("origin", "configured", "enabled", "has_image", "failure"),
    [
        (CycleTrigger.CADENCE, True, True, True, None),
        (CycleTrigger.CADENCE, True, True, True, "summary"),
        (CycleTrigger.CADENCE, True, True, True, "edit"),
        (CycleTrigger.CADENCE, True, True, False, None),
        (CycleTrigger.CADENCE, True, False, True, None),
        (CycleTrigger.CADENCE, False, True, True, None),
        (CycleTrigger.ON_DEMAND, True, True, True, None),
    ],
)
async def test_report_attachment_uses_the_final_report_as_a_comfyui_prompt_source(
    tmp_path, monkeypatch, origin, configured, enabled, has_image, failure
):
    db = _make_db(tmp_path)
    _seed_report_row(db, "scheduled-run", origin)
    config = _make_config(db=db)
    config.runtime.set_configured(SCHEDULED_REPORT_IMAGE_EDITING, configured)
    if enabled:
        db.config.set(SCHEDULED_REPORT_IMAGE_EDITING, "1", "Edit scheduled report attachments")
    source_url = "https://example.com/astral"
    final_report = f"Astral Drift arrives in spring. See {source_url}"
    original = base64.b64decode(ONE_PX_PNG_B64)
    if has_image:
        db.media.put(data=original, mime_type="image/png", source_url=source_url)

    chat = MagicMock()
    chat.handle_job_report = AsyncMock(return_value=ControllerResponse(answer=final_report))
    model = AsyncMock()
    prompt = "An astronaut drifts past a planet in bloom."
    model.chat.return_value = LlmResponse(message=LlmMessage(role="assistant", content=prompt))
    comfyui = AsyncMock()

    def submit_edit(description, images, *, reserve_prompt, acknowledge_prompt):
        prompt_id = reserve_prompt()
        assert prompt_id is not None
        acknowledge_prompt(prompt_id, prompt_id)
        return prompt_id

    comfyui.submit_edit.side_effect = submit_edit
    comfyui.fetch_edit.return_value = b"mocked edited PNG"
    if failure == "summary":
        model.chat.side_effect = LlmError("model unavailable")
    elif failure == "edit":
        comfyui.fetch_edit.side_effect = ComfyUIError("image generation unavailable")

    channel = SignalChannel(
        "http://signal.test", _PENNY_NUMBER, chat, db, turn_registry=TurnRegistry()
    )
    send_raw = AsyncMock(return_value=42)
    monkeypatch.setattr(channel, "_send_raw", send_raw)
    drainer = SendQueueDrainer(
        db,
        config,
        chat,
        _IdleCollector(),
        attachment_editor=ScheduledAttachmentEditor(model, comfyui, db.send_queue, timeout=5)
        if configured
        else None,
    )
    drainer.set_channel(channel)
    try:
        if not await drainer.execute():
            await wait_until(
                lambda: all(pending.task.done() for pending in drainer._image_edits.values())
            )
            assert await drainer.execute()
    finally:
        await drainer.close()
        await channel.close()

    should_summarize = configured and enabled and has_image and origin == CycleTrigger.CADENCE
    if should_summarize:
        assert model.chat.await_count == 1
        request = model.chat.await_args.kwargs
        assert request["messages"][-1].content == final_report
        assert request["call_link"].parent_run_id == "scheduled-run"
    else:
        model.chat.assert_not_awaited()
    if should_summarize and failure != "summary":
        comfyui.submit_edit.assert_awaited_once()
        assert comfyui.submit_edit.await_args.args == (prompt, [(original, "image/png")])
        comfyui.fetch_edit.assert_awaited_once()
    else:
        comfyui.submit_edit.assert_not_awaited()
        comfyui.fetch_edit.assert_not_awaited()

    expected = None
    if has_image:
        image = (
            comfyui.fetch_edit.return_value if should_summarize and failure is None else original
        )
        expected = [f"data:image/png;base64,{base64.b64encode(image).decode()}"]
    assert send_raw.await_args is not None
    assert send_raw.await_args.args[1:3] == (final_report, expected)
    stored = db.messages.find_outgoing_by_content(final_report)
    assert stored is not None and stored.id is not None
    report_call = chat.handle_job_report.await_args
    assert report_call is not None and stored.run_id == report_call.kwargs["call_link"].run_id
    assert db.messages.attachments_for_message_ids([stored.id]).get(stored.id) == expected
    assert db.send_queue.next_pending() is None


@pytest.fixture
async def scheduled_edit(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    _seed_report_row(db, "scheduled-run", CycleTrigger.CADENCE)
    config = _make_config(cooldown_seconds=0, db=db)
    config.runtime.set_configured(SCHEDULED_REPORT_IMAGE_EDITING, True)
    db.config.set(SCHEDULED_REPORT_IMAGE_EDITING, "1", "Edit scheduled report attachments")
    source_url = "https://example.com/astral"
    report = f"Astral Drift arrives in spring. See {source_url}"
    db.media.put(
        data=base64.b64decode(ONE_PX_PNG_B64), mime_type="image/png", source_url=source_url
    )
    chat = MagicMock()
    chat.handle_job_report = AsyncMock(return_value=ControllerResponse(answer=report))
    model = AsyncMock()
    model.chat.return_value = LlmResponse(
        message=LlmMessage(role="assistant", content="An astronaut near a planet.")
    )
    comfyui = AsyncMock()
    comfyui.fetch_edit.return_value = b"mocked edited PNG"
    send_raw = AsyncMock(return_value=42)
    channel = SignalChannel(
        "http://signal.test", _PENNY_NUMBER, chat, db, turn_registry=TurnRegistry()
    )
    monkeypatch.setattr(channel, "_send_raw", send_raw)
    drainers = []

    def make_drainer(*, timeout=5):
        drainer = SendQueueDrainer(
            db,
            config,
            chat,
            _IdleCollector(),
            attachment_editor=ScheduledAttachmentEditor(
                model, comfyui, db.send_queue, timeout=timeout
            ),
        )
        drainer.set_channel(channel)
        drainers.append(drainer)
        return drainer

    try:
        yield SimpleNamespace(
            db=db,
            chat=chat,
            model=model,
            comfyui=comfyui,
            send_raw=send_raw,
            make_drainer=make_drainer,
            report=report,
        )
    finally:
        for drainer in drainers:
            await drainer.close()
        await channel.close()


@pytest.mark.parametrize(
    "interrupted_state", ["before_acceptance", "lost_response", "acknowledged"]
)
async def test_restart_reconciles_scheduled_image_submission(scheduled_edit, interrupted_state):
    edit = scheduled_edit
    interrupted = asyncio.Event()
    server_prompt_id = str(uuid4())

    async def submit_edit(description, images, *, reserve_prompt, acknowledge_prompt):
        requested_id = reserve_prompt()
        assert requested_id is not None
        pending = edit.db.send_queue.next_pending()
        assert pending is not None and pending.image_prompt_id == requested_id
        assert not pending.image_prompt_acknowledged
        if edit.comfyui.submit_edit.await_count == 1:
            if interrupted_state == "lost_response":
                edit.comfyui.recover_submission.side_effect = lambda reserved_id: (
                    server_prompt_id if reserved_id == requested_id else None
                )
            if interrupted_state != "acknowledged":
                interrupted.set()
                await asyncio.Future()
        # Some servers return a replacement for the requested ID.
        acknowledge_prompt(requested_id, server_prompt_id)
        return server_prompt_id

    async def fetch_edit(prompt_id, *, image_count):
        assert image_count == 1
        if interrupted_state == "acknowledged" and edit.comfyui.fetch_edit.await_count == 1:
            interrupted.set()
            await asyncio.Future()
        return b"mocked edited PNG"

    edit.comfyui.submit_edit.side_effect = submit_edit
    edit.comfyui.fetch_edit.side_effect = fetch_edit
    edit.comfyui.recover_submission.return_value = None
    initial = edit.make_drainer()

    assert not await initial.execute()
    await asyncio.wait_for(interrupted.wait(), timeout=3)
    await initial.close()

    pending = edit.db.send_queue.next_pending()
    assert pending is not None and pending.image_prompt_id is not None
    assert pending.image_prompt_acknowledged == (interrupted_state == "acknowledged")
    if pending.image_prompt_acknowledged:
        assert pending.image_prompt_id == server_prompt_id
    resumed = edit.make_drainer(timeout=60)
    assert not await resumed.execute()
    await wait_until(lambda: resumed._image_edits[pending.id].task.done(), timeout=3)
    current = edit.db.send_queue.next_pending()
    assert current is not None and current.image_prompt_acknowledged
    assert current.image_deadline == pending.image_deadline
    assert await resumed.execute()

    edit.chat.handle_job_report.assert_awaited_once()
    assert edit.comfyui.submit_edit.await_count == (
        2 if interrupted_state == "before_acceptance" else 1
    )
    assert edit.comfyui.fetch_edit.await_args.args == (server_prompt_id,)
    edit.send_raw.assert_awaited_once()
    assert edit.send_raw.await_args.args[2] == [
        f"data:image/png;base64,{base64.b64encode(b'mocked edited PNG').decode()}"
    ]


async def test_scheduled_wait_leaves_scheduler_free_and_archive_prevents_delivery(scheduled_edit):
    edit = scheduled_edit
    fetching = asyncio.Event()
    next_task_ran = asyncio.Event()
    release_result = asyncio.Event()

    def submit_edit(description, images, *, reserve_prompt, acknowledge_prompt):
        prompt_id = reserve_prompt()
        acknowledge_prompt(prompt_id, prompt_id)
        return prompt_id

    async def fetch_edit(prompt_id, *, image_count):
        fetching.set()
        await release_result.wait()
        return b"mocked edited PNG"

    class OtherTask:
        name = "other_task"

        async def execute(self) -> bool:
            await fetching.wait()
            next_task_ran.set()
            return False

    edit.comfyui.submit_edit.side_effect = submit_edit
    edit.comfyui.fetch_edit.side_effect = fetch_edit
    drainer = edit.make_drainer()
    scheduler = BackgroundScheduler(
        [
            PeriodicSchedule(drainer, lambda: 0.01, lambda: 0),
            PeriodicSchedule(OtherTask(), lambda: 0.01, lambda: 0),
        ],
        tick_interval=0.01,
    )
    row = edit.db.send_queue.next_pending()
    task = asyncio.create_task(scheduler.run())
    try:
        await asyncio.wait_for(next_task_ran.wait(), timeout=1)
        edit.db.send_queue.enqueue("The second report is ready.", "other")
        await wait_until(lambda: edit.send_raw.await_count == 1, timeout=1)
        assert edit.send_raw.await_args.args[1] == "The second report is ready."
        assert fetching.is_set() and not release_result.is_set()

        scheduler.notify_foreground_start()
        edit.db.memories.archive(_REPORT_COLLECTION)
        release_result.set()
        await wait_until(lambda: drainer._image_edits[row.id].task.done(), timeout=1)
        edit.send_raw.assert_awaited_once()
        scheduler.notify_foreground_end()
        await wait_until(lambda: not drainer._image_edits, timeout=1)
    finally:
        release_result.set()
        scheduler.stop()
        await asyncio.wait_for(task, timeout=2)
    edit.send_raw.assert_awaited_once()
    assert edit.db.messages.find_outgoing_by_content(edit.report) is None
    with Session(edit.db.engine) as session:
        cancelled = session.get(SendQueueItem, row.id)
        assert cancelled is not None and cancelled.cancelled_at and cancelled.sent_at is None


async def test_expired_scheduled_edit_falls_back_after_restart(scheduled_edit):
    edit = scheduled_edit
    row = edit.db.send_queue.next_pending()
    deadline = datetime.now(UTC).timestamp() - 1
    edit.db.send_queue.start_image_preparation(row.id, deadline)
    assert await edit.make_drainer().execute()
    edit.comfyui.fetch_edit.assert_not_awaited()
    edit.comfyui.submit_edit.assert_not_awaited()
    assert edit.send_raw.await_args.args[2] == [f"data:image/png;base64,{ONE_PX_PNG_B64}"]


async def test_turning_off_scheduled_edit_sends_original_while_edit_is_pending(scheduled_edit):
    edit = scheduled_edit
    fetching = asyncio.Event()

    def submit_edit(description, images, *, reserve_prompt, acknowledge_prompt):
        prompt_id = reserve_prompt()
        acknowledge_prompt(prompt_id, prompt_id)
        return prompt_id

    async def fetch_edit(prompt_id, *, image_count):
        fetching.set()
        await asyncio.Future()

    edit.comfyui.submit_edit.side_effect = submit_edit
    edit.comfyui.fetch_edit.side_effect = fetch_edit
    drainer = edit.make_drainer()
    assert not await drainer.execute()
    await asyncio.wait_for(fetching.wait(), timeout=1)
    edit.db.config.set(SCHEDULED_REPORT_IMAGE_EDITING, "0", "Edit scheduled report attachments")
    assert await drainer.execute()
    assert not drainer._image_edits
    edit.send_raw.assert_awaited_once()
    assert edit.send_raw.await_args.args[2] == [f"data:image/png;base64,{ONE_PX_PNG_B64}"]


@pytest.mark.asyncio
async def test_a_queued_report_is_not_delivered_while_a_job_run_is_in_progress(
    tmp_path, test_config
):
    """#3025: with a job run mid-cycle, the drainer holds its whole tick back —
    the queued report stays pending, nothing is composed, the channel hears
    nothing.  The run here is a real user-requested one (``run_for``), the path
    outside the scheduler the old idle/cooldown gates never saw."""
    db = _make_db(tmp_path)
    channel = _make_channel()
    model = _GatedLlm()
    collector = _make_real_collector(db, test_config, model)
    drainer = _make_drainer(db, channel, collector=collector)
    _seed_report_row(db, "run-report-1")

    cycle = asyncio.create_task(collector.run_for(_REPORT_COLLECTION))
    await wait_until(collector.is_cycle_running)

    assert await drainer.execute() is False
    channel.send_response.assert_not_awaited()
    # Delayed, never dropped: the row is exactly as it was.
    assert db.send_queue.next_pending() is not None

    model.release.set()
    await cycle


@pytest.mark.asyncio
async def test_a_queued_report_is_delivered_once_the_cycle_ends(tmp_path, test_config):
    """#3025: the same report, held back while the cycle ran, goes out on the
    first tick with no run in progress."""
    db = _make_db(tmp_path)
    channel = _make_channel()
    model = _GatedLlm()
    collector = _make_real_collector(db, test_config, model)
    # The compose's delivery turn — a canned reply is all this test needs from it;
    # the turn itself is test_report_delivery.py's subject.
    chat = MagicMock()
    chat.handle_job_report = AsyncMock(
        return_value=ControllerResponse(answer="Astral Drift was announced for spring!")
    )
    drainer = SendQueueDrainer(db=db, config=_make_config(), chat_agent=chat, collector=collector)
    drainer.set_channel(channel)
    _seed_report_row(db, "run-report-1")

    cycle = asyncio.create_task(collector.run_for(_REPORT_COLLECTION))
    await wait_until(collector.is_cycle_running)
    assert await drainer.execute() is False
    model.release.set()
    await cycle
    await wait_until(lambda: not collector.is_cycle_running())

    assert await drainer.execute() is True
    channel.send_response.assert_awaited_once()
    assert db.send_queue.next_pending() is None


@pytest.mark.asyncio
async def test_delivery_is_unchanged_when_no_job_run_is_in_progress(tmp_path, test_config):
    """#3025: with a real collector sitting idle (no run in progress), delivery
    behaves exactly as before — the queued message goes out this tick."""
    db = _make_db(tmp_path)
    channel = _make_channel()
    collector = _make_real_collector(
        db,
        test_config,
        LlmClient("http://localhost:9", "test-model", max_retries=1, retry_delay=0.0),
    )
    drainer = _make_drainer(db, channel, collector=collector)
    db.send_queue.enqueue(content="all quiet on the watch", collection=_COLLECTION)

    assert await drainer.execute() is True
    channel.send_response.assert_awaited_once()
    assert db.send_queue.next_pending() is None
