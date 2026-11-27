"""Exercise the server tool through the normal tool executor and ComfyUI HTTP contract."""

import asyncio
import base64
import json
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from PIL import Image

from penny.agents.models import NO_INBOX
from penny.config import ComfyUIConfig
from penny.llm.call_link import CallLink
from penny.llm.comfyui_client import ComfyUIClient
from penny.penny import Penny
from penny.tests.conftest import TEST_SENDER
from penny.tools.base import ToolExecutor, ToolRegistry
from penny.tools.edit_image import EditImageTool
from penny.tools.models import ToolCall


def _write_workflow(
    path: Path, image_nodes: tuple[str, ...], text_node: str, seed_node: str, output_node: str
) -> str:
    graph: dict[str, object] = {
        node: {"class_type": "LoadImage", "inputs": {"image": "example.png"}}
        for node in image_nodes
    }
    graph[text_node] = {"class_type": "CLIPTextEncode", "inputs": {"text": "example"}}
    graph[seed_node] = {"class_type": "RandomNoise", "inputs": {"noise_seed": 0}}
    graph[output_node] = {"class_type": "SaveImage", "inputs": {"images": ["decode", 0]}}
    path.write_text(json.dumps(graph), encoding="utf-8")
    return str(path)


def _png(color: str) -> bytes:
    data = BytesIO()
    Image.new("RGB", (2, 2), color).save(data, "PNG")
    return data.getvalue()


