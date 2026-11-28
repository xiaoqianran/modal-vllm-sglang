"""Private image workbench. Web UI and workers share only durable asset IDs."""
import base64
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


class SDKH3:
    """Production SDK; private assets stay on Modal, never the public HF bucket."""
    def __init__(self, backend=None):
        from orchestration.sdk import Backend
        self.backend = backend or Backend()

    def render_asset(self, store, job):
        from orchestration.sdk import model_specs, cloud_artifact
        from orchestration.deployment_state import model_source_version
        from runtime.serving.deployment_probe import probe_name
        from runtime.serving.preview_batch import stage_bytes
        from runtime.tasks.task_spec import prepare, result_prefix, execution_signature
        from runtime.tasks.task_identity import intent_digest
        from runtime.tasks.artifacts import read_image
        import math
        key = f"jobs/{job['id']}.json"
        current = json_get(store, key)
        record = current.get("gpu")
        if record is None:
            spec = model_specs()["minimax"]
            fingerprint = self.backend.fingerprint(spec)
            version = intent_digest([model_source_version(spec), spec, fingerprint])
            if self.backend.modal.Function.from_name(spec["app_name"], probe_name(version)).remote() != version:
                raise RuntimeError("H3 deployment version mismatch")
            width, height = map(int, job["size"].split("x"))
            scale = max(1.0, math.sqrt(1048576 / (width * height)))
            width, height = (math.ceil(x * scale / 32) * 32 for x in (width, height))
            if max(width, height) > 8192 or width * height > MAX_PIXELS:
                raise ValueError("Unsupported edit canvas")
            operation = "i2i" if job["parent_id"] else "t2i"
            item = {"id": job["id"], "operation": operation, "prompt": job["prompt"],
                    "seed": job["seed"], "params": {"size": f"{width}x{height}"}}
            if job["parent_id"]:
                parent = asset_path(job["parent_id"])
                meta = json_get(store, parent + "/meta.json")
                ref = meta.get("artifact")
                if ref is None:
                    # Old private uploads are staged once on CPU. No HF publication.
                    blob = store.read(parent + "/image")
                    ref = stage_bytes(self.backend.volume,
                                      {"path": "workflow-inputs/" + meta["sha256"],
                                       "sha256": meta["sha256"], "size": len(blob)}, blob)
                item["image_artifact"] = ref
            _, signature = prepare(spec, item)
            prefix = result_prefix(spec, "private-studio", "001-main", operation)
            record = {"id": item["id"], "prefix": prefix, "intent_hash": signature,
                      "execution_hash": execution_signature(signature, fingerprint),
                      "cache_fingerprint": fingerprint, "status": "uncertain"}
            # Keep evidence before admission; on crash never blindly replay.
            json_put(store, key, {**current, "gpu": record})
            existing = self.backend.preflight_result(record)
            if existing is not None:
                if existing["status"] != "completed":
                    raise RuntimeError("Previous GPU submission is unresolved")
            else:
                record["call_id"] = self.backend.submit(spec, item, prefix, fingerprint,
                                                        deployment_version=version)
                record["status"] = "queued"
                json_put(store, key, {**current, "gpu": record})
        deadline = time.monotonic() + 1300
        while True:
            result = self.backend.poll(record)
            if result is not None:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("Studio GPU call is unresolved")
            time.sleep(.5)
        if result["status"] != "completed":
            raise RuntimeError("Studio GPU call failed")
        artifact = cloud_artifact({**record, "result": result}, "image")
        ref = {k: artifact[k] for k in ("path", "sha256", "size")}
        # Verify dimensions and bytes on CPU, then store metadata only.
        blob = read_image(ref, self.backend.volume)
        _, mime, width, height = inspect_image(blob)
        meta = {"id": job["id"], "parent_id": job["parent_id"], "prompt": job["prompt"],
                "mime": mime, "width": width, "height": height, "sha256": ref["sha256"],
                "artifact": ref}
        json_put(store, asset_path(meta["id"]) + "/meta.json", meta)
        return meta


def run_job(store, engine, job):
    job_id, parent = job["id"], job["parent_id"]
    key = f"jobs/{job_id}.json"
    current = json_get(store, key)
    if current.get("status") == "ready":
        return
    if not current.get("gpu") and time.time() - job["submitted_at"] > 1800:
        json_put(store, key, {**current, "status": "failed", "error": "排队超时，未开始推理。"})
        return
    json_put(store, key, {**current, "status": "rendering"})
    try:
        result = engine.render_asset(store, job)
    except Exception as exc:
        # Keep engine/endpoint/credential details out of the browser response.
        print(f"Image Studio {job_id}: {type(exc).__name__}", flush=True)
        json_put(store, key, {**json_get(store, key), "status": "failed", "error": "推理失败，原图未改动。"})
        return
    json_put(store, key, {**json_get(store, key), "status": "ready", "asset": result})


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
        if meta.get("artifact"):
            from runtime.serving.modal_setup import model_volume
            from runtime.tasks.artifacts import read_image
            return Response(read_image(meta["artifact"], model_volume()), media_type=meta["mime"])
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
    engine = SDKH3()
    executor = ThreadPoolExecutor(max_workers=1)
    return create_app(store, lambda job: executor.submit(run_job, store, engine, job),
                      os.environ.get("IMAGE_STUDIO_PASSWORD"))
