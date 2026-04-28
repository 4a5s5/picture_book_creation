"""Standalone end-to-end text and image workflow."""

from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
import os
import re
import signal
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import requests
import yaml
from PIL import Image, ImageDraw

try:
    from google import genai
    from google.genai import types as genai_types
except Exception:  # pragma: no cover
    genai = None
    genai_types = None


SKILL_ROOT = Path(__file__).resolve().parents[1]
REFERENCES_DIR = SKILL_ROOT / "references"
DEFAULT_OUTPUT_ROOT = SKILL_ROOT / "tasks"
DEFAULT_CONFIG_PATH = SKILL_ROOT / "workflow_config.yaml"
STATE_FILENAME = "task_state.json"
LOCK_FILENAME = ".task.lock"
CANCEL_REQUESTED = threading.Event()
FORCE_EXIT_AFTER_MAIN = True


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _handle_shutdown(signum: int, _frame: Any) -> None:
    CANCEL_REQUESTED.set()
    raise KeyboardInterrupt(f"Interrupted by signal {signum}.")


def install_shutdown_handlers() -> None:
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        with contextlib.suppress(Exception):
            signal.signal(signum, _handle_shutdown)


def start_parent_watchdog() -> None:
    parent_pid = os.getppid()
    if parent_pid <= 0:
        return

    def watch_parent() -> None:
        while not CANCEL_REQUESTED.is_set():
            time.sleep(2)
            if not process_exists(parent_pid):
                os._exit(130)

    thread = threading.Thread(target=watch_parent, daemon=True)
    thread.start()


def parent_watchdog_enabled(args: argparse.Namespace) -> bool:
    if getattr(args, "watch_parent", False):
        return True
    value = os.environ.get("PICTURE_BOOK_WATCH_PARENT", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def force_exit_enabled(args: argparse.Namespace) -> bool:
    if getattr(args, "no_force_exit", False):
        return False
    value = os.environ.get("PICTURE_BOOK_NO_FORCE_EXIT", "").strip().lower()
    return value not in {"1", "true", "yes", "on"}


def exit_process(code: int, force: bool) -> None:
    sys.stdout.flush()
    sys.stderr.flush()
    if force:
        os._exit(code)
    raise SystemExit(code)


def json_print(data: Any, compact: bool = False) -> None:
    if compact:
        print(json.dumps(data, ensure_ascii=False), flush=True)
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2), flush=True)


def read_json_source(value: str) -> Any:
    if value == "-":
        return json.load(__import__("sys").stdin)
    path = Path(value)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8-sig"))
    return json.loads(value)


def read_text_or_value(value: Optional[str]) -> str:
    if not value:
        return ""
    path = Path(value)
    if path.exists():
        return path.read_text(encoding="utf-8-sig")
    return value


def ensure_page(page: dict[str, Any]) -> dict[str, Any]:
    required = {"index", "type", "content"}
    missing = required - set(page)
    if missing:
        raise ValueError(f"Page is missing fields: {sorted(missing)}")
    return page


def ensure_pages(pages: Any) -> list[dict[str, Any]]:
    if not isinstance(pages, list) or not pages:
        raise ValueError("Payload field 'pages' must be a non-empty list.")
    return [ensure_page(page) for page in pages]


def normalize_requested_style(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    return text or None


def normalize_page_count(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise ValueError("page_count must be an integer.")
    if count < 4 or count > 32:
        raise ValueError("page_count must be between 4 and 32.")
    return count


def normalize_positive_int(value: Any, default: int, field_name: str) -> int:
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be an integer.")
    if number < 1:
        raise ValueError(f"{field_name} must be greater than 0.")
    return number


def load_yaml_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}


def resolve_config_path(config_value: Optional[str]) -> Path:
    if config_value:
        path = Path(config_value)
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        if path.exists():
            return path
        raise FileNotFoundError(f"Config file not found: {path}")
    if DEFAULT_CONFIG_PATH.exists():
        return DEFAULT_CONFIG_PATH
    raise FileNotFoundError(
        "No existing workflow_config.yaml found. Do not run init-config during generation. "
        f"Create or provide the configured file at the fixed default path: {DEFAULT_CONFIG_PATH}"
    )


def is_demo_provider(name: Optional[str], config: dict[str, Any]) -> bool:
    return str(name or "").startswith("demo_") or config.get("type") in {"mock", "mock_text"}


def assert_generation_config_ready(raw: dict[str, Any], config_path: Path) -> None:
    if bool(raw.get("allow_demo_providers", False)):
        return
    text_generation = raw.get("text_generation", {})
    image_generation = raw.get("image_generation", {})
    text_name = text_generation.get("active_provider")
    image_name = image_generation.get("active_provider")
    text_cfg = text_generation.get("providers", {}).get(text_name, {})
    image_cfg = image_generation.get("providers", {}).get(image_name, {})
    demo_fields = []
    if is_demo_provider(text_name, text_cfg):
        demo_fields.append("text_generation.active_provider")
    if is_demo_provider(image_name, image_cfg):
        demo_fields.append("image_generation.active_provider")
    if not demo_fields:
        return
    raise RuntimeError(
        "Config still uses demo providers and cannot run generation. "
        f"Config: {config_path}. "
        f"Change {', '.join(demo_fields)} to real providers such as openai_text/google_text and openai_image/google_image, "
        "then fill their api_key/base_url/model fields. "
        "Set allow_demo_providers: true only for local mock tests."
    )


def mask_secret(secret: Optional[str]) -> Optional[str]:
    if not secret:
        return secret
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}...{secret[-4:]}"


def compress_image(image_data: bytes, max_size_kb: int = 200, max_dimension: int = 2048) -> bytes:
    limit = max_size_kb * 1024
    if len(image_data) <= limit:
        return image_data

    image = Image.open(io.BytesIO(image_data))
    if image.mode in ("RGBA", "LA", "P"):
        background = Image.new("RGB", image.size, (255, 255, 255))
        if image.mode == "P":
            image = image.convert("RGBA")
        background.paste(image, mask=image.split()[-1] if image.mode in ("RGBA", "LA") else None)
        image = background
    elif image.mode != "RGB":
        image = image.convert("RGB")

    width, height = image.size
    if width > max_dimension or height > max_dimension:
        ratio = min(max_dimension / width, max_dimension / height)
        image = image.resize((int(width * ratio), int(height * ratio)), Image.Resampling.LANCZOS)

    quality = 85
    output_bytes = image_data
    while quality >= 20:
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=quality, optimize=True)
        output_bytes = output.getvalue()
        if len(output_bytes) <= limit:
            break
        quality -= 5

    return output_bytes


