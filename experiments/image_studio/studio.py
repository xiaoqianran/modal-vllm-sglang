"""Private image workbench. Web UI and workers share only durable asset IDs."""
import base64
import binascii
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
import httpx
from PIL import Image, ImageOps

MAX_UPLOAD = 12 * 1024 * 1024
MAX_OUTPUT = 64 * 1024 * 1024
MAX_PIXELS = 16_777_216
SIZES = {"16:9": "2048x1152", "1:1": "1536x1536", "9:16": "1152x2048"}
ID = re.compile(r"[0-9a-f]{32}")
ROOT = Path(__file__).resolve().parent


class DiskStore:
    def __init__(self, root):
        self.root = Path(root)

    def write(self, key, value):
        dst = self.root / key
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(uuid.uuid4().hex + ".tmp")
        try:
            tmp.write_bytes(value)
            tmp.replace(dst)
        finally:
            tmp.unlink(missing_ok=True)

    def read(self, key):
        return (self.root / key).read_bytes()


def json_get(store, key):
    return json.loads(store.read(key))


def json_put(store, key, obj):
    store.write(key, json.dumps(obj, ensure_ascii=False).encode())


def asset_path(asset_id):
    if not isinstance(asset_id, str) or not ID.fullmatch(asset_id):
        raise ValueError("无效的图片编号")
    return f"assets/{asset_id}"


def inspect_image(blob, *, normalize=False):
    with Image.open(io.BytesIO(blob)) as image:
        if image.format not in {"PNG", "JPEG", "WEBP"} or getattr(image, "n_frames", 1) != 1:
            raise ValueError("只接受单张 PNG、JPEG 或 WebP")
        w, h = image.size
        if min(w, h) < 32 or w * h > MAX_PIXELS or max(w, h) > 8192:
            raise ValueError("图片尺寸不在允许范围内")
        image.load()
        if normalize:
            upright = ImageOps.exif_transpose(image)
            # Remove private EXIF/GPS and canonicalize to a format the editor accepts.
            output = io.BytesIO()
            mode = "RGBA" if "A" in upright.getbands() or "transparency" in upright.info else "RGB"
            clean = Image.new(mode, upright.size)
            clean.paste(upright.convert(mode))
            clean.save(output, format="PNG")
            return output.getvalue(), "image/png", *clean.size
        return blob, Image.MIME[image.format], w, h


def save_asset(store, data, *, parent=None, prompt="", normalize=False):
    blob, mime, w, h = inspect_image(data, normalize=normalize)
    if len(blob) > MAX_OUTPUT:
        raise ValueError("图片编码后过大")
    asset_id = uuid.uuid4().hex
    prefix = asset_path(asset_id)
    meta = {"id": asset_id, "parent_id": parent, "prompt": prompt, "mime": mime,
            "width": w, "height": h, "sha256": hashlib.sha256(blob).hexdigest()}
    store.write(prefix + "/image", blob)
    json_put(store, prefix + "/meta.json", meta)  # published last
    return meta


class H3:
    def __init__(self, endpoint, headers=None, transport=None):
        if not endpoint:
            raise ValueError("IMAGE_STUDIO_H3_URL is required")
        self.endpoint = endpoint.rstrip("/")
        self.headers = headers or {}
        self.transport = transport

    def render(self, job, source=None):
        payload = {"prompt": job["prompt"], "size": job["size"], "seed": job["seed"],
                   "num_inference_steps": 6, "generator_device": "cpu",
                   "response_format": "b64_json"}
        with httpx.Client(headers=self.headers, timeout=httpx.Timeout(720, connect=15),
                          transport=self.transport) as client:
            if source is None:
                reply = client.post(self.endpoint + "/v1/images/generations", json=payload)
            else:
                blob, mime = source
                ext = {"image/webp": "webp", "image/jpeg": "jpg", "image/png": "png"}[mime]
                payload["prompt"] = "<Picture 1> " + payload["prompt"]
                reply = client.post(self.endpoint + "/v1/images/edits", data=payload,
                                    files={"image": (f"source.{ext}", blob, mime)})
            reply.raise_for_status()
        try:
            encoded = reply.json()["data"][0]["b64_json"]
            if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_OUTPUT + 2) // 3):
                raise ValueError("Invalid encoded image size")
            return base64.b64decode(encoded, validate=True)
        except (KeyError, IndexError, TypeError, binascii.Error) as exc:
            raise ValueError("Invalid image service response") from exc


