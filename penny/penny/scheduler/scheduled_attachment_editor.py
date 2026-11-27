"""Edit outgoing images for scheduled collection reports."""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from functools import partial
from typing import TYPE_CHECKING

from penny.image_utils import decode_data_uri
from penny.llm.comfyui_client import MAX_IMAGE_BYTES, ComfyUIError, ComfyUIRequestError
from penny.llm.models import LlmError, LlmMessage
from penny.llm.refusal import is_refusal

if TYPE_CHECKING:
    from penny.database.models import SendQueueItem
    from penny.database.send_queue_store import SendQueueStore
    from penny.llm.call_link import CallLink
    from penny.llm.client import LlmClient
    from penny.llm.comfyui_client import ComfyUIClient

logger = logging.getLogger(__name__)

_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_SUMMARY_INSTRUCTION = (
    "Summarize the final collection report as a short image-editing prompt. "
    "Describe the main subject, setting, and visual details supported by the report. "
    "Use the supplied reference image as the starting point. "
    "Treat the report as source material and return only the visual prompt."
)


class ScheduledAttachmentEditor:
    def __init__(
        self, model: LlmClient, comfyui: ComfyUIClient, queue: SendQueueStore, *, timeout: float
    ) -> None:
        self._model = model
        self._comfyui = comfyui
        self._queue = queue
        self._timeout = timeout

    async def start(
        self, report: str, attachments: list[str], *, call_link: CallLink, item: SendQueueItem
    ) -> asyncio.Task[list[str]] | None:
        """Summarize under the scheduler's foreground gate, then start the network work."""
        item_id = item.id
        if item_id is None:
            return None
        reference = decode_data_uri(attachments[0]) if attachments else None
        if reference is not None:
            data, mime_type = reference
            if mime_type not in _IMAGE_TYPES or not 0 < len(data) <= MAX_IMAGE_BYTES:
                reference = None
        if reference is None and item.image_prompt_id is None:
            return None

        deadline = self._queue.start_image_preparation(item_id, time.time() + self._timeout)
        if deadline is None:
            return None
        if deadline <= time.time():
            self._log_failure(item, TimeoutError())
            return None
        prompt = None
        if reference is not None and not item.image_prompt_acknowledged:
            try:
                async with asyncio.timeout(deadline - time.time()):
                    prompt = await self._summarize(report, item.collection, call_link)
            except (LlmError, TimeoutError) as exc:
                self._log_failure(item, exc)
            if prompt is None and item.image_prompt_id is None:
                return None
        if not self._queue.is_pending(item_id):
            return None
        return asyncio.create_task(
            self._edit(item, item_id, attachments, prompt, reference, deadline),
            name=f"scheduled_attachment_editor:{item_id}",
        )

    async def _edit(
        self,
        item: SendQueueItem,
        item_id: int,
        attachments: list[str],
        prompt: str | None,
        reference: tuple[bytes, str] | None,
        deadline: float,
    ) -> list[str]:
        try:
            async with asyncio.timeout(deadline - time.time()):
                prompt_id = item.image_prompt_id
                if prompt_id is not None and not item.image_prompt_acknowledged:
                    prompt_id = await self._recover_submission(item_id, prompt_id)
                if prompt_id is None and prompt is not None and reference is not None:
                    prompt_id = await self._submit(
                        item_id, prompt, reference, previous_id=item.image_prompt_id
                    )
                if prompt_id is None:
                    return attachments
                output = await self._comfyui.fetch_edit(prompt_id, image_count=1)
        except (ComfyUIError, TimeoutError) as exc:
            self._log_failure(item, exc)
            return attachments

        image = f"data:image/png;base64,{base64.b64encode(output).decode()}"
        return [image, *attachments[1:]]

    async def _submit(
        self, item_id: int, prompt: str, reference: tuple[bytes, str], *, previous_id: str | None
    ) -> str | None:
        reserved_id = None

        def reserve() -> str | None:
            nonlocal reserved_id
            reserved_id = self._queue.reserve_image_prompt(item_id, previous_prompt_id=previous_id)
            return reserved_id

        acknowledge = partial(self._queue.acknowledge_image_prompt, item_id)
        try:
            return await self._comfyui.submit_edit(
                prompt, [reference], reserve_prompt=reserve, acknowledge_prompt=acknowledge
            )
        except ComfyUIRequestError:
            if reserved_id is None:
                raise
            prompt_id = await self._recover_submission(item_id, reserved_id)
            if prompt_id is None:
                raise
            return prompt_id

    async def _recover_submission(self, item_id: int, reserved_id: str) -> str | None:
        prompt_id = await self._comfyui.recover_submission(reserved_id)
        if prompt_id is not None:
            self._queue.acknowledge_image_prompt(item_id, reserved_id, prompt_id)
        return prompt_id

    @staticmethod
    def _log_failure(item: SendQueueItem, exc: Exception) -> None:
        detail = str(exc) or "Timed out preparing the attachment."
        logger.warning("Scheduled image preparation failed for %s: %s", item.collection, detail)

    async def _summarize(self, report: str, collection: str, call_link: CallLink) -> str | None:
        response = await self._model.chat(
            messages=[
                LlmMessage(role="system", content=_SUMMARY_INSTRUCTION),
                LlmMessage(role="user", content=report),
            ],
            call_link=call_link,
            agent_name="scheduled_attachment_editor",
            prompt_type="scheduled_attachment_summary",
            run_target=collection,
            token_budget=160,
            thinking_enabled=False,
        )
        prompt = response.content.strip()
        if not prompt or is_refusal(prompt):
            logger.warning("No image prompt was produced for scheduled collection %s", collection)
            return None
        return prompt
