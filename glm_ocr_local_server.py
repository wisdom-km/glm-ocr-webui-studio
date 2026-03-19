import argparse
import base64
import io
import os
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import requests
import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from PIL import Image
from transformers import AutoModelForImageTextToText, AutoProcessor


ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "hf-cache"
MODEL_ID = "zai-org/GLM-OCR"
HF_HUB_MODELS_DIR = Path.home() / ".cache" / "huggingface" / "hub"
SPECIAL_TOKEN_RE = re.compile(r"<\|[^|]+\|>")
MAX_NEW_TOKENS_LIMIT = int(os.environ.get("GLMOCR_MAX_NEW_TOKENS", "4096"))

os.environ.setdefault("HF_HOME", str(CACHE_DIR))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
# Windows + hf_transfer occasionally raises:
# "Cannot send a request, as the client has been closed."
# Prefer stable default; if users need hf_transfer they can override externally.
os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "0")
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

REQUEST_COUNTER = 0
REQUEST_COUNTER_LOCK = threading.Lock()
LAST_STATUS_LOG_AT = 0.0


def server_log(level: str, message: str, **fields: Any) -> None:
    suffix = " | ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    if suffix:
        print(f"[GLM-OCR Local Server] [{level}] {message} | {suffix}")
    else:
        print(f"[GLM-OCR Local Server] [{level}] {message}")


def next_request_id() -> str:
    global REQUEST_COUNTER
    with REQUEST_COUNTER_LOCK:
        REQUEST_COUNTER += 1
        return f"req_{REQUEST_COUNTER:06d}"


def maybe_log_status(**fields: Any) -> None:
    global LAST_STATUS_LOG_AT
    now = time.time()
    if now - LAST_STATUS_LOG_AT < 15:
        return
    LAST_STATUS_LOG_AT = now
    server_log("INFO", "status check", **fields)


def format_cuda_memory_snapshot() -> str | None:
    if not torch.cuda.is_available():
        return None


