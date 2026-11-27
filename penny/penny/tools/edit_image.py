"""Edit image files in Penny's configured server workspace."""

from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from PIL import Image
from pydantic import Field

from penny.llm.comfyui_client import MAX_IMAGE_BYTES, ComfyUIClient, ComfyUIError
from penny.tools.base import Tool
from penny.tools.models import NonBlankText, ToolArgs, ToolResult

if TYPE_CHECKING:
    from penny.config import ComfyUIConfig
    from penny.llm.call_link import CallLink

_IMAGE_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


class EditImageArgs(ToolArgs):
    description: NonBlankText
    input_images: list[str] = Field(min_length=1, max_length=2)


class EditImageTool(Tool):
    name = "edit_image"
    deduplicate_successful_calls = True
    description = (
        "Generate an edited image with ComfyUI from one or two image files already in "
        "Penny's server image directory. Use the relative filenames supplied by the user. "
        "The first image is the main input and the second is an optional reference. "
        "Returns the relative path of the generated PNG on the server."
    )
    parameters = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "The requested image edit."},
            "input_images": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 2,
                "description": "Ordered paths relative to the server image directory.",
            },
        },
        "required": ["description", "input_images"],
    }
    args_model = EditImageArgs

    def __init__(self, settings: ComfyUIConfig, *, timeout: float) -> None:
        if not settings.image_dir:
            raise ComfyUIError("Set COMFYUI_IMAGE_DIR to a dedicated server image directory.")
        directory = Path(settings.image_dir)
        try:
            self._image_dir = directory.resolve(strict=True)
        except OSError, ValueError:
            raise ComfyUIError("COMFYUI_IMAGE_DIR must be an existing directory.") from None
        if not self._image_dir.is_dir():
            raise ComfyUIError("COMFYUI_IMAGE_DIR must be an existing directory.")
        self.client = ComfyUIClient(settings, timeout=timeout)

    async def execute(self, call_link: CallLink, **kwargs: Any) -> ToolResult:
        args = EditImageArgs(**kwargs)
        try:
            if len(args.input_images) not in self.client.image_counts:
                supported = " or ".join(str(count) for count in self.client.image_counts)
                raise ComfyUIError(f"The configured workflows take {supported} input image(s).")
            images = await asyncio.to_thread(self._load_inputs, args.input_images)
            output = await self.client.edit(args.description, images)
            output_path = self._save_output(output)
        except ComfyUIError as exc:
            return ToolResult(message=str(exc), success=False)
        return ToolResult(message=f"Saved the generated image to `{output_path}`.", mutated=True)

    def _load_inputs(self, filenames: list[str]) -> list[tuple[bytes, str]]:
        images = []
        for index, filename in enumerate(filenames, start=1):
            relative = Path(filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise ComfyUIError(f"Input {index} must use a path in the server image directory.")
            try:
                path = (self._image_dir / relative).resolve(strict=True)
                if not path.is_relative_to(self._image_dir) or not path.is_file():
                    raise ComfyUIError(
                        f"Input {index} must name a file inside the server image directory."
                    )
                with path.open("rb") as file:
                    data = file.read(MAX_IMAGE_BYTES + 1)
            except OSError, ValueError:
                raise ComfyUIError(
                    f"Could not read input {index} in the image directory."
                ) from None
            if len(data) > MAX_IMAGE_BYTES:
                raise ComfyUIError(f"Input {index} exceeds 32 MiB.")
            mime = _image_mime(data)
            if mime is None:
                raise ComfyUIError(f"Input {index} must be a valid PNG, JPEG, or WebP file.")
            images.append((data, mime))
        return images

    def _save_output(self, image: bytes) -> str:
        relative = Path("outputs") / f"{uuid4().hex}.png"
        directory = self._image_dir / relative.parent
        try:
            directory.mkdir(exist_ok=True)
            if directory.is_symlink():
                raise OSError("The output directory is a symlink")
            with NamedTemporaryFile(dir=directory, delete=False) as temporary:
                path = Path(temporary.name)
                try:
                    temporary.write(image)
                    temporary.close()
                    path.replace(self._image_dir / relative)
                finally:
                    path.unlink(missing_ok=True)
        except OSError:
            raise ComfyUIError(
                "Could not write the generated PNG to the server image directory."
            ) from None
        return relative.as_posix()

    @classmethod
    def to_action_str(cls, arguments: dict) -> str:
        return "Editing an image"


def _image_mime(data: bytes) -> str | None:
    try:
        with Image.open(BytesIO(data), formats=list(_IMAGE_TYPES)) as image:
            image.verify()
            return _IMAGE_TYPES.get(image.format or "")
    except Image.DecompressionBombError, OSError, SyntaxError, ValueError:
        return None
