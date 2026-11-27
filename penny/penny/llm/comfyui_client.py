"""Run the DungeonsAndLlamas FLUX.2 Klein API workflows on ComfyUI."""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from secrets import randbits
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4
from weakref import WeakValueDictionary

import httpx
from PIL import Image

if TYPE_CHECKING:
    from penny.config import ComfyUIConfig

MAX_IMAGE_BYTES = 32 * 1024 * 1024
_INTERACTIVE_JOB_LIMIT = 128
_RECENT_HISTORY_LIMIT = 64


class ComfyUIError(Exception):
    """An actionable error safe to show in tool results and preflight."""


class ComfyUIJobError(ComfyUIError):
    """The server rejected the workflow or reported a terminal job failure."""


class ComfyUIRequestError(ComfyUIError):
    """A request did not complete, so its server-side outcome may be unknown."""


@dataclass(frozen=True)
class _InteractiveJob:
    id: str
    acknowledged: bool


@dataclass(frozen=True)
class _Workflow:
    graph: dict[str, Any]
    image_nodes: tuple[str, ...]
    text_node: str
    seed_node: str
    output_node: str

    @classmethod
    def load(cls, filename: str, *, image_count: int) -> _Workflow:
        if image_count == 1:
            image_nodes, text_node, seed_node, output_node = ("76",), "75:74", "75:73", "9"
        else:
            image_nodes, text_node, seed_node, output_node = ("76", "81"), "92:109", "92:106", "94"
        bindings = [
            *((node, "LoadImage", "image") for node in image_nodes),
            (text_node, "CLIPTextEncode", "text"),
            (seed_node, "RandomNoise", "noise_seed"),
            (output_node, "SaveImage", "images"),
        ]
        try:
            graph = json.loads(Path(filename).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ComfyUIError(f"Could not read the {image_count}-image workflow JSON.") from exc
        for node_id, node_class, input_name in bindings:
            node = graph.get(node_id) if isinstance(graph, dict) else None
            if (
                not isinstance(node, dict)
                or node.get("class_type") != node_class
                or not isinstance(node.get("inputs"), dict)
                or input_name not in node["inputs"]
            ):
                raise ComfyUIError(
                    f"The {image_count}-image workflow needs node {node_id} "
                    f"({node_class}) with input {input_name}. Use the FLUX.2 Klein API export."
                )
        return cls(graph, image_nodes, text_node, seed_node, output_node)

    def prompt(self, description: str, images: list[str]) -> dict[str, Any]:
        graph = deepcopy(self.graph)
        for node, image in zip(self.image_nodes, images, strict=True):
            graph[node]["inputs"]["image"] = image
        graph[self.text_node]["inputs"]["text"] = description
        graph[self.seed_node]["inputs"]["noise_seed"] = randbits(53)
        return graph


class ComfyUIClient:
    """Upload ordered references and retrieve the output of a single queued job."""

    def __init__(self, settings: ComfyUIConfig, *, timeout: float):
        try:
            url = httpx.URL(settings.url or "")
        except httpx.InvalidURL:
            raise ComfyUIError("COMFYUI_URL must be an HTTPS server URL.") from None
        if url.scheme != "https" or not url.host or url.userinfo or url.query or url.fragment:
            raise ComfyUIError("COMFYUI_URL must be an HTTPS server URL without credentials.")
        if not settings.username or not settings.password:
            raise ComfyUIError("Set COMFYUI_USERNAME and COMFYUI_PASSWORD.")
        self._url = url
        self._auth = httpx.BasicAuth(settings.username, settings.password)
        self._timeout = timeout
        self._interactive_jobs: OrderedDict[tuple[str, ...], _InteractiveJob] = OrderedDict()
        self._interactive_locks: WeakValueDictionary[tuple[str, ...], asyncio.Lock] = (
            WeakValueDictionary()
        )
        self._workflows = {
            count: _Workflow.load(path, image_count=count)
            for count, path in ((1, settings.one_image_workflow), (2, settings.two_image_workflow))
            if path
        }
        if not self._workflows:
            raise ComfyUIError("Set COMFYUI_WORKFLOW_ONE_IMAGE or COMFYUI_WORKFLOW_TWO_IMAGE.")

    @property
    def image_counts(self) -> tuple[int, ...]:
        return tuple(self._workflows)

    def _session(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self._url, auth=self._auth, timeout=15)

    async def health(self) -> None:
        """Check the authenticated API without queueing work."""
        try:
            async with self._session() as client:
                _json_object(await client.get("system_stats"))
        except httpx.RequestError:
            raise ComfyUIError(
                "Could not connect to ComfyUI. Check its URL and availability."
            ) from None

    async def edit(self, description: str, images: list[tuple[bytes, str]]) -> bytes:
        workflow = self._workflow(len(images))
        key = (description, *(f"{mime}:{sha256(data).hexdigest()}" for data, mime in images))
        lock = self._interactive_locks.setdefault(key, asyncio.Lock())
        async with lock, self._edit_session() as client:
            try:
                job = self._interactive_jobs.get(key)
                prompt_id = None
                if job is not None:
                    if job.acknowledged:
                        prompt_id = await self._find_job(client, job.id)
                    else:
                        prompt_id = await self._recover_submission(client, job.id)
                if prompt_id is None:
                    graph = await self._prepare_prompt(client, workflow, description, images)
                    reserved_id = str(uuid4())
                    self._remember_interactive_job(key, _InteractiveJob(reserved_id, False))
                    prompt_id = await self._submit(client, graph, prompt_id=reserved_id)
                self._remember_interactive_job(key, _InteractiveJob(prompt_id, True))
                image = await self._fetch_image(client, prompt_id, workflow.output_node)
            except ComfyUIJobError:
                self._interactive_jobs.pop(key, None)
                raise
            self._interactive_jobs.pop(key, None)
            return image

    def _remember_interactive_job(self, key: tuple[str, ...], job: _InteractiveJob) -> None:
        self._interactive_jobs[key] = job
        self._interactive_jobs.move_to_end(key)
        if len(self._interactive_jobs) > _INTERACTIVE_JOB_LIMIT:
            self._interactive_jobs.popitem(last=False)

    async def submit_edit(
        self,
        description: str,
        images: list[tuple[bytes, str]],
        *,
        reserve_prompt: Callable[[], str | None],
        acknowledge_prompt: Callable[[str, str], None] | None = None,
    ) -> str | None:
        """Prepare the inputs, then reserve a durable job ID immediately before the POST."""
        workflow = self._workflow(len(images))
        async with self._edit_session() as client:
            graph = await self._prepare_prompt(client, workflow, description, images)
            prompt_id = reserve_prompt()
            if prompt_id is None:
                return None
            submitted_id = await self._submit(client, graph, prompt_id=prompt_id)
            if acknowledge_prompt is not None:
                acknowledge_prompt(prompt_id, submitted_id)
            return submitted_id

    async def recover_submission(self, reserved_id: str) -> str | None:
        """Find the actual job ID after a submission lost its acknowledgement."""
        async with self._edit_session() as client:
            return await self._recover_submission(client, reserved_id)

    async def _recover_submission(self, client: httpx.AsyncClient, reserved_id: str) -> str | None:
        prompt_id = await self._find_job(client, reserved_id, match_client_id=True)
        if prompt_id is not None:
            return prompt_id
        # The request can still be validating when the client loses its response.
        await asyncio.sleep(1)
        return await self._find_job(client, reserved_id, match_client_id=True)

    @classmethod
    async def _find_job(
        cls, client: httpx.AsyncClient, prompt_id: str, *, match_client_id: bool = False
    ) -> str | None:
        # Check the live queue first so a job finishing between reads is in history.
        queue = _json_object(await client.get("queue"))
        for lane in ("queue_running", "queue_pending"):
            entries = queue.get(lane)
            if not isinstance(entries, list):
                raise ComfyUIError("ComfyUI returned an unexpected queue response.")
            for entry in entries:
                if not isinstance(entry, list) or len(entry) < 2:
                    raise ComfyUIError("ComfyUI returned an unexpected queue entry.")
                if entry[1] == prompt_id:
                    return prompt_id
                if match_client_id and cls._matches_client_id(entry, prompt_id):
                    return _prompt_uuid(entry[1])
        history = _json_object(await client.get(f"history/{prompt_id}"))
        if prompt_id in history:
            return prompt_id
        if match_client_id:
            # A server that minted a different ID still records the client ID.
            recent = _json_object(
                await client.get("history", params={"max_items": _RECENT_HISTORY_LIMIT})
            )
            for job_id, entry in recent.items():
                if isinstance(entry, dict) and cls._matches_client_id(
                    entry.get("prompt"), prompt_id
                ):
                    return _prompt_uuid(job_id)
        return None

    @staticmethod
    def _matches_client_id(entry: Any, client_id: str) -> bool:
        return (
            isinstance(entry, list)
            and len(entry) > 3
            and isinstance(entry[3], dict)
            and entry[3].get("client_id") == client_id
        )

    async def fetch_edit(self, prompt_id: str, *, image_count: int) -> bytes:
        """Wait for an existing job and return its saved PNG."""
        workflow = self._workflow(image_count)
        async with self._edit_session() as client:
            return await self._fetch_image(client, prompt_id, workflow.output_node)

    def _workflow(self, image_count: int) -> _Workflow:
        workflow = self._workflows.get(image_count)
        if workflow is None:
            raise ComfyUIError(f"Configure a workflow for {image_count} input images.")
        return workflow

    @asynccontextmanager
    async def _edit_session(self) -> AsyncIterator[httpx.AsyncClient]:
        try:
            async with asyncio.timeout(self._timeout), self._session() as client:
                yield client
        except TimeoutError, httpx.TimeoutException:
            raise ComfyUIRequestError(
                "Timed out waiting for ComfyUI. Check its queue or job history."
            ) from None
        except httpx.RequestError:
            raise ComfyUIRequestError(
                "Could not complete the request to ComfyUI. Check the service."
            ) from None

    async def _prepare_prompt(
        self,
        client: httpx.AsyncClient,
        workflow: _Workflow,
        description: str,
        images: list[tuple[bytes, str]],
    ) -> dict[str, Any]:
        names = [await self._upload(client, data, mime) for data, mime in images]
        return workflow.prompt(description, names)

    @staticmethod
    async def _submit(client: httpx.AsyncClient, graph: dict[str, Any], *, prompt_id: str) -> str:
        request = {"prompt": graph, "prompt_id": prompt_id, "client_id": prompt_id}
        response = await client.post("prompt", json=request)
        if response.status_code == 400:
            raise ComfyUIJobError(
                "ComfyUI rejected the workflow. Check its nodes and models on the server."
            )
        try:
            submitted_id = _json_object(response).get("prompt_id")
        except ComfyUIError as exc:
            if not response.is_success:
                raise
            raise ComfyUIRequestError(str(exc)) from None
        return _prompt_uuid(submitted_id)

    async def _fetch_image(
        self, client: httpx.AsyncClient, prompt_id: str, output_node: str
    ) -> bytes:
        output = await self._wait_for_output(client, prompt_id, output_node)
        image = await self._download(client, output)
        await asyncio.to_thread(_verify_png, image)
        return image

    @staticmethod
    async def _upload(client: httpx.AsyncClient, data: bytes, mime: str) -> str:
        extension = mime.removeprefix("image/")
        uploaded = _json_object(
            await client.post(
                "upload/image",
                data={"type": "input", "overwrite": "false"},
                files={"image": (f"penny-{uuid4().hex}.{extension}", data, mime)},
            )
        )
        filename = uploaded.get("name")
        subfolder = uploaded.get("subfolder", "")
        if not isinstance(filename, str) or not filename or not isinstance(subfolder, str):
            raise ComfyUIError("ComfyUI did not return an uploaded image filename.")
        return f"{subfolder}/{filename}" if subfolder else filename

    @staticmethod
    async def _wait_for_output(
        client: httpx.AsyncClient, prompt_id: str, output_node: str
    ) -> dict[str, str]:
        while True:
            history = _json_object(await client.get(f"history/{prompt_id}"))
            if prompt_id in history:
                break
            await asyncio.sleep(1)

        try:
            entry = history[prompt_id]
            status = entry["status"]
            if status["status_str"] != "success" or status["completed"] is not True:
                raise ComfyUIJobError(
                    "ComfyUI failed while running the workflow. Check the server job history."
                )
            image = entry["outputs"][output_node]["images"][0]
            filename = image["filename"]
            subfolder = image.get("subfolder", "")
            if (
                not isinstance(filename, str)
                or not filename
                or not isinstance(subfolder, str)
                or image["type"] != "output"
            ):
                raise ValueError("invalid output image reference")
        except AttributeError, IndexError, KeyError, TypeError, ValueError:
            raise ComfyUIJobError("ComfyUI returned no saved image for this workflow.") from None
        return {"filename": filename, "subfolder": subfolder, "type": "output"}

    @staticmethod
    async def _download(client: httpx.AsyncClient, output: dict[str, str]) -> bytes:
        data = bytearray()
        async with client.stream("GET", "view", params=output) as response:
            if response.status_code == 404:
                raise ComfyUIJobError("ComfyUI no longer has the saved image.")
            _check_status(response)
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                    raise ComfyUIJobError("The ComfyUI output exceeds 32 MiB.")
                data.extend(chunk)
        return bytes(data)


def _check_status(response: httpx.Response) -> None:
    if response.status_code in (401, 403):
        raise ComfyUIError("ComfyUI denied access. Check the configured credentials.")
    if response.is_server_error:
        raise ComfyUIRequestError(
            f"ComfyUI returned HTTP {response.status_code}. Check the service."
        )
    if not response.is_success:
        raise ComfyUIError(f"ComfyUI returned HTTP {response.status_code}. Check the service.")


def _prompt_uuid(prompt_id: Any) -> str:
    if not isinstance(prompt_id, str):
        raise ComfyUIRequestError("ComfyUI did not return a job ID.")
    try:
        return str(UUID(prompt_id))
    except ValueError:
        raise ComfyUIRequestError("ComfyUI returned an invalid job ID.") from None


def _verify_png(data: bytes) -> None:
    try:
        with Image.open(BytesIO(data), formats=["PNG"]) as image:
            image.verify()
    except Image.DecompressionBombError, OSError, SyntaxError, ValueError:
        raise ComfyUIJobError("ComfyUI did not return a valid PNG.") from None


def _json_object(response: httpx.Response) -> dict[str, Any]:
    _check_status(response)
    try:
        value = response.json()
    except ValueError:
        raise ComfyUIError("ComfyUI returned invalid JSON.") from None
    if not isinstance(value, dict):
        raise ComfyUIError("ComfyUI returned an unexpected response.")
    return value