@pytest.fixture
def comfyui_settings(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    return ComfyUIConfig(
        enabled=True,
        url="https://comfy.test/comfy",
        username="penny",
        password="test-password",
        image_dir=str(images),
        one_image_workflow=_write_workflow(tmp_path / "one.json", ("76",), "75:74", "75:73", "9"),
        two_image_workflow=_write_workflow(
            tmp_path / "two.json", ("76", "81"), "92:109", "92:106", "94"
        ),
    )


def _mock_server(monkeypatch, handler):
    def session(client):
        return httpx.AsyncClient(
            base_url=client._url,
            auth=client._auth,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(ComfyUIClient, "_session", session)


async def _execute(tool, filenames):
    registry = ToolRegistry()
    registry.register(tool)
    return await ToolExecutor(registry, forced_plan=False).execute(
        ToolCall(
            tool="edit_image", arguments={"description": "Make it green", "input_images": filenames}
        ),
        CallLink.top("edit-image-test"),
    )


@pytest.mark.parametrize(
    ("image_nodes", "text_node", "seed_node", "output_node"),
    [(("76",), "75:74", "75:73", "9"), (("76", "81"), "92:109", "92:106", "94")],
)
async def test_edit_image_dispatches_and_saves_the_result(
    comfyui_settings, make_config, monkeypatch, image_nodes, text_node, seed_node, output_node
):
    directory = Path(comfyui_settings.image_dir)
    inputs = [_png("red"), _png("blue")][: len(image_nodes)]
    filenames = [f"input-{index}.png" for index in range(len(inputs))]
    for name, data in zip(filenames, inputs, strict=True):
        (directory / name).write_bytes(data)
    generated = _png("green")
    prompt_id = str(uuid4())
    uploaded = []
    history_polls = 0
    authorization = "Basic " + base64.b64encode(b"penny:test-password").decode()

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal history_polls
        assert request.headers["authorization"] == authorization
        path = request.url.path
        if path == "/comfy/system_stats":
            return httpx.Response(200, json={})
        if path == "/comfy/upload/image":
            assert request.method == "POST"
            index = len(uploaded)
            assert inputs[index] in request.content
            name = f"penny/{index}.png"
            uploaded.append(name)
            return httpx.Response(200, json={"name": f"{index}.png", "subfolder": "penny"})
        if path == "/comfy/prompt":
            assert request.method == "POST"
            graph = json.loads(request.content)["prompt"]
            assert [graph[node]["inputs"]["image"] for node in image_nodes] == uploaded
            assert graph[text_node]["inputs"]["text"] == "Make it green"
            assert isinstance(graph[seed_node]["inputs"]["noise_seed"], int)
            return httpx.Response(200, json={"prompt_id": prompt_id})
        if path == f"/comfy/history/{prompt_id}":
            history_polls += 1
            if history_polls == 1:
                return httpx.Response(200, json={})
            return httpx.Response(
                200,
                json={
                    prompt_id: {
                        "status": {"status_str": "success", "completed": True},
                        "outputs": {
                            output_node: {
                                "images": [
                                    {
                                        "filename": "edited.png",
                                        "subfolder": "edits",
                                        "type": "output",
                                    }
                                ]
                            }
                        },
                    }
                },
            )
        assert path == "/comfy/view"
        assert dict(request.url.params) == {
            "filename": "edited.png",
            "subfolder": "edits",
            "type": "output",
        }
        return httpx.Response(200, content=generated)

    _mock_server(monkeypatch, respond)
    penny = Penny(make_config(comfyui=comfyui_settings))
    assert penny.image_edit_tool is not None
    instructions = await penny.chat_agent._build_system_prompt(None)
    assert "ComfyUI workflow" not in instructions
    assert penny.config.runtime.is_configured("SCHEDULED_REPORT_IMAGE_EDITING")
    assert penny.config.runtime.SCHEDULED_REPORT_IMAGE_EDITING == 0
    await penny.image_edit_tool.client.health()
    tool = next(tool for tool in penny.chat_agent.get_tools() if tool.name == "edit_image")
    result = await _execute(tool, filenames)
    assert result.success and result.mutated
    assert result.media_id is None
    (output,) = (directory / "outputs").iterdir()
    assert output.read_bytes() == generated
    assert result.message == f"Saved the generated image to `outputs/{output.name}`."


async def test_chat_reuses_a_successful_edit_when_the_model_repeats_the_call(
    comfyui_settings, make_config, mock_llm, test_user_info, monkeypatch
):
    directory = Path(comfyui_settings.image_dir)
    (directory / "input.png").write_bytes(_png("red"))
    edit = AsyncMock(return_value=_png("green"))
    monkeypatch.setattr(ComfyUIClient, "edit", edit)
    penny = Penny(make_config(comfyui=comfyui_settings))

    def respond(request, count):
        if count <= 3:
            description = "Make it green" if count < 3 else "Make it dark green"
            return mock_llm._make_tool_call_response(
                request, "edit_image", {"description": description, "input_images": ["input.png"]}
            )
        return mock_llm._make_text_response(request, "I saved the two green versions.")

    mock_llm.set_response_handler(respond)
    response = await penny.chat_agent.handle(
        "Edit input.png to make a green image and a dark green version.",
        TEST_SENDER,
        call_link=CallLink.top("repeat-image-edit"),
        inbox=NO_INBOX,
    )

    assert response.answer == "I saved the two green versions."
    assert [call.args[0] for call in edit.await_args_list] == [
        "Make it green",
        "Make it dark green",
    ]
    assert len(list((directory / "outputs").iterdir())) == 2


async def test_scheduled_submission_uses_the_reserved_prompt_id(comfyui_settings, monkeypatch):
    prompt_id = str(uuid4())
    submitted = []

    def respond(request):
        if request.url.path.endswith("/upload/image"):
            return httpx.Response(200, json={"name": "input.png"})
        assert request.url.path.endswith("/prompt")
        submitted.append(json.loads(request.content)["prompt_id"])
        return httpx.Response(200, json={"prompt_id": prompt_id})

    _mock_server(monkeypatch, respond)
    client = ComfyUIClient(comfyui_settings, timeout=5)
    result = await client.submit_edit(
        "Make it green", [(_png("red"), "image/png")], reserve_prompt=lambda: prompt_id
    )
    assert result == prompt_id
    assert submitted == [prompt_id]


@pytest.mark.parametrize(
    "interruption", ["timeout", "cancelled", "lost_acknowledgement", "reassigned_id"]
)
async def test_interactive_retry_uses_the_queued_comfyui_job(
    comfyui_settings, monkeypatch, interruption
):
    directory = Path(comfyui_settings.image_dir)
    (directory / "input.png").write_bytes(_png("red"))
    queued_ids = []
    server_id = str(uuid4()) if interruption == "reassigned_id" else None
    hit_wait = asyncio.Event()
    retrying = False
    uploaded = 0

    async def respond(request):
        nonlocal uploaded, server_id
        path = request.url.path
        if path.endswith("/upload/image"):
            uploaded += 1
            return httpx.Response(200, json={"name": "input.png"})
        if path.endswith("/prompt"):
            submission = json.loads(request.content)
            prompt_id = submission["prompt_id"]
            assert submission["client_id"] == prompt_id
            queued_ids.append(prompt_id)
            server_id = server_id or prompt_id
            if interruption in {"lost_acknowledgement", "reassigned_id"}:
                raise httpx.ReadError("connection dropped after enqueue", request=request)
            return httpx.Response(200, json={"prompt_id": server_id})
        if path.endswith("/queue"):
            running = [] if interruption == "reassigned_id" else [[0, server_id]]
            return httpx.Response(200, json={"queue_running": running, "queue_pending": []})
        if interruption == "reassigned_id" and path.endswith(f"/history/{queued_ids[0]}"):
            return httpx.Response(200, json={})
        if path.endswith("/history"):
            assert "max_items" in request.url.params
            return httpx.Response(
                200, json={server_id: {"prompt": [0, server_id, {}, {"client_id": queued_ids[0]}]}}
            )
        if path.endswith(f"/history/{server_id}"):
            if not retrying:
                hit_wait.set()
                await asyncio.Future()
            return httpx.Response(
                200,
                json={
                    server_id: {
                        "status": {"status_str": "success", "completed": True},
                        "outputs": {
                            "9": {"images": [{"filename": "edited.png", "type": "output"}]}
                        },
                    }
                },
            )
        assert path.endswith("/view")
        return httpx.Response(200, content=_png("green"))

    _mock_server(monkeypatch, respond)
    tool = EditImageTool(comfyui_settings, timeout=0.2 if interruption == "timeout" else 5)
    operation = asyncio.create_task(_execute(tool, ["input.png"]))
    if interruption == "cancelled":
        try:
            await asyncio.wait_for(hit_wait.wait(), timeout=3)
        finally:
            operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
    else:
        assert not (await operation).success
    retrying = True
    result = await _execute(tool, ["input.png"])
    assert result.success
    assert len(queued_ids) == uploaded == 1
    (output,) = (directory / "outputs").iterdir()
    assert output.read_bytes() == _png("green")


@pytest.mark.parametrize("mode", ["disabled", "incomplete", "two-image-only"])
async def test_scheduled_image_setting_requires_a_one_image_workflow(
    make_config, comfyui_settings, mode
):
    settings = (
        replace(comfyui_settings, one_image_workflow=None)
        if mode == "two-image-only"
        else ComfyUIConfig(enabled=mode == "incomplete")
    )
    penny = Penny(make_config(comfyui=settings))
    tools = {tool.name for tool in penny.chat_agent.get_tools()}
    assert ("edit_image" in tools) is (mode == "two-image-only")
    assert bool(penny.comfyui_config_error) is (mode == "incomplete")
    assert not penny.config.runtime.is_configured("SCHEDULED_REPORT_IMAGE_EDITING")


async def test_edit_image_accepts_a_single_configured_workflow(comfyui_settings):
    settings = replace(comfyui_settings, two_image_workflow=None)
    tool = EditImageTool(settings, timeout=5)
    assert tool.client.image_counts == (1,)
    result = await _execute(tool, ["first.png", "second.png"])
    assert not result.success
    assert "take 1 input" in result.message


async def test_edit_image_rejects_paths_outside_its_workspace(
    comfyui_settings, tmp_path, monkeypatch
):
    outside = tmp_path / "outside.png"
    outside.write_bytes(_png("red"))
    (Path(comfyui_settings.image_dir) / "link.png").symlink_to(outside)

    def unexpected_request(request):
        pytest.fail(f"Unexpected request to {request.url.path}")

    _mock_server(monkeypatch, unexpected_request)
    tool = EditImageTool(comfyui_settings, timeout=5)
    for filename in ("../outside.png", str(outside), "link.png"):
        result = await _execute(tool, [filename])
        assert not result.success
        assert "image directory" in result.message


async def test_edit_image_reports_failed_comfyui_jobs(comfyui_settings, monkeypatch):
    directory = Path(comfyui_settings.image_dir)
    (directory / "input.png").write_bytes(_png("red"))
    prompt_id = str(uuid4())

    def respond(request):
        if request.url.path.endswith("/upload/image"):
            return httpx.Response(200, json={"name": "input.png"})
        if request.url.path.endswith("/prompt"):
            return httpx.Response(200, json={"prompt_id": prompt_id})
        assert request.url.path.endswith(f"/history/{prompt_id}")
        return httpx.Response(
            200, json={prompt_id: {"status": {"status_str": "error", "completed": False}}}
        )

    _mock_server(monkeypatch, respond)
    result = await _execute(EditImageTool(comfyui_settings, timeout=5), ["input.png"])
    assert not result.success
    assert "failed while running the workflow" in result.message
    assert not (directory / "outputs").exists()