def resolve_local_model_source(model_id: str) -> str:
    override = os.environ.get("GLMOCR_MODEL_PATH", "").strip()
    if override:
        override_path = Path(override).expanduser()
        if override_path.exists():
            return str(override_path)

    repo_cache_dir = HF_HUB_MODELS_DIR / f"models--{model_id.replace('/', '--')}"
    snapshots_dir = repo_cache_dir / "snapshots"
    if snapshots_dir.exists():
        snapshot_candidates = sorted(
            [p for p in snapshots_dir.iterdir() if p.is_dir()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for candidate in snapshot_candidates:
            if (candidate / "config.json").exists() and (candidate / "model.safetensors").exists():
                return str(candidate)
    return model_id
    try:
        device = torch.cuda.current_device()
        allocated = torch.cuda.memory_allocated(device) / (1024 * 1024)
        reserved = torch.cuda.memory_reserved(device) / (1024 * 1024)
        max_allocated = torch.cuda.max_memory_allocated(device) / (1024 * 1024)
        return (
            f"allocated={allocated:.0f}MiB,"
            f"reserved={reserved:.0f}MiB,"
            f"max_allocated={max_allocated:.0f}MiB"
        )
    except Exception:
        return None


class MessageContentPart(BaseModel):
    type: str
    text: str | None = None
    image_url: dict[str, Any] | str | None = None


class ChatMessage(BaseModel):
    role: str
    content: list[MessageContentPart] | str


class ChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage]
    max_tokens: int | None = 2048
    temperature: float | None = None
    trace_task_id: str | None = None
    trace_request_id: str | None = None
    trace_page: int | None = None
    trace_region: int | None = None
    trace_stage: str | None = None


class ModelRuntime:
    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        self.lock = threading.Lock()
        self.processor = None
        self.model = None
        self.device_description = "unknown"
        self.loaded = False
        self.loading = False
        self.last_load_error: str | None = None
        self.load_lock = threading.Lock()

    def load(self) -> None:
        if self.model is not None and self.processor is not None:
            return
        with self.load_lock:
            if self.model is not None and self.processor is not None:
                return
            self.loading = True
            try:
                cuda_available = torch.cuda.is_available()
                if cuda_available:
                    gpu_name = torch.cuda.get_device_name(0)
                    print(f"[GLM-OCR Local Server] CUDA available: True")
                    print(f"[GLM-OCR Local Server] GPU: {gpu_name}")
                else:
                    print("[GLM-OCR Local Server] CUDA available: False")
                    print("[GLM-OCR Local Server] Falling back to CPU")
                self.last_load_error = None
                model_source = resolve_local_model_source(self.model_id)
                server_log(
                    "INFO",
                    "model load source",
                    model=self.model_id,
                    source=model_source,
                )
                try:
                    self.processor = AutoProcessor.from_pretrained(
                        model_source,
                        local_files_only=True,
                    )
                    self.model = AutoModelForImageTextToText.from_pretrained(
                        pretrained_model_name_or_path=model_source,
                        torch_dtype="auto",
                        device_map="auto",
                        local_files_only=True,
                    )
                except Exception as local_exc:
                    server_log(
                        "WARN",
                        "local cache load failed; fallback to online",
                        model=self.model_id,
                        source=model_source,
                        error=str(local_exc),
                    )
                    self.processor = AutoProcessor.from_pretrained(self.model_id)
                    self.model = AutoModelForImageTextToText.from_pretrained(
                        pretrained_model_name_or_path=self.model_id,
                        torch_dtype="auto",
                        device_map="auto",
                    )
                self.device_description = str(getattr(self.model, "device", "unknown"))
                self.loaded = True
                print(f"[GLM-OCR Local Server] Model loaded: {self.model_id}")
                print(f"[GLM-OCR Local Server] Model device: {self.device_description}")
            except Exception as exc:
                self.model = None
                self.processor = None
                self.loaded = False
                self.device_description = "unknown"
                self.last_load_error = str(exc)
                server_log("ERROR", "model load failed", model=self.model_id, error=self.last_load_error)
                raise
            finally:
                self.loading = False

    def ensure_loading_async(self) -> bool:
        if self.loaded or self.loading:
            return False

        def _target() -> None:
            try:
                self.load()
            except Exception as exc:
                print(f"[GLM-OCR Local Server] Warmup failed: {exc}")

        threading.Thread(target=_target, daemon=True).start()
        return True

    def generate(
        self,
        messages: list[dict[str, Any]],
        max_new_tokens: int,
        request_id: str = "unknown",
        task_id: str | None = None,
        page: int | None = None,
        region: int | None = None,
    ) -> str:
        self.load()
        assert self.processor is not None
        assert self.model is not None
        with self.lock:
            generate_started_at = time.time()
            image_count = sum(
                1
                for message in messages
                for part in message.get("content", [])
                if part.get("type") == "image"
            )
            text_count = sum(
                1
                for message in messages
                for part in message.get("content", [])
                if part.get("type") == "text"
            )
            image_sizes = [
                f"{part['image'].size[0]}x{part['image'].size[1]}"
                for message in messages
                for part in message.get("content", [])
                if part.get("type") == "image" and part.get("image") is not None
            ]
            cuda_before = format_cuda_memory_snapshot()
            server_log(
                "INFO",
                "generate start",
                request_id=request_id,
                task_id=task_id,
                page=page,
                region=region,
                images=image_count,
                texts=text_count,
                max_new_tokens=max_new_tokens,
                device=self.device_description,
                image_sizes=",".join(image_sizes[:4]) if image_sizes else None,
                cuda_before=cuda_before,
            )
            inputs = self.processor.apply_chat_template(
                messages,
                tokenize=True,
                add_generation_prompt=True,
                return_dict=True,
                return_tensors="pt",
            ).to(self.model.device)
            inputs.pop("token_type_ids", None)
            prompt_tokens = int(inputs["input_ids"].shape[1]) if "input_ids" in inputs else None
            image_tensor_shape = None
            if "pixel_values" in inputs:
                image_tensor_shape = "x".join(str(dim) for dim in inputs["pixel_values"].shape)
            server_log(
                "INFO",
                "generate inputs",
                request_id=request_id,
                task_id=task_id,
                page=page,
                region=region,
                prompt_tokens=prompt_tokens,
                image_tensor_shape=image_tensor_shape,
                max_new_tokens=max_new_tokens,
            )
            generated_ids = self.model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
            )
            output_text = self.processor.decode(
                generated_ids[0][inputs["input_ids"].shape[1] :],
                skip_special_tokens=True,
            )
            generated_tokens = max(0, int(generated_ids[0].shape[0] - inputs["input_ids"].shape[1]))
            cuda_after = format_cuda_memory_snapshot()
            server_log(
                "INFO",
                "generate end",
                request_id=request_id,
                task_id=task_id,
                page=page,
                region=region,
                elapsed=f"{time.time() - generate_started_at:.2f}s",
                prompt_tokens=prompt_tokens,
                generated_tokens=generated_tokens,
                cuda_after=cuda_after,
            )
            return self.clean_output(output_text)

    @staticmethod
    def clean_output(text: str) -> str:
        text = SPECIAL_TOKEN_RE.sub("", text)
        text = text.replace("<|user|>", "").replace("<|assistant|>", "")
        return text.strip()


runtime = ModelRuntime(MODEL_ID)
app = FastAPI(title="GLM OCR Local Server")


def load_image_from_url(raw: str) -> Image.Image:
    if raw.startswith("data:"):
        _, encoded = raw.split(",", 1)
        image_bytes = base64.b64decode(encoded)
        return Image.open(io.BytesIO(image_bytes)).convert("RGB")

    if raw.startswith("file://"):
        parsed = urlparse(raw)
        path = unquote(parsed.path or "")
        if path.startswith("/") and len(path) >= 3 and path[2] == ":":
            path = path[1:]
        return Image.open(path).convert("RGB")

    if raw.startswith(("http://", "https://")):
        response = requests.get(raw, timeout=60)
        response.raise_for_status()
        return Image.open(io.BytesIO(response.content)).convert("RGB")

    return Image.open(raw).convert("RGB")