def run_job(store, engine, job):
    job_id, parent = job["id"], job["parent_id"]
    key = f"jobs/{job_id}.json"
    if time.time() - job["submitted_at"] > 1800:
        json_put(store, key, {**job, "status": "failed", "error": "排队超时，未开始推理。"})
        return
    json_put(store, key, {**job, "status": "rendering"})
    try:
        source = None
        if parent:
            prefix = asset_path(parent)
            meta = json_get(store, prefix + "/meta.json")
            source = (store.read(prefix + "/image"), meta["mime"])
        output = engine.render(job, source)
        result = save_asset(store, output, parent=parent, prompt=job["prompt"])
    except Exception as exc:
        # Keep engine/endpoint/credential details out of the browser response.
        print(f"Image Studio {job_id}: {type(exc).__name__}", flush=True)
        json_put(store, key, {**job, "status": "failed", "error": "推理失败，原图未改动。"})
        return
    json_put(store, key, {**job, "status": "ready", "asset": result})


def create_app(store, submit, password):
    if not password:
        raise ValueError("IMAGE_STUDIO_PASSWORD is required")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    expected = "Basic " + base64.b64encode(f"studio:{password}".encode()).decode()

    @app.middleware("http")
    async def private(request, call_next):
        value = request.headers.get("Authorization", "")
        if not secrets.compare_digest(value.encode(), expected.encode()):
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Image Studio"'})
        if request.method not in {"GET", "HEAD"} and request.headers.get("X-Studio-Request") != "1":
            return JSONResponse({"detail": "拒绝非工作台请求"}, status_code=403)
        result = await call_next(request)
        result.headers.update({"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
                               "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"})
        return result

    @app.get("/")
    def index():
        return FileResponse(ROOT / "index.html")

    def get_asset(key):
        try:
            prefix = asset_path(key)
            return prefix, json_get(store, prefix + "/meta.json")
        except (ValueError, FileNotFoundError):
            raise HTTPException(404, "找不到图片") from None

    @app.get("/assets/{asset_id}")
    def asset(asset_id: str):
        prefix, meta = get_asset(asset_id)
        return Response(store.read(prefix + "/image"), media_type=meta["mime"])

    @app.post("/uploads", status_code=201)
    async def upload(request: Request):
        parts = bytearray()
        async for chunk in request.stream():
            if len(parts) + len(chunk) > MAX_UPLOAD:
                raise HTTPException(413, "上传不能超过 12 MiB")
            parts.extend(chunk)
        try:
            return await run_in_threadpool(save_asset, store, bytes(parts), normalize=True)
        except (ValueError, OSError, Image.DecompressionBombError):
            raise HTTPException(400, "请选择有效的单张 PNG、JPEG 或 WebP") from None

    def queue_job(body):
        prompt = body.get("prompt")
        mode = body.get("operation")
        parent = body.get("parent_id")
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 3000:
            raise HTTPException(400, "请输入 1–3000 字的要求")
        if mode == "edit":
            _, source = get_asset(parent)
            size = f'{source["width"]}x{source["height"]}'
        elif mode == "generate" and parent is None:
            aspect = body.get("aspect")
            if not isinstance(aspect, str) or aspect not in SIZES:
                raise HTTPException(400, "不支持的画幅")
            size = SIZES[aspect]
        else:
            raise HTTPException(400, "请选择生成或编辑")
        job = {"id": uuid.uuid4().hex, "parent_id": parent, "prompt": prompt.strip(),
               "size": size, "seed": secrets.randbits(32), "status": "queued",
               "submitted_at": time.time()}
        json_put(store, f'jobs/{job["id"]}.json', job)
        try:
            submit(job)
        except Exception:
            # A dispatch acknowledgement can be lost; leave the durable job
            # visible without silently issuing a second GPU request.
            return {**job, "status": "dispatch_unknown"}
        return job

    @app.post("/jobs", status_code=202)
    async def create_job(request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 32768:
                raise HTTPException(413, "请求过大")
            raw.extend(chunk)
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "任务必须是有效的 JSON 对象") from None
        return await run_in_threadpool(queue_job, body)

    @app.get("/jobs/{job_id}")
    def status(job_id: str):
        if not ID.fullmatch(job_id):
            raise HTTPException(404, "找不到任务")
        try:
            job = json_get(store, f"jobs/{job_id}.json")
        except FileNotFoundError:
            raise HTTPException(404, "找不到任务") from None
        if job["status"] in {"queued", "rendering"} and time.time() - job["submitted_at"] > 1800:
            return {**job, "status": "stalled", "error": "任务超时，原图未改动。"}
        return job

    return app


def local_app():
    store = DiskStore(os.environ.get("IMAGE_STUDIO_ROOT", ".runtime/image-studio"))
    engine = H3(os.environ.get("IMAGE_STUDIO_H3_URL"), endpoint_headers())
    executor = ThreadPoolExecutor(max_workers=1)
    return create_app(store, lambda job: executor.submit(run_job, store, engine, job),
                      os.environ.get("IMAGE_STUDIO_PASSWORD"))


def endpoint_headers():
    return {header: os.environ[var] for header, var in
            (("Modal-Key", "IMAGE_STUDIO_H3_MODAL_KEY"), ("Modal-Secret", "IMAGE_STUDIO_H3_MODAL_SECRET"))
            if os.environ.get(var)}
