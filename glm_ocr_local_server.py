import argparse
import base64
import io
import os
import re
import threading
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
SPECIAL_TOKEN_RE = re.compile(r"<\|[^|]+\|>")

os.environ.setdefault("HF_HOME", str(CACHE_DIR))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "1")
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")


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


class ModelRuntime:
    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        self.lock = threading.Lock()
        self.processor = None
        self.model = None
        self.device_description = "unknown"
        self.loaded = False
        self.loading = False
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

    def generate(self, messages: list[dict[str, Any]], max_new_tokens: int) -> str:
        self.load()
        assert self.processor is not None
        assert self.model is not None
        with self.lock:
            inputs = self.processor.apply_chat_template(
                messages,
                tokenize=True,
                add_generation_prompt=True,
                return_dict=True,
                return_tensors="pt",
            ).to(self.model.device)
            inputs.pop("token_type_ids", None)
            generated_ids = self.model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
            )
            output_text = self.processor.decode(
                generated_ids[0][inputs["input_ids"].shape[1] :],
                skip_special_tokens=True,
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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/status")
def status() -> dict[str, Any]:
    cuda_available = torch.cuda.is_available()
    gpu_name = torch.cuda.get_device_name(0) if cuda_available else None
    return {
        "status": "ok",
        "model_id": runtime.model_id,
        "loaded": runtime.loaded,
        "loading": runtime.loading,
        "device": runtime.device_description,
        "cuda_available": cuda_available,
        "gpu_name": gpu_name,
    }


@app.post("/warmup")
def warmup() -> dict[str, Any]:
    started = runtime.ensure_loading_async()
    return {"status": "started" if started else "idle"}


@app.post("/v1/chat/completions")
def chat_completions(request: ChatCompletionRequest) -> dict[str, Any]:
    try:
        converted = convert_messages(request.messages)
        has_image = any(
            part.get("type") == "image"
            for message in converted
            for part in message.get("content", [])
        )
        if not has_image:
            output_text = "hello"
        else:
            output_text = runtime.generate(
                converted,
                max_new_tokens=int(request.max_tokens or 2048),
            )
        output_text = ModelRuntime.clean_output(output_text)
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