def convert_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message.content, str):
            converted.append(
                {
                    "role": message.role,
                    "content": [{"type": "text", "text": message.content}],
                }
            )
            continue

        parts = []
        for item in message.content:
            if item.type == "text":
                parts.append({"type": "text", "text": item.text or ""})
                continue
            if item.type == "image_url":
                image_value = item.image_url
                if isinstance(image_value, dict):
                    url = image_value.get("url")
                else:
                    url = image_value
                if not url:
                    continue
                parts.append({"type": "image", "image": load_image_from_url(str(url))})
        converted.append({"role": message.role, "content": parts})
    return converted


def summarize_converted_messages(messages: list[dict[str, Any]]) -> tuple[int, int, str | None]:
    image_count = 0
    text_count = 0
    first_image_size: str | None = None
    for message in messages:
        for part in message.get("content", []):
            if part.get("type") == "image":
                image_count += 1
                if first_image_size is None:
                    image = part.get("image")
                    if image is not None and hasattr(image, "size"):
                        first_image_size = f"{image.size[0]}x{image.size[1]}"
            elif part.get("type") == "text":
                text_count += 1
    return image_count, text_count, first_image_size


@app.get("/health")
def health() -> dict[str, str]:
    server_log("INFO", "health check", status="ok")
    return {"status": "ok"}


@app.get("/status")
def status() -> dict[str, Any]:
    cuda_available = torch.cuda.is_available()
    gpu_name = torch.cuda.get_device_name(0) if cuda_available else None
    payload = {
        "status": "ok",
        "model_id": runtime.model_id,
        "loaded": runtime.loaded,
        "loading": runtime.loading,
        "device": runtime.device_description,
        "cuda_available": cuda_available,
        "gpu_name": gpu_name,
        "last_load_error": runtime.last_load_error,
    }
    maybe_log_status(
        loaded=payload["loaded"],
        loading=payload["loading"],
        device=payload["device"],
    )
    return payload


@app.post("/warmup")
def warmup() -> dict[str, Any]:
    started_at = time.time()
    started = runtime.ensure_loading_async()
    status = "started" if started else "idle"
    server_log(
        "INFO",
        "warmup request",
        status=status,
        elapsed=f"{time.time() - started_at:.2f}s",
    )
    return {"status": status}


@app.post("/v1/chat/completions")
def chat_completions(request: ChatCompletionRequest) -> dict[str, Any]:
    request_id = next_request_id()
    request_started_at = time.time()
    try:
        converted = convert_messages(request.messages)
        image_count, text_count, first_image_size = summarize_converted_messages(converted)
        server_log(
            "INFO",
            "request start",
            request_id=request_id,
            task_id=request.trace_task_id,
            upstream_request_id=request.trace_request_id,
            page=request.trace_page,
            region=request.trace_region,
            phase=request.trace_stage,
            images=image_count,
            texts=text_count,
            first_image_size=first_image_size,
            max_tokens=request.max_tokens,
            max_tokens_limit=MAX_NEW_TOKENS_LIMIT,
        )
        has_image = any(
            part.get("type") == "image"
            for message in converted
            for part in message.get("content", [])
        )
        if not has_image:
            output_text = "hello"
        else:
            requested_tokens = int(request.max_tokens or 2048)
            effective_tokens = min(requested_tokens, MAX_NEW_TOKENS_LIMIT)
            if effective_tokens < requested_tokens:
                server_log(
                    "WARN",
                    "max_tokens capped",
                    request_id=request_id,
                    task_id=request.trace_task_id,
                    upstream_request_id=request.trace_request_id,
                    requested=requested_tokens,
                    effective=effective_tokens,
                )
            output_text = runtime.generate(
                converted,
                max_new_tokens=effective_tokens,
                request_id=request.trace_request_id or request_id,
                task_id=request.trace_task_id,
                page=request.trace_page,
                region=request.trace_region,
            )
        output_text = ModelRuntime.clean_output(output_text)
        server_log(
            "INFO",
            "request end",
            request_id=request_id,
            task_id=request.trace_task_id,
            upstream_request_id=request.trace_request_id,
            page=request.trace_page,
            region=request.trace_region,
            status="success",
            elapsed=f"{time.time() - request_started_at:.2f}s",
        )
        return {
            "id": "glmocr-local",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": output_text},
                    "finish_reason": "stop",
                }
            ],
        }
    except Exception as exc:
        server_log(
            "ERROR",
            "request end",
            request_id=request_id,
            task_id=request.trace_task_id,
            upstream_request_id=request.trace_request_id,
            page=request.trace_page,
            region=request.trace_region,
            status="failed",
            elapsed=f"{time.time() - request_started_at:.2f}s",
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Local OpenAI-compatible server for zai-org/GLM-OCR")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5002)
    parser.add_argument("--model", default=MODEL_ID)
    args = parser.parse_args()

    runtime.model_id = args.model
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[GLM-OCR Local Server] Starting on http://{args.host}:{args.port}")
    print(f"[GLM-OCR Local Server] Model id: {args.model}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