def save_png_bytes(image_data: bytes, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(image_data)


def make_thumbnail(image_data: bytes) -> bytes:
    return compress_image(image_data, max_size_kb=50)


def to_data_uri(image_data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(image_data).decode("utf-8")


def decode_data_uri(data: str) -> bytes:
    if data.startswith("data:"):
        data = data.split(",", 1)[1]
    return base64.b64decode(data)


class TextGeneratorBase:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.api_key = config.get("api_key")
        self.base_url = config.get("base_url")

    def generate_text(self, prompt: str, **kwargs: Any) -> str:
        raise NotImplementedError


class ImageGeneratorBase:
    supports_reference_images = False
    supports_multiple_reference_images = False

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.api_key = config.get("api_key")
        self.base_url = config.get("base_url")

    def generate_image(self, prompt: str, **kwargs: Any) -> bytes:
        raise NotImplementedError


class MockTextGenerator(TextGeneratorBase):
    def generate_text(self, prompt: str, **kwargs: Any) -> str:
        topic = kwargs.get("topic", "未命名主题")
        mode = kwargs.get("mode", "outline")
        if mode == "content":
            payload = {
                "titles": [
                    f"{topic}：一本学会好好相处的绘本",
                    f"{topic}：送给孩子的温柔成长故事",
                    f"{topic}：在故事里学会表达和尊重",
                ],
                "copywriting": (
                    f"这是一个围绕“{topic}”展开的儿童绘本故事。\n"
                    f"故事通过具体生活场景，让孩子理解分享、表达情绪和尊重他人的边界。\n"
                    f"整体画面适合亲子共读，节奏温和，结尾落在成长与理解上。"
                ),
                "tags": [topic, "儿童绘本", "亲子共读", "情绪管理", "社交启蒙", "成长故事"],
            }
            return json.dumps(payload, ensure_ascii=False)

        page_count_match = re.search(r"期望页数：\s*(\d+)", prompt)
        requested_page_count = int(page_count_match.group(1)) if page_count_match else 8
        page_count = max(6, min(requested_page_count, 16))
        body_pages = max(4, page_count - 2)

        pages = [
            "[封面]\n"
            f"标题：{topic}\n"
            "副标题：温柔讲给孩子听的成长故事\n"
            "画面重点：主角鲜明、氛围温暖、具有绘本封面感"
        ]
        for idx in range(body_pages):
            ordinal = idx + 1
            pages.append(
                "[内容]\n"
                f"第{ordinal}页：故事推进场景 {ordinal}\n"
                f"画面内容：围绕“{topic}”展开一个连续的儿童绘本场景，突出角色动作、情绪变化和环境细节。"
            )
        pages.append("[总结]\n结尾：主角学会更好的相处方式，故事落在温暖和成长上")
        return "\n<page>\n".join(pages)


class MockImageGenerator(ImageGeneratorBase):
    supports_reference_images = True
    supports_multiple_reference_images = True

    def generate_image(self, prompt: str, **kwargs: Any) -> bytes:
        image = Image.new("RGB", (768, 1024), (245, 239, 230))
        draw = ImageDraw.Draw(image)
        lines = [
            "Mock Image Output",
            f"Type: {kwargs.get('page_type', 'unknown')}",
            f"Model: {kwargs.get('model', 'mock-image-model')}",
            "",
        ]
        lines.extend(prompt[:420].splitlines()[:12])
        y = 40
        for line in lines:
            draw.text((40, y), line, fill=(35, 35, 35))
            y += 42

        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()


class OpenAICompatibleTextGenerator(TextGeneratorBase):
    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        if not self.api_key or not self.base_url:
            raise ValueError("Text generation requires api_key and base_url.")
        self.base_url = self.base_url.rstrip("/")
        if self.base_url.endswith("/v1"):
            self.base_url = self.base_url[:-3]
        self.endpoint_type = config.get("endpoint_type", "/v1/chat/completions")
        if not self.endpoint_type.startswith("/"):
            self.endpoint_type = "/" + self.endpoint_type

    def generate_text(self, prompt: str, **kwargs: Any) -> str:
        url = f"{self.base_url}{self.endpoint_type}"
        images: list[bytes] = kwargs.get("images") or []
        content: Any
        if images:
            content = [{"type": "text", "text": prompt}]
            for image in images:
                content.append({"type": "image_url", "image_url": {"url": to_data_uri(compress_image(image))}})
        else:
            content = prompt

        payload = {
            "model": kwargs.get("model") or self.config.get("model"),
            "messages": [{"role": "user", "content": content}],
            "temperature": kwargs.get("temperature", self.config.get("temperature", 1.0)),
            "max_tokens": kwargs.get("max_output_tokens", self.config.get("max_output_tokens", 8000)),
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        response = requests.post(url, headers=headers, json=payload, timeout=300)
        if response.status_code != 200:
            raise RuntimeError(f"Text request failed: {response.status_code} {response.text[:500]}")
        choices = response.json().get("choices") or []
        if not choices:
            raise RuntimeError("Text API returned no choices.")
        return choices[0]["message"]["content"]


class GoogleGeminiTextGenerator(TextGeneratorBase):
    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        if genai is None or genai_types is None:
            raise RuntimeError("google-genai package is not installed.")
        if not self.api_key:
            raise ValueError("Google text generation requires api_key.")
        client_kwargs: dict[str, Any] = {"api_key": self.api_key, "vertexai": False}
        if config.get("base_url"):
            client_kwargs["http_options"] = {"base_url": config["base_url"], "api_version": "v1beta"}
        self.client = genai.Client(**client_kwargs)

    def generate_text(self, prompt: str, **kwargs: Any) -> str:
        parts: list[Any] = [genai_types.Part(text=prompt)]
        for image in kwargs.get("images") or []:
            parts.append(
                genai_types.Part(
                    inline_data=genai_types.Blob(mime_type="image/png", data=compress_image(image))
                )
            )
        contents = [genai_types.Content(role="user", parts=parts)]
        config = genai_types.GenerateContentConfig(
            temperature=kwargs.get("temperature", self.config.get("temperature", 1.0)),
            max_output_tokens=kwargs.get("max_output_tokens", self.config.get("max_output_tokens", 8000)),
        )
        chunks: list[str] = []
        for chunk in self.client.models.generate_content_stream(
            model=kwargs.get("model") or self.config.get("model"),
            contents=contents,
            config=config,
        ):
            if getattr(chunk, "text", None):
                chunks.append(chunk.text)
        return "".join(chunks).strip()


class OpenAICompatibleImageGenerator(ImageGeneratorBase):
    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        if not self.api_key or not self.base_url:
            raise ValueError("OpenAI-compatible image generation requires api_key and base_url.")
        self.base_url = self.base_url.rstrip("/")
        if self.base_url.endswith("/v1"):
            self.base_url = self.base_url[:-3]
        self.endpoint_type = config.get("endpoint_type", "/v1/images/generations")
        if not self.endpoint_type.startswith("/"):
            self.endpoint_type = "/" + self.endpoint_type

    def generate_image(self, prompt: str, **kwargs: Any) -> bytes:
        url = f"{self.base_url}{self.endpoint_type}"
        timeout_seconds = int(kwargs.get("timeout_seconds") or self.config.get("page_timeout_seconds", 120))
        payload = {
            "model": kwargs.get("model") or self.config.get("model"),
            "prompt": prompt,
            "n": 1,
            "size": kwargs.get("size") or self.config.get("default_size", "1024x1024"),
            "quality": kwargs.get("quality") or self.config.get("quality", "standard"),
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        response = requests.post(url, headers=headers, json=payload, timeout=timeout_seconds)
        if response.status_code != 200:
            raise RuntimeError(f"Image request failed: {response.status_code} {response.text[:500]}")
        data = response.json().get("data") or []
        if not data:
            raise RuntimeError("Image API returned no data.")
        item = data[0]
        if "b64_json" in item:
            return decode_data_uri(item["b64_json"])
        if "url" in item:
            img = requests.get(item["url"], timeout=timeout_seconds)
            img.raise_for_status()
            return img.content
        raise RuntimeError("Could not extract image data from image response.")


class ImageApiGenerator(ImageGeneratorBase):
    supports_reference_images = True
    supports_multiple_reference_images = True

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        if not self.api_key or not self.base_url:
            raise ValueError("Image API generation requires api_key and base_url.")
        self.base_url = self.base_url.rstrip("/")
        if self.base_url.endswith("/v1"):
            self.base_url = self.base_url[:-3]
        self.endpoint_type = config.get("endpoint_type", "/v1/images/generations")
        if not self.endpoint_type.startswith("/"):
            self.endpoint_type = "/" + self.endpoint_type

    def generate_image(self, prompt: str, **kwargs: Any) -> bytes:
        url = f"{self.base_url}{self.endpoint_type}"
        timeout_seconds = int(kwargs.get("timeout_seconds") or self.config.get("page_timeout_seconds", 120))
        payload = {
            "model": kwargs.get("model") or self.config.get("model"),
            "prompt": prompt,
            "response_format": "b64_json",
            "aspect_ratio": kwargs.get("aspect_ratio") or self.config.get("default_aspect_ratio", "3:4"),
            "image_size": self.config.get("image_size", "4K"),
        }
        references = kwargs.get("reference_images") or []
        if references:
            payload["image"] = [to_data_uri(compress_image(image)) for image in references]
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        response = requests.post(url, headers=headers, json=payload, timeout=timeout_seconds)
        if response.status_code != 200:
            raise RuntimeError(f"Image API request failed: {response.status_code} {response.text[:500]}")
        data = response.json().get("data") or []
        if not data:
            raise RuntimeError("Image API returned no data.")
        item = data[0]
        if "b64_json" in item:
            return decode_data_uri(item["b64_json"])
        if "url" in item:
            img = requests.get(item["url"], timeout=timeout_seconds)
            img.raise_for_status()
            return img.content
        raise RuntimeError("Could not extract image data from custom image response.")


class GoogleGenAIGenerator(ImageGeneratorBase):
    supports_reference_images = True

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        if genai is None or genai_types is None:
            raise RuntimeError("google-genai package is not installed.")
        if not self.api_key:
            raise ValueError("Google image generation requires api_key.")
        client_kwargs: dict[str, Any] = {"api_key": self.api_key, "vertexai": False}
        if config.get("base_url"):
            client_kwargs["http_options"] = {"base_url": config["base_url"], "api_version": "v1beta"}
        self.client = genai.Client(**client_kwargs)

    def generate_image(self, prompt: str, **kwargs: Any) -> bytes:
        parts: list[Any] = []
        if kwargs.get("reference_image"):
            parts.append(
                genai_types.Part(
                    inline_data=genai_types.Blob(
                        mime_type="image/png",
                        data=compress_image(kwargs["reference_image"]),
                    )
                )
            )
        parts.append(genai_types.Part(text=prompt))
        contents = [genai_types.Content(role="user", parts=parts)]
        config = genai_types.GenerateContentConfig(
            response_modalities=["TEXT", "IMAGE"],
            image_config=genai_types.ImageConfig(
                aspect_ratio=kwargs.get("aspect_ratio") or self.config.get("default_aspect_ratio", "3:4")
            ),
        )
        image_data = None
        for chunk in self.client.models.generate_content_stream(
            model=kwargs.get("model") or self.config.get("model"),
            contents=contents,
            config=config,
        ):
            if chunk.candidates and chunk.candidates[0].content and chunk.candidates[0].content.parts:
                for part in chunk.candidates[0].content.parts:
                    if getattr(part, "inline_data", None):
                        image_data = part.inline_data.data
                        break
        if not image_data:
            raise RuntimeError("Google image generation returned no image data.")
        return image_data


TEXT_GENERATOR_TYPES = {
    "mock_text": MockTextGenerator,
    "google_gemini": GoogleGeminiTextGenerator,
    "openai_compatible": OpenAICompatibleTextGenerator,
    "openai": OpenAICompatibleTextGenerator,
}

IMAGE_GENERATOR_TYPES = {
    "mock": MockImageGenerator,
    "google_genai": GoogleGenAIGenerator,
    "openai_compatible": OpenAICompatibleImageGenerator,
    "openai": OpenAICompatibleImageGenerator,
    "image_api": ImageApiGenerator,
}


@dataclass
class WorkflowConfig:
    text_provider_name: str
    text_provider_config: dict[str, Any]
    image_provider_name: str
    image_provider_config: dict[str, Any]
    output_root: Path
    task_lock_stale_seconds: int = 300


class TaskStore:
    def __init__(self, output_root: Path, lock_stale_seconds: int = 300):
        self.output_root = output_root
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.lock_stale_seconds = lock_stale_seconds
        self._lock = threading.Lock()

    def task_dir(self, task_id: str) -> Path:
        return self.output_root / task_id

    def state_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / STATE_FILENAME

    def lock_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / LOCK_FILENAME

    def task_exists(self, task_id: str) -> bool:
        return self.state_path(task_id).exists()

    def _read_lock(self, task_id: str) -> dict[str, Any]:
        lock_path = self.lock_path(task_id)
        if not lock_path.exists():
            return {}
        try:
            return json.loads(lock_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def load_state(self, task_id: str) -> dict[str, Any]:
        path = self.state_path(task_id)
        if not path.exists():
            raise FileNotFoundError(f"Task state not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def save_state(self, task_id: str, state: dict[str, Any]) -> None:
        task_dir = self.task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        state_path = self.state_path(task_id)
        temp_path = state_path.with_suffix(".json.tmp")
        with self._lock:
            temp_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp_path.replace(state_path)

    def save_error(self, task_id: str, error: str, stage: str, extra: Optional[dict[str, Any]] = None) -> Path:
        task_dir = self.task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        error_path = task_dir / "task_error.json"
        payload = {
            "success": False,
            "task_id": task_id,
            "stage": stage,
            "error": error,
            "created_at": int(time.time()),
        }
        if extra:
            payload.update(extra)
        error_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return error_path

    def save_run_status(self, task_id: str, stage: str, extra: Optional[dict[str, Any]] = None) -> Path:
        task_dir = self.task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        status_path = task_dir / "task_run_status.json"
        payload = {
            "success": None,
            "task_id": task_id,
            "stage": stage,
            "pid": os.getpid(),
            "updated_at": int(time.time()),
        }
        if extra:
            payload.update(extra)
        status_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return status_path

    def diagnose(self, task_id: str) -> dict[str, Any]:
        task_dir = self.task_dir(task_id)
        lock = self._read_lock(task_id)
        pid = int(lock.get("pid") or 0) if lock else 0
        created_at = int(lock.get("created_at") or 0) if lock else 0
        age_seconds = int(time.time()) - created_at if created_at else None
        error_path = task_dir / "task_error.json"
        status_path = task_dir / "task_run_status.json"
        state_path = self.state_path(task_id)
        return {
            "success": True,
            "task_id": task_id,
            "task_dir": str(task_dir),
            "exists": task_dir.exists(),
            "has_state": state_path.exists(),
            "state_path": str(state_path),
            "has_error": error_path.exists(),
            "error_path": str(error_path) if error_path.exists() else None,
            "error": json.loads(error_path.read_text(encoding="utf-8")) if error_path.exists() else None,
            "has_status": status_path.exists(),
            "status_path": str(status_path) if status_path.exists() else None,
            "status": json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else None,
            "has_lock": bool(lock),
            "lock": lock,
            "lock_pid_alive": process_exists(pid) if pid else False,
            "lock_age_seconds": age_seconds,
            "lock_stale_seconds": self.lock_stale_seconds,
            "files": sorted(file.name for file in task_dir.glob("*") if file.is_file()) if task_dir.exists() else [],
        }

    def cleanup_lock(self, task_id: str, force: bool = False) -> dict[str, Any]:
        lock_path = self.lock_path(task_id)
        existing = self._read_lock(task_id)
        if not lock_path.exists():
            return {"success": True, "task_id": task_id, "removed": False, "reason": "no_lock"}
        pid = int(existing.get("pid") or 0)
        alive = process_exists(pid) if pid else False
        if alive and not force:
            raise RuntimeError(f"Refusing to remove lock for live pid {pid}. Use --force only if you are certain it is stale.")
        lock_path.unlink()
        return {"success": True, "task_id": task_id, "removed": True, "lock": existing, "forced": force}

    def reset_task_outputs(self, task_id: str) -> None:
        task_dir = self.task_dir(task_id)
        if not task_dir.exists():
            return
        patterns = ("*.png", "thumb_*.png", STATE_FILENAME, "task_error.json", "task_run_status.json")
        for pattern in patterns:
            for path in task_dir.glob(pattern):
                if path.is_file():
                    path.unlink()

    @contextlib.contextmanager
    def task_lock(self, task_id: str):
        task_dir = self.task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.lock_path(task_id)
        payload = {
            "pid": os.getpid(),
            "created_at": int(time.time()),
        }
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                existing = self._read_lock(task_id)
                pid = int(existing.get("pid") or 0)
                created_at = int(existing.get("created_at") or 0)
                lock_age = int(time.time()) - created_at if created_at else 0
                has_dead_pid = bool(pid) and not process_exists(pid)
                has_unowned_stale_lock = not pid and lock_age >= self.lock_stale_seconds
                if has_dead_pid or has_unowned_stale_lock:
                    with contextlib.suppress(FileNotFoundError):
                        lock_path.unlink()
                    continue
                raise RuntimeError(
                    f"Task '{task_id}' is already running. Existing lock: {existing or str(lock_path)}"
                )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            yield
        finally:
            if lock_path.exists():
                lock_path.unlink()


class WorkflowEngine:
    def __init__(self, config: WorkflowConfig):
        text_cls = TEXT_GENERATOR_TYPES.get(config.text_provider_config.get("type", config.text_provider_name))
        if text_cls is None:
            raise ValueError(f"Unsupported text provider type: {config.text_provider_config.get('type')}")
        image_cls = IMAGE_GENERATOR_TYPES.get(config.image_provider_config.get("type", config.image_provider_name))
        if image_cls is None:
            raise ValueError(f"Unsupported image provider type: {config.image_provider_config.get('type')}")

        self.config = config
        self.text_generator = text_cls(config.text_provider_config)
        self.image_generator = image_cls(config.image_provider_config)
        self.store = TaskStore(config.output_root, lock_stale_seconds=config.task_lock_stale_seconds)

        self.outline_prompt = (REFERENCES_DIR / "outline-prompt.txt").read_text(encoding="utf-8")
        self.content_prompt = (REFERENCES_DIR / "content-prompt.txt").read_text(encoding="utf-8")
        self.image_prompt_full = (REFERENCES_DIR / "prompt-full.txt").read_text(encoding="utf-8")
        self.image_prompt_short = (REFERENCES_DIR / "prompt-short.txt").read_text(encoding="utf-8")

        self.short_prompt = bool(config.image_provider_config.get("short_prompt", False))
        self.high_concurrency = bool(config.image_provider_config.get("high_concurrency", False))
        self.max_workers = max(1, int(config.image_provider_config.get("max_workers", 1)))
        self.page_timeout_seconds = normalize_positive_int(
            config.image_provider_config.get("page_timeout_seconds"),
            120,
            "page_timeout_seconds",
        )
        self.scan_interval_seconds = normalize_positive_int(
            config.image_provider_config.get("scan_interval_seconds"),
            60,
            "scan_interval_seconds",
        )

    def _image_filename(self, index: int) -> str:
        return f"{index}.png"

    def _thumbnail_filename(self, index: int) -> str:
        return f"thumb_{self._image_filename(index)}"

    def _sync_state_with_files(self, task_id: str, state: dict[str, Any]) -> dict[str, Any]:
        task_dir = self.store.task_dir(task_id)
        pages = ensure_pages(state.get("pages") or [])
        generated = {
            str(key): str(value)
            for key, value in (state.get("generated") or {}).items()
            if value
        }
        failed = {
            str(key): str(value)
            for key, value in (state.get("failed") or {}).items()
            if value
        }
        changed = False

        for page in pages:
            idx = int(page["index"])
            key = str(idx)
            filename = self._image_filename(idx)
            image_path = task_dir / filename
            if image_path.exists():
                if generated.get(key) != filename:
                    generated[key] = filename
                    changed = True
                if key in failed:
                    failed.pop(key, None)
                    changed = True
                thumb_path = task_dir / self._thumbnail_filename(idx)
                if not thumb_path.exists():
                    save_png_bytes(make_thumbnail(image_path.read_bytes()), thumb_path)
                    changed = True
            elif generated.get(key) == filename:
                generated.pop(key, None)
                changed = True

        state["generated"] = generated
        state["failed"] = failed
        state["page_count"] = len(pages)

        if pages and not state.get("cover_image"):
            cover_page, _ = self._select_cover(pages)
            cover_path = task_dir / self._image_filename(int(cover_page["index"]))
            if cover_path.exists():
                state["cover_image"] = base64.b64encode(compress_image(cover_path.read_bytes())).decode("utf-8")
                changed = True

        if changed:
            self.store.save_state(task_id, state)
        return state

    def parse_outline(self, outline_text: str) -> list[dict[str, Any]]:
        if "<page>" in outline_text.lower():
            chunks = re.split(r"<page>", outline_text, flags=re.IGNORECASE)
        else:
            chunks = outline_text.split("---")
        type_mapping = {
            "封面": "cover",
            "内容": "content",
            "总结": "summary",
            "cover": "cover",
            "content": "content",
            "summary": "summary",
        }
        pages: list[dict[str, Any]] = []
        for index, raw in enumerate(chunks):
            page_text = raw.strip()
            if not page_text:
                continue
            page_type = "content"
            match = re.match(r"\[(\S+)\]", page_text)
            if match:
                page_type = type_mapping.get(match.group(1), "content")
            pages.append({"index": index, "type": page_type, "content": page_text})
        return pages

    def parse_content_bundle(self, raw_text: str) -> dict[str, Any]:
        payload_text = raw_text.strip()
        fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", payload_text, flags=re.DOTALL)
        if fence_match:
            payload_text = fence_match.group(1)
        data = json.loads(payload_text)
        titles = data.get("titles", [])
        tags = data.get("tags", [])
        if isinstance(titles, str):
            titles = [titles]
        if isinstance(tags, str):
            tags = [item.strip() for item in tags.split(",") if item.strip()]
        return {
            "success": True,
            "titles": titles,
            "copywriting": data.get("copywriting", ""),
            "tags": tags,
        }

    def build_image_prompt(self, page: dict[str, Any], outline: str, topic: str, style: Optional[str]) -> str:
        if self.short_prompt:
            return self.image_prompt_short.format(
                page_type=page["type"],
                page_content=page["content"],
                selected_style=style or "未指定，请根据故事内容自动选择最贴切的儿童绘本风格",
            )
        return self.image_prompt_full.format(
            page_type=page["type"],
            page_content=page["content"],
            full_outline=outline,
            user_topic=topic,
            selected_style=style or "未指定，请根据故事内容自动选择最贴切的儿童绘本风格",
        )

    def create_state(
        self,
        task_id: str,
        topic: str,
        outline: str,
        pages: list[dict[str, Any]],
        user_images: Optional[list[bytes]],
        style: Optional[str] = None,
        requested_page_count: Optional[int] = None,
    ) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "topic": topic,
            "user_topic": topic,
            "style": style,
            "requested_page_count": requested_page_count,
            "page_count": len(pages),
            "full_outline": outline,
            "outline_raw": outline,
            "pages": pages,
            "user_images": [base64.b64encode(item).decode("utf-8") for item in (user_images or [])],
            "cover_image": None,
            "generated": {},
            "failed": {},
            "content_bundle": None,
        }

    def generate_outline(
        self,
        topic: str,
        images: Optional[list[bytes]] = None,
        page_count: Optional[int] = None,
        style: Optional[str] = None,
    ) -> dict[str, Any]:
        prompt = self.outline_prompt.format(
            topic=topic,
            page_count_hint=page_count if page_count is not None else "未指定，由你根据故事复杂度决定",
            style_hint=style or "未指定，由你根据故事主题、情绪与场景自动选择最贴切的儿童绘本风格",
        )
        if images:
            prompt += (
                f"\n\n注意：用户提供了 {len(images)} 张参考图片，请在生成大纲时考虑这些图片的内容和风格。"
                "这些图片可能是角色参考、场景参考或色彩参考，请将它们融入儿童绘本的角色设定、场景设计或色彩氛围中。"
            )
        outline_text = self.text_generator.generate_text(
            prompt=prompt,
            model=self.config.text_provider_config.get("model"),
            temperature=self.config.text_provider_config.get("temperature", 1.0),
            max_output_tokens=self.config.text_provider_config.get("max_output_tokens", 8000),
            images=images or [],
            topic=topic,
            mode="outline",
        )
        pages = self.parse_outline(outline_text)
        return {
            "success": True,
            "outline": outline_text,
            "pages": pages,
            "has_images": bool(images),
        }

    def generate_content(self, topic: str, outline: str, style: Optional[str] = None) -> dict[str, Any]:
        prompt = self.content_prompt.format(
            topic=topic,
            outline=outline,
            style_hint=style or "未指定，由你根据故事内容自动判断最适合儿童绘本读者的视觉与叙事风格",
        )
        raw = self.text_generator.generate_text(
            prompt=prompt,
            model=self.config.text_provider_config.get("model"),
            temperature=self.config.text_provider_config.get("temperature", 1.0),
            max_output_tokens=self.config.text_provider_config.get("max_output_tokens", 8000),
            topic=topic,
            mode="content",
        )
        return self.parse_content_bundle(raw)

    def _save_page_image(self, task_id: str, index: int, image_data: bytes) -> str:
        task_dir = self.store.task_dir(task_id)
        filename = f"{index}.png"
        save_png_bytes(image_data, task_dir / filename)
        save_png_bytes(make_thumbnail(image_data), task_dir / f"thumb_{filename}")
        return filename

    def _load_reference_images(self, state: dict[str, Any], cover_image: Optional[bytes]) -> list[bytes]:
        refs = [compress_image(base64.b64decode(item)) for item in state.get("user_images", [])]
        if cover_image:
            refs.append(compress_image(cover_image))
        return refs

    def _generate_single_page(
        self,
        task_id: str,
        page: dict[str, Any],
        state: dict[str, Any],
        cover_image: Optional[bytes],
        timeout_seconds: Optional[int] = None,
    ) -> tuple[int, bool, Optional[str], Optional[str], Optional[bytes]]:
        prompt = self.build_image_prompt(
            page,
            state.get("full_outline", ""),
            state.get("topic", ""),
            state.get("style"),
        )
        kwargs: dict[str, Any] = {
            "model": self.config.image_provider_config.get("model"),
            "page_type": page["type"],
            "timeout_seconds": timeout_seconds or self.page_timeout_seconds,
        }
        refs = self._load_reference_images(state, cover_image)
        if self.image_generator.supports_multiple_reference_images and refs:
            kwargs["reference_images"] = refs
        elif self.image_generator.supports_reference_images and refs:
            kwargs["reference_image"] = refs[-1]

        if isinstance(self.image_generator, OpenAICompatibleImageGenerator):
            kwargs["size"] = self.config.image_provider_config.get("default_size", "1024x1024")
            kwargs["quality"] = self.config.image_provider_config.get("quality", "standard")
        else:
            kwargs["aspect_ratio"] = self.config.image_provider_config.get("default_aspect_ratio", "3:4")

        try:
            image_data = self.image_generator.generate_image(prompt=prompt, **kwargs)
            filename = self._save_page_image(task_id, int(page["index"]), image_data)
            return int(page["index"]), True, filename, None, image_data
        except Exception as exc:
            return int(page["index"]), False, None, str(exc), None

    def _select_cover(self, pages: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        cover = next((page for page in pages if page["type"] == "cover"), None)
        if cover is None:
            return pages[0], pages[1:]
        return cover, [page for page in pages if page is not cover]

    def _missing_pages(self, task_id: str, state: dict[str, Any]) -> list[dict[str, Any]]:
        task_dir = self.store.task_dir(task_id)
        pages = ensure_pages(state["pages"])
        missing: list[dict[str, Any]] = []
        for page in pages:
            index = int(page["index"])
            if not (task_dir / self._image_filename(index)).exists():
                missing.append(page)
        return missing

    def _scan_generation_progress(
        self,
        task_id: str,
        state: dict[str, Any],
        target_indices: list[int],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        state = self._sync_state_with_files(task_id, state)
        target = {int(item) for item in target_indices}
        generated = {
            int(index)
            for index in (state.get("generated") or {}).keys()
            if int(index) in target
        }
        failed = {
            int(index)
            for index in (state.get("failed") or {}).keys()
            if int(index) in target
        }
        missing = sorted(target - generated)
        progress = {
            "task_id": task_id,
            "target_indices": sorted(target),
            "target_count": len(target),
            "completed_indices": sorted(generated),
            "completed_count": len(generated),
            "missing_indices": missing,
            "missing_count": len(missing),
            "failed_indices": sorted(failed),
            "failed": {
                str(index): state.get("failed", {}).get(str(index))
                for index in sorted(failed)
            },
        }
        return state, progress

    def generate_images(
        self,
        task_id: str,
        state: dict[str, Any],
        only_missing: bool = False,
    ):
        state = self._sync_state_with_files(task_id, state)
        pages = self._missing_pages(task_id, state) if only_missing else ensure_pages(state["pages"])
        target_indices = [int(page["index"]) for page in pages]
        total_timeout_seconds = len(target_indices) * self.page_timeout_seconds
        started_at = time.time()
        deadline_monotonic = time.monotonic() + total_timeout_seconds
        next_scan_monotonic = time.monotonic()

        state["image_generation_runtime"] = {
            "only_missing": only_missing,
            "target_indices": target_indices,
            "target_count": len(target_indices),
            "page_timeout_seconds": self.page_timeout_seconds,
            "scan_interval_seconds": self.scan_interval_seconds,
            "total_timeout_seconds": total_timeout_seconds,
            "started_at": started_at,
            "deadline_at": started_at + total_timeout_seconds,
        }
        self.store.save_state(task_id, state)

        if not pages:
            state, progress = self._scan_generation_progress(task_id, state, target_indices)
            self.store.save_run_status(task_id, "finished", {"success": True, "status": "complete"})
            yield {
                "event": "finish",
                "data": {
                    "success": True,
                    "task_id": task_id,
                    "status": "complete",
                    **progress,
                    "generated": state["generated"],
                    "failed": state["failed"],
                },
            }
            return
        yield {
            "event": "generation_window",
            "data": {
                "task_id": task_id,
                "only_missing": only_missing,
                "target_indices": target_indices,
                "target_count": len(target_indices),
                "page_timeout_seconds": self.page_timeout_seconds,
                "scan_interval_seconds": self.scan_interval_seconds,
                "total_timeout_seconds": total_timeout_seconds,
            },
        }
        cover_page, other_pages = self._select_cover(pages)

        if state.get("cover_image") and cover_page["type"] != "cover":
            cover_page = None
            other_pages = pages

        if cover_page is not None:
            if CANCEL_REQUESTED.is_set() or time.monotonic() >= deadline_monotonic:
                state, progress = self._scan_generation_progress(task_id, state, target_indices)
                yield {"event": "timeout", "data": progress}
                yield {
                    "event": "finish",
                    "data": {
                        "success": False,
                        "task_id": task_id,
                        "status": "timeout",
                        **progress,
                        "generated": state["generated"],
                        "failed": state["failed"],
                    },
                }
                return
            timeout_for_page = max(1, min(self.page_timeout_seconds, int(deadline_monotonic - time.monotonic())))
            yield {"event": "progress", "data": {"index": cover_page["index"], "status": "generating", "phase": "cover"}}
            index, success, filename, error, cover_data = self._generate_single_page(
                task_id,
                cover_page,
                state,
                None,
                timeout_seconds=timeout_for_page,
            )
            if success:
                state["generated"][str(index)] = filename
                state["failed"].pop(str(index), None)
                state["cover_image"] = base64.b64encode(compress_image(cover_data or b"")).decode("utf-8") if cover_data else None
                self.store.save_state(task_id, state)
                yield {"event": "complete", "data": {"index": index, "status": "done", "filename": filename, "phase": "cover"}}
            else:
                state["failed"][str(index)] = error
                self.store.save_state(task_id, state)
                yield {"event": "error", "data": {"index": index, "status": "error", "message": error, "phase": "cover"}}
            if time.monotonic() >= next_scan_monotonic:
                state, progress = self._scan_generation_progress(task_id, state, target_indices)
                yield {"event": "scan", "data": progress}
                next_scan_monotonic = time.monotonic() + self.scan_interval_seconds

        cover_ref = base64.b64decode(state["cover_image"]) if state.get("cover_image") else None
        if other_pages:
            yield {"event": "progress", "data": {"status": "batch_start", "phase": "content", "count": len(other_pages)}}
            if self.high_concurrency and self.max_workers > 1:
                with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                    futures = {
                        pool.submit(
                            self._generate_single_page,
                            task_id,
                            page,
                            state,
                            cover_ref,
                            timeout_seconds=self.page_timeout_seconds,
                        ): page
                        for page in other_pages
                    }
                    for future in as_completed(futures):
                        idx, ok, fname, err, _ = future.result()
                        if ok:
                            state["generated"][str(idx)] = fname
                            state["failed"].pop(str(idx), None)
                            yield {"event": "complete", "data": {"index": idx, "status": "done", "filename": fname, "phase": "content"}}
                        else:
                            state["failed"][str(idx)] = err
                            yield {"event": "error", "data": {"index": idx, "status": "error", "message": err, "phase": "content"}}
                        self.store.save_state(task_id, state)
                        if time.monotonic() >= next_scan_monotonic:
                            state, progress = self._scan_generation_progress(task_id, state, target_indices)
                            yield {"event": "scan", "data": progress}
                            next_scan_monotonic = time.monotonic() + self.scan_interval_seconds
            else:
                for page in other_pages:
                    if CANCEL_REQUESTED.is_set() or time.monotonic() >= deadline_monotonic:
                        state, progress = self._scan_generation_progress(task_id, state, target_indices)
                        yield {"event": "timeout", "data": progress}
                        break
                    timeout_for_page = max(1, min(self.page_timeout_seconds, int(deadline_monotonic - time.monotonic())))
                    yield {"event": "progress", "data": {"index": page["index"], "status": "generating", "phase": "content"}}
                    idx, ok, fname, err, _ = self._generate_single_page(
                        task_id,
                        page,
                        state,
                        cover_ref,
                        timeout_seconds=timeout_for_page,
                    )
                    if ok:
                        state["generated"][str(idx)] = fname
                        state["failed"].pop(str(idx), None)
                        yield {"event": "complete", "data": {"index": idx, "status": "done", "filename": fname, "phase": "content"}}
                    else:
                        state["failed"][str(idx)] = err
                        yield {"event": "error", "data": {"index": idx, "status": "error", "message": err, "phase": "content"}}
                    self.store.save_state(task_id, state)
                    if time.monotonic() >= next_scan_monotonic:
                        state, progress = self._scan_generation_progress(task_id, state, target_indices)
                        yield {"event": "scan", "data": progress}
                        next_scan_monotonic = time.monotonic() + self.scan_interval_seconds

        state, progress = self._scan_generation_progress(task_id, state, target_indices)
        timed_out = bool(progress["missing_indices"]) and time.monotonic() >= deadline_monotonic
        finish_success = not progress["missing_indices"] and not progress["failed_indices"]
        finish_status = "timeout" if timed_out else ("complete" if finish_success else "partial")
        self.store.save_run_status(task_id, "finished", {"success": finish_success, "status": finish_status})
        yield {
            "event": "finish",
            "data": {
                "success": finish_success,
                "task_id": task_id,
                "status": finish_status,
                **progress,
                "generated": state["generated"],
                "failed": state["failed"],
                "continue_command": (
                    f"generate-images --config <config> --input {self.store.state_path(task_id)} --only-missing"
                    if progress["missing_indices"] else None
                ),
            },
        }

    def retry_single(self, task_id: str, page: dict[str, Any], use_reference: bool = True) -> dict[str, Any]:
        with self.store.task_lock(task_id):
            state = self._sync_state_with_files(task_id, self.store.load_state(task_id))
            cover_ref = base64.b64decode(state["cover_image"]) if (use_reference and state.get("cover_image")) else None
            idx, ok, fname, err, _ = self._generate_single_page(task_id, ensure_page(page), state, cover_ref)
            if ok:
                state["generated"][str(idx)] = fname
                state["failed"].pop(str(idx), None)
            else:
                state["failed"][str(idx)] = err
            self.store.save_state(task_id, state)
            return {"success": ok, "index": idx, "filename": fname, "error": err}

    def regenerate_single(
        self,
        task_id: str,
        page: dict[str, Any],
        use_reference: bool = True,
        full_outline: Optional[str] = None,
        user_topic: Optional[str] = None,
    ) -> dict[str, Any]:
        with self.store.task_lock(task_id):
            state = self._sync_state_with_files(task_id, self.store.load_state(task_id))
            if full_outline is not None:
                state["full_outline"] = full_outline
                state["outline_raw"] = full_outline
            if user_topic is not None:
                state["topic"] = user_topic
                state["user_topic"] = user_topic
            self.store.save_state(task_id, state)
        return self.retry_single(task_id, page, use_reference=use_reference)

    def task_state(self, task_id: str) -> dict[str, Any]:
        state = self._sync_state_with_files(task_id, self.store.load_state(task_id))
        task_dir = self.store.task_dir(task_id)
        return {
            "success": True,
            "task_id": task_id,
            "task_dir": str(task_dir),
            "topic": state.get("topic"),
            "style": state.get("style"),
            "requested_page_count": state.get("requested_page_count"),
            "outline": state.get("outline_raw"),
            "pages": state.get("pages", []),
            "content_bundle": state.get("content_bundle"),
            "generated": state.get("generated", {}),
            "failed": state.get("failed", {}),
            "has_cover": bool(state.get("cover_image")),
            "files": sorted(file.name for file in task_dir.glob("*") if file.is_file()),
        }

    def run(
        self,
        topic: str,
        task_id: Optional[str],
        user_images: Optional[list[bytes]],
        page_count: Optional[int] = None,
        style: Optional[str] = None,
        skip_content: bool = False,
    ):
        current_task_id = task_id or f"task_{uuid.uuid4().hex[:8]}"
        with self.store.task_lock(current_task_id):
            status_path = self.store.save_run_status(
                current_task_id,
                "locked",
                {
                    "topic": topic,
                    "page_count": page_count,
                    "style": style,
                },
            )
            yield {
                "event": "run_start",
                "data": {
                    "task_id": current_task_id,
                    "status_file": str(status_path),
                    "pid": os.getpid(),
                },
            }
            if self.store.task_exists(current_task_id):
                raise RuntimeError(
                    f"Task '{current_task_id}' already exists. Use retry/regenerate for single pages or generate-images --only-missing."
                )
            self.store.save_run_status(current_task_id, "generating_outline", {"topic": topic})
            outline_result = self.generate_outline(topic, user_images, page_count=page_count, style=style)
            state = self.create_state(
                task_id=current_task_id,
                topic=topic,
                outline=outline_result["outline"],
                pages=outline_result["pages"],
                user_images=user_images,
                style=style,
                requested_page_count=page_count,
            )
            self.store.save_state(current_task_id, state)
            yield {
                "event": "outline_complete",
                "data": {
                    "task_id": current_task_id,
                    "outline": outline_result["outline"],
                    "pages": outline_result["pages"],
                    "page_count": len(outline_result["pages"]),
                },
            }

            if not skip_content:
                self.store.save_run_status(current_task_id, "generating_content", {"topic": topic})
                content_bundle = self.generate_content(topic, outline_result["outline"], style=style)
                state["content_bundle"] = content_bundle
                self.store.save_state(current_task_id, state)
                yield {"event": "content_complete", "data": content_bundle}

            self.store.save_run_status(current_task_id, "generating_images", {"topic": topic})
            for event in self.generate_images(current_task_id, state):
                yield event


def workflow_from_config(config_path: Path, provider_override: Optional[str], allow_demo: bool = False) -> WorkflowEngine:
    raw = load_yaml_config(config_path)
    if not allow_demo:
        assert_generation_config_ready(raw, config_path)
    text_generation = raw.get("text_generation", {})
    image_generation = raw.get("image_generation", {})
    text_provider_name = text_generation.get("active_provider")
    image_provider_name = provider_override or image_generation.get("active_provider")

    text_providers = text_generation.get("providers", {})
    image_providers = image_generation.get("providers", {})
    if text_provider_name not in text_providers:
        raise ValueError(f"Text provider not found: {text_provider_name}")
    if image_provider_name not in image_providers:
        raise ValueError(f"Image provider not found: {image_provider_name}")

    output_root = Path(raw.get("output_root", "./tasks"))
    if not output_root.is_absolute():
        output_root = (config_path.parent / output_root).resolve()

    cfg = WorkflowConfig(
        text_provider_name=text_provider_name,
        text_provider_config=text_providers[text_provider_name],
        image_provider_name=image_provider_name,
        image_provider_config=image_providers[image_provider_name],
        output_root=output_root,
        task_lock_stale_seconds=normalize_positive_int(
            raw.get("task_lock_stale_seconds"),
            300,
            "task_lock_stale_seconds",
        ),
    )
    return WorkflowEngine(cfg)


def command_init_config(args: argparse.Namespace) -> int:
    target = Path(args.output) if args.output else DEFAULT_CONFIG_PATH
    if not target.is_absolute():
        target = (Path.cwd() / target).resolve()
    if target.exists() and not args.force:
        raise FileExistsError(f"File already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text((REFERENCES_DIR / "config.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    json_print({"success": True, "output": str(target.resolve())}, compact=args.compact)
    return 0


def command_config(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.config)
    raw = load_yaml_config(config_path)
    text_name = raw.get("text_generation", {}).get("active_provider")
    image_name = args.provider or raw.get("image_generation", {}).get("active_provider")
    text_cfg = dict(raw.get("text_generation", {}).get("providers", {}).get(text_name, {}))
    image_cfg = dict(raw.get("image_generation", {}).get("providers", {}).get(image_name, {}))
    if "api_key" in text_cfg:
        text_cfg["api_key"] = mask_secret(str(text_cfg["api_key"]))
    if "api_key" in image_cfg:
        image_cfg["api_key"] = mask_secret(str(image_cfg["api_key"]))
    result = {
        "success": True,
        "ready_for_generation": not any(
            (
                is_demo_provider(text_name, text_cfg),
                is_demo_provider(image_name, image_cfg),
            )
        )
        or bool(raw.get("allow_demo_providers", False)),
        "allow_demo_providers": bool(raw.get("allow_demo_providers", False)),
        "config_path": str(config_path.resolve()),
        "output_root": raw.get("output_root", "./tasks"),
        "text_active_provider": text_name,
        "image_active_provider": raw.get("image_generation", {}).get("active_provider"),
        "selected_image_provider": image_name,
        "uses_demo_text_provider": is_demo_provider(text_name, text_cfg),
        "uses_demo_image_provider": is_demo_provider(image_name, image_cfg),
        "text_provider_config": text_cfg,
        "image_provider_config": image_cfg,
    }
    json_print(result, compact=args.compact)
    return 0


def command_generate_outline(args: argparse.Namespace) -> int:
    workflow = workflow_from_config(resolve_config_path(args.config), args.provider)
    payload = read_json_source(args.input)
    if not isinstance(payload, dict):
        raise ValueError("Outline input must be a JSON object.")
    topic = read_text_or_value(payload.get("topic") or payload.get("user_topic"))
    if not topic:
        raise ValueError("Payload field 'topic' or 'user_topic' is required.")
    user_images = [Path(item).read_bytes() for item in payload.get("user_images", [])]
    page_count = normalize_page_count(payload.get("page_count"))
    style = normalize_requested_style(payload.get("style"))
    result = workflow.generate_outline(topic, user_images or None, page_count=page_count, style=style)
    json_print(result, compact=args.compact)
    return 0


def command_generate_content(args: argparse.Namespace) -> int:
    workflow = workflow_from_config(resolve_config_path(args.config), args.provider)
    payload = read_json_source(args.input)
    if not isinstance(payload, dict):
        raise ValueError("Content input must be a JSON object.")
    topic = read_text_or_value(payload.get("topic") or payload.get("user_topic"))
    outline = read_text_or_value(payload.get("outline") or payload.get("full_outline"))
    if not topic or not outline:
        raise ValueError("Payload fields 'topic' and 'outline' or 'full_outline' are required.")
    style = normalize_requested_style(payload.get("style"))
    result = workflow.generate_content(topic, outline, style=style)
    json_print(result, compact=args.compact)
    return 0


def command_generate_images(args: argparse.Namespace) -> int:
    workflow = workflow_from_config(resolve_config_path(args.config), args.provider)
    payload = read_json_source(args.input)
    if not isinstance(payload, dict):
        raise ValueError("Image input must be a JSON object.")
    task_id = payload.get("task_id") or f"task_{uuid.uuid4().hex[:8]}"
    if args.only_missing:
        state = workflow.store.load_state(task_id)
    else:
        if workflow.store.task_exists(task_id):
            raise RuntimeError(
                f"Task '{task_id}' already exists. Use --only-missing to continue unfinished pages, or choose a new task_id."
            )
        pages = ensure_pages(payload.get("pages"))
        topic = read_text_or_value(payload.get("topic") or payload.get("user_topic"))
        outline = read_text_or_value(payload.get("outline") or payload.get("full_outline"))
        user_images = [Path(item).read_bytes() for item in payload.get("user_images", [])]
        page_count = normalize_page_count(payload.get("page_count"))
        style = normalize_requested_style(payload.get("style"))
        state = workflow.create_state(
            task_id=task_id,
            topic=topic,
            outline=outline,
            pages=pages,
            user_images=user_images or None,
            style=style,
            requested_page_count=page_count,
        )
        workflow.store.save_state(task_id, state)
    success = False
    with workflow.store.task_lock(task_id):
        for event in workflow.generate_images(task_id, state, only_missing=args.only_missing):
            json_print(event, compact=args.compact)
            if event["event"] == "finish":
                success = bool(event["data"]["success"])
    json_print({"event": "cli_exit", "success": success, "task_id": task_id, "exit_code": 0 if success else 1}, compact=args.compact)
    return 0 if success else 1


def command_run(args: argparse.Namespace) -> int:
    workflow = workflow_from_config(resolve_config_path(args.config), args.provider)
    payload = read_json_source(args.input)
    if not isinstance(payload, dict):
        raise ValueError("Run input must be a JSON object.")
    topic = read_text_or_value(payload.get("topic") or payload.get("user_topic"))
    if not topic:
        raise ValueError("Payload field 'topic' or 'user_topic' is required.")
    user_images = [Path(item).read_bytes() for item in payload.get("user_images", [])]
    page_count = normalize_page_count(payload.get("page_count"))
    style = normalize_requested_style(payload.get("style"))
    success = False
    task_id = payload.get("task_id") or f"task_{uuid.uuid4().hex[:8]}"
    try:
        for event in workflow.run(
            topic=topic,
            task_id=task_id,
            user_images=user_images or None,
            page_count=page_count,
            style=style,
            skip_content=args.skip_content,
        ):
            json_print(event, compact=args.compact)
            if event["event"] == "outline_complete":
                task_id = event["data"]["task_id"]
            if event["event"] == "finish":
                success = bool(event["data"]["success"])
    except Exception as exc:
        if task_id:
            error_path = workflow.store.save_error(task_id, str(exc), "run")
            json_print(
                {"success": False, "event": "fatal", "task_id": task_id, "error": str(exc), "error_file": str(error_path)},
                compact=args.compact,
            )
        raise
    json_print({"event": "cli_exit", "success": success, "task_id": task_id, "exit_code": 0 if success else 1}, compact=args.compact)
    return 0 if success else 1


def command_run_topic(args: argparse.Namespace) -> int:
    workflow = workflow_from_config(resolve_config_path(args.config), args.provider)
    topic = read_text_or_value(args.topic)
    if not topic:
        raise ValueError("--topic is required.")
    user_images = [Path(item).read_bytes() for item in args.user_image]
    page_count = normalize_page_count(args.page_count)
    style = normalize_requested_style(args.style)
    success = False
    task_id = args.task_id or f"task_{uuid.uuid4().hex[:8]}"
    try:
        for event in workflow.run(
            topic=topic,
            task_id=task_id,
            user_images=user_images or None,
            page_count=page_count,
            style=style,
            skip_content=args.skip_content,
        ):
            json_print(event, compact=args.compact)
            if event["event"] == "outline_complete":
                task_id = event["data"]["task_id"]
            if event["event"] == "finish":
                success = bool(event["data"]["success"])
    except Exception as exc:
        if task_id:
            error_path = workflow.store.save_error(task_id, str(exc), "run-topic")
            json_print(
                {"success": False, "event": "fatal", "task_id": task_id, "error": str(exc), "error_file": str(error_path)},
                compact=args.compact,
            )
        raise
    json_print({"event": "cli_exit", "success": success, "task_id": task_id, "exit_code": 0 if success else 1}, compact=args.compact)
    return 0 if success else 1


def command_retry(args: argparse.Namespace) -> int:
    workflow = workflow_from_config(resolve_config_path(args.config), args.provider)
    page = ensure_page(read_json_source(args.page))
    result = workflow.retry_single(args.task_id, page, use_reference=not args.no_reference)
    json_print(result, compact=args.compact)
    return 0 if result["success"] else 1


def command_regenerate(args: argparse.Namespace) -> int:
    workflow = workflow_from_config(resolve_config_path(args.config), args.provider)
    page = ensure_page(read_json_source(args.page))
    result = workflow.regenerate_single(
        task_id=args.task_id,
        page=page,
        use_reference=not args.no_reference,
        full_outline=read_text_or_value(args.full_outline) if args.full_outline is not None else None,
        user_topic=read_text_or_value(args.user_topic) if args.user_topic is not None else None,
    )
    json_print(result, compact=args.compact)
    return 0 if result["success"] else 1


def command_task_state(args: argparse.Namespace) -> int:
    if args.config:
        workflow = workflow_from_config(resolve_config_path(args.config), args.provider)
    else:
        cfg = WorkflowConfig(
            text_provider_name="mock_text",
            text_provider_config={"type": "mock_text"},
            image_provider_name="mock",
            image_provider_config={"type": "mock"},
            output_root=DEFAULT_OUTPUT_ROOT,
        )
        workflow = WorkflowEngine(cfg)
    try:
        result = workflow.task_state(args.task_id)
    except FileNotFoundError:
        result = workflow.store.diagnose(args.task_id)
        result["success"] = False
        result["error"] = result.get("error") or "Task state not found."
    json_print(result, compact=args.compact)
    return 0


def command_diagnose_task(args: argparse.Namespace) -> int:
    if args.config:
        workflow = workflow_from_config(resolve_config_path(args.config), args.provider)
        store = workflow.store
    else:
        store = TaskStore(DEFAULT_OUTPUT_ROOT)
    json_print(store.diagnose(args.task_id), compact=args.compact)
    return 0


def command_cleanup_lock(args: argparse.Namespace) -> int:
    if args.config:
        workflow = workflow_from_config(resolve_config_path(args.config), args.provider)
        store = workflow.store
    else:
        store = TaskStore(DEFAULT_OUTPUT_ROOT)
    result = store.cleanup_lock(args.task_id, force=args.force)
    json_print(result, compact=args.compact)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Standalone text and image workflow.")
    parser.add_argument(
        "--watch-parent",
        action="store_true",
        help="Exit when the parent process exits. Disabled by default because OpenClaw can re-parent long-running tasks.",
    )
    parser.add_argument(
        "--no-force-exit",
        action="store_true",
        help="Do not forcefully terminate the Python process after the CLI command returns.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_config = subparsers.add_parser("init-config", help="Write an example config file.")
    init_config.add_argument("--output", help="Target YAML path. Defaults to the fixed skill-root workflow_config.yaml.")
    init_config.add_argument("--force", action="store_true", help="Overwrite an existing file.")
    init_config.add_argument("--compact", action="store_true")
    init_config.set_defaults(func=command_init_config)

    config_cmd = subparsers.add_parser("config", help="Inspect a config file.")
    config_cmd.add_argument("--config")
    config_cmd.add_argument("--provider")
    config_cmd.add_argument("--compact", action="store_true")
    config_cmd.set_defaults(func=command_config)

    outline_cmd = subparsers.add_parser("generate-outline", help="Generate outline pages from a topic.")
    outline_cmd.add_argument("--config")
    outline_cmd.add_argument("--input", required=True)
    outline_cmd.add_argument("--provider")
    outline_cmd.add_argument("--compact", action="store_true")
    outline_cmd.set_defaults(func=command_generate_outline)

    content_cmd = subparsers.add_parser("generate-content", help="Generate titles, copy, and tags from an outline.")
    content_cmd.add_argument("--config")
    content_cmd.add_argument("--input", required=True)
    content_cmd.add_argument("--provider")
    content_cmd.add_argument("--compact", action="store_true")
    content_cmd.set_defaults(func=command_generate_content)

    image_cmd = subparsers.add_parser("generate-images", help="Generate images from prepared pages.")
    image_cmd.add_argument("--config")
    image_cmd.add_argument("--input", required=True)
    image_cmd.add_argument("--provider")
    image_cmd.add_argument("--only-missing", action="store_true", help="Generate only pages that do not already exist on disk.")
    image_cmd.add_argument("--compact", action="store_true")
    image_cmd.set_defaults(func=command_generate_images)

    run_cmd = subparsers.add_parser("run", help="Run topic to outline to content to images.")
    run_cmd.add_argument("--config")
    run_cmd.add_argument("--input", required=True)
    run_cmd.add_argument("--provider")
    run_cmd.add_argument("--skip-content", action="store_true")
    run_cmd.add_argument("--compact", action="store_true")
    run_cmd.set_defaults(func=command_run)

    run_topic_cmd = subparsers.add_parser("run-topic", help="Run the full workflow directly from a natural-language topic.")
    run_topic_cmd.add_argument("--config")
    run_topic_cmd.add_argument("--topic", required=True, help="Natural-language picture-book request or a path to a text file.")
    run_topic_cmd.add_argument("--task-id")
    run_topic_cmd.add_argument("--page-count")
    run_topic_cmd.add_argument("--style")
    run_topic_cmd.add_argument("--user-image", action="append", default=[])
    run_topic_cmd.add_argument("--provider")
    run_topic_cmd.add_argument("--skip-content", action="store_true")
    run_topic_cmd.add_argument("--compact", action="store_true")
    run_topic_cmd.set_defaults(func=command_run_topic)

    retry_cmd = subparsers.add_parser("retry", help="Retry a single page.")
    retry_cmd.add_argument("--config")
    retry_cmd.add_argument("--task-id", required=True)
    retry_cmd.add_argument("--page", required=True)
    retry_cmd.add_argument("--provider")
    retry_cmd.add_argument("--no-reference", action="store_true")
    retry_cmd.add_argument("--compact", action="store_true")
    retry_cmd.set_defaults(func=command_retry)

    regenerate_cmd = subparsers.add_parser("regenerate", help="Regenerate a single page.")
    regenerate_cmd.add_argument("--config")
    regenerate_cmd.add_argument("--task-id", required=True)
    regenerate_cmd.add_argument("--page", required=True)
    regenerate_cmd.add_argument("--provider")
    regenerate_cmd.add_argument("--no-reference", action="store_true")
    regenerate_cmd.add_argument("--full-outline")
    regenerate_cmd.add_argument("--user-topic")
    regenerate_cmd.add_argument("--compact", action="store_true")
    regenerate_cmd.set_defaults(func=command_regenerate)

    task_state_cmd = subparsers.add_parser("task-state", help="Inspect persisted task state.")
    task_state_cmd.add_argument("--task-id", required=True)
    task_state_cmd.add_argument("--config")
    task_state_cmd.add_argument("--provider")
    task_state_cmd.add_argument("--compact", action="store_true")
    task_state_cmd.set_defaults(func=command_task_state)

    diagnose_cmd = subparsers.add_parser("diagnose-task", help="Inspect task directory, lock, status, and error files even when state is missing.")
    diagnose_cmd.add_argument("--task-id", required=True)
    diagnose_cmd.add_argument("--config")
    diagnose_cmd.add_argument("--provider")
    diagnose_cmd.add_argument("--compact", action="store_true")
    diagnose_cmd.set_defaults(func=command_diagnose_task)

    cleanup_lock_cmd = subparsers.add_parser("cleanup-lock", help="Remove a stale task lock.")
    cleanup_lock_cmd.add_argument("--task-id", required=True)
    cleanup_lock_cmd.add_argument("--config")
    cleanup_lock_cmd.add_argument("--provider")
    cleanup_lock_cmd.add_argument("--force", action="store_true", help="Remove the lock even if its pid appears alive.")
    cleanup_lock_cmd.add_argument("--compact", action="store_true")
    cleanup_lock_cmd.set_defaults(func=command_cleanup_lock)

    return parser


def main() -> int:
    global FORCE_EXIT_AFTER_MAIN
    install_shutdown_handlers()
    parser = build_parser()
    args = parser.parse_args()
    FORCE_EXIT_AFTER_MAIN = force_exit_enabled(args)
    if parent_watchdog_enabled(args):
        start_parent_watchdog()
    try:
        return args.func(args)
    except Exception as exc:
        json_print({"success": False, "error": str(exc)}, compact=getattr(args, "compact", False))
        return 1


if __name__ == "__main__":
    exit_process(main(), force=FORCE_EXIT_AFTER_MAIN)
