import asyncio
import logging
import os
import re
import shutil
import time
import uuid
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional
import cv2
import magic
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import Counter, Histogram
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from .api_call import HFRemoteColorizer
from dotenv import load_dotenv
load_dotenv()

# ── configure logging ───────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── constants & paths ───────────────────────────────────────────────────────
CODE_DIR = Path(__file__).resolve().parent
APP_DATA = Path("./app_data")
TEMP_DIR = APP_DATA / "tmp_sessions"
RESULTS_DIR = APP_DATA / "colorizedImages"
STATIC_DIR = Path(__file__).resolve().parent.parent / "frontend"
RL_ALLOWED = Counter(
    "ratelimit_allowed_total",
    "Count of requests allowed by the rate limiter",
    ["scope"],  # e.g. "upload_check", "predict_bin"
)
RL_BLOCKED = Counter(
    "ratelimit_blocked_total",
    "Count of requests blocked by the rate limiter",
    ["scope"],
)

COLORIZE_SECONDS = Histogram(
    "colorize_seconds",
    "Time spent colorizing one image",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10)
)
MAX_FILES_PER_SESSION = 25
RATE_LIMIT_WINDOW = 60
MAX_UPLOADS_PER_MIN = 5
MAX_UPLOAD_BYTES = 1 * 1024 * 1024   
CHUNK_SIZE = 64 * 1024

for d in (APP_DATA, TEMP_DIR, RESULTS_DIR, STATIC_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ── async redis  ───────────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL")

if REDIS_URL:
    try:
        from redis import asyncio as aioredis
        _redis = aioredis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=2,
            health_check_interval=30,
            retry_on_timeout=True,
        )
        logger.info("Redis rate limiting enabled")
    except Exception as e:
        logger.warning(f"Redis init failed, falling back to in-memory rate limit: {e}")
        _redis = None
else:
    logger.info("REDIS_URL not set; rate limiter will fall back to in-memory")

# ── global state ───────────────────────────────────────────────────────────
ALLOWED_MIME_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp"}
upload_history: Dict[str, List[float]] = {}
api_url=os.getenv("API_URL")
colorizer = HFRemoteColorizer(api_url=api_url)

# Threadpool sized to machine: up to 5x cores, capped at 32
MAX_WORKERS = min(32, (os.cpu_count() or 1) * 5)
sem=asyncio.Semaphore(MAX_WORKERS)
executor: Optional[ThreadPoolExecutor] = None



class UploadCheckPayload(BaseModel):
    currentFileCount: int
    newFileCount: int
    totalFileCount: int
    sessionToken: str
    fingerprint: str

class ProcessingResult(BaseModel):
    success: bool
    message: str
    session_token: str
    processed_count: int
    colorized_images: List[str] = []

RL_LUA = """
local inc  = tonumber(ARGV[1])
local mttl = tonumber(ARGV[2])
local dttl = tonumber(ARGV[3])

local m = redis.call('INCRBY', KEYS[1], inc)
if m == inc then
  redis.call('EXPIRE', KEYS[1], mttl)
end

local d = redis.call('INCRBY', KEYS[2], inc)
if d == inc then
  redis.call('EXPIRE', KEYS[2], dttl)
end

return {m, d}
"""
_RL_LUA_SHA = None

async def _ensure_rl_script_loaded():
    global _RL_LUA_SHA
    if _redis and not _RL_LUA_SHA:
        _RL_LUA_SHA = await _redis.script_load(RL_LUA)

def real_client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    ip = (xff.split(",")[0].strip() if xff else (request.client.host or "unknown"))
    if ip.startswith("::ffff:"):  # IPv6-mapped IPv4
        ip = ip.split(":")[-1]
    return ip

def make_rate_key(request: Request, fingerprint: Optional[str] = None) -> str:
    ip = real_client_ip(request)
    fp = fingerprint or request.headers.get("x-client-fingerprint") or ""
    if fp:
        fp_hash = hashlib.sha1(fp.encode()).hexdigest()[:8]
        return f"{ip}:{fp_hash}"
    return ip

async def check_limits(r, key: str, inc: int = 1):
    if not r:
        raise HTTPException(503, "Rate limiter unavailable")

    now = time.time()
    minute_bucket = int(now // RATE_LIMIT_WINDOW)  # 60s windows
    day_bucket    = int(now // 86400)              # daily windows

    mkey = f"rl:{key}:m:{minute_bucket}"
    dkey = f"rl:{key}:d:{day_bucket}"

    mttl = RATE_LIMIT_WINDOW * 2       # keep 2 windows (e.g., 120s)
    dttl = 86400 + 600                 # 1 day + buffer

    # ensure script is loaded
    await _ensure_rl_script_loaded()

    try:
        res = await r.evalsha(_RL_LUA_SHA, 2, mkey, dkey, inc, mttl, dttl)
    except Exception:       
        res = await r.eval(RL_LUA, 2, mkey, dkey, inc, mttl, dttl)
        try:
            
            globals()["_RL_LUA_SHA"] = await r.script_load(RL_LUA)
        except Exception:
            pass

    mcount = int(res[0])
    dcount = int(res[1])

    if mcount > MAX_UPLOADS_PER_MIN:
        retry = str(RATE_LIMIT_WINDOW - int(now % RATE_LIMIT_WINDOW))
        raise HTTPException(
            429, "Rate limit exceeded. Try again in a minute.",
            headers={"Retry-After": retry}
        )
    if dcount > MAX_FILES_PER_SESSION:
        retry = str(86400 - int(now % 86400))
        raise HTTPException(
            429, "Daily quota reached. Try again tomorrow.",
            headers={"Retry-After": retry}
        )

def verify_image_type(upload_file: UploadFile):
    sample = upload_file.file.read(2048)
    mime = magic.from_buffer(sample, mime=True)
    upload_file.file.seek(0)
    if mime not in ALLOWED_MIME_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {mime}")
    
def create_temp_dir(session_token: str) -> Path:
    temp_dir = TEMP_DIR / session_token
    temp_dir.mkdir(parents=True, exist_ok=True)
    return temp_dir

def create_results_dir(session_token: str) -> Path:
    results_dir = RESULTS_DIR / session_token
    results_dir.mkdir(parents=True, exist_ok=True)
    return results_dir
def real_client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host or "unknown"

async def save_upload_file_async_chunked(upload: UploadFile, dest: Path, limit: int = MAX_UPLOAD_BYTES, chunk_size: int = 256 * 1024):
    loop = asyncio.get_running_loop()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"")
    total = 0
    def _append(dst: Path, data: bytes):
        with dst.open("ab") as f: f.write(data)
    while True:
        data = await upload.read(chunk_size)
        if not data: break
        total += len(data)
        if total > limit:
            while await upload.read(chunk_size): pass
            raise HTTPException(413, f"File too large (>{limit} bytes)")
        await loop.run_in_executor(executor, _append, dest, data)
    await upload.seek(0)

def process_image_sync(input_path: Path, output_path: Path) -> bool:
    try:
        start=time.perf_counter()
        img = cv2.imread(str(input_path))
        if img is None:
            logger.error(f"Failed to load image: {input_path}")
            return False
        out = colorizer.process(img)
        COLORIZE_SECONDS.observe(time.perf_counter() - start)
        if out is None:
            logger.error(f"Colorizer returned None for: {input_path.name}")
            return False
        ok = cv2.imwrite(str(output_path), out)
        return bool(ok)
    except Exception:
        logger.exception("process_image_sync error")
        return False
async def _read_limited(upload: UploadFile, limit: int) -> bytes:
    """Stream the upload without reading everything at once; enforce size limit."""
    total = 0
    buf = bytearray()
    while True:
        chunk = await upload.read(CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            # drain rest so the client connection is cleanly consumed
            while await upload.read(CHUNK_SIZE):
                pass
            raise HTTPException(status_code=413, detail=f"File too large (>{limit} bytes)")
        buf.extend(chunk)
    return bytes(buf)

async def _colorize_np_bgr(img_bgr: np.ndarray) -> np.ndarray:
    """Offload the blocking HF call to the threadpool."""
    loop = asyncio.get_running_loop()
    def _run():
        out = colorizer.process(img_bgr)  # sync, may block
        if out is None:
            raise RuntimeError("Colorizer returned None")
        return out
    return await loop.run_in_executor(executor, _run)

async def _encode_png(img_bgr: np.ndarray) -> bytes:
    loop = asyncio.get_running_loop()
    def _run():
        ok, buf = cv2.imencode(".png", img_bgr)
        if not ok:
            raise RuntimeError("PNG encode failed")
        return buf.tobytes()
    return await loop.run_in_executor(executor, _run)

# ══════════════════════════════════════════════════════════════════════════════
# API ROUTES
# ══════════════════════════════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    global executor
    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    yield
    executor.shutdown(wait=False)
    try:
        await colorizer.aclose()
    except Exception:
        pass

app = FastAPI(
    title="Image Colorization API",
    version="1.0.0",
    description="A service that colorizes grayscale images using a GAN-based model via a HF api.",
    lifespan=lifespan,
)
Instrumentator().instrument(app).expose(
    app,
    endpoint="/metrics",
    include_in_schema=False,
)
@app.post("/upload/check")
async def upload_check(payload: UploadCheckPayload, request: Request):

    key = make_rate_key(request, payload.fingerprint)

    try:
        await check_limits(_redis, key,inc=payload.newFileCount)
        RL_ALLOWED.labels(scope="upload_check").inc()
    except HTTPException as e:
        if e.status_code == 429:
            RL_BLOCKED.labels(scope="upload_check").inc()
        raise
    return {"allowed": True}

@app.get("/config")
async def get_config():
    return {"backendUrl": os.getenv("BACKEND_URL")}

@app.post("/api/colorize", response_model=ProcessingResult)
async def colorize_images(
    request: Request,
    files: List[UploadFile] = File(...),
    sessionToken: Optional[str] = Form(None),
    fingerprint: Optional[str] = Form(None),
):
    if not files:
        raise HTTPException(400, "No images provided")
    if len(files) > 5:  
        raise HTTPException(413, "Too many files in one request (max 5).")

    key = make_rate_key(request, fingerprint)
    
    try:
        await check_limits(_redis, key,inc=len(files))
        RL_ALLOWED.labels(scope="colorize_batch").inc()
    except HTTPException as e:
        if e.status_code == 429:
            RL_BLOCKED.labels(scope="colorize_batch").inc()
        raise

    sessionToken = sessionToken or str(uuid.uuid4())
    temp_dir = create_temp_dir(sessionToken)
    results_dir = create_results_dir(sessionToken)

    try:
        existing = list(results_dir.glob("colorized_*.png"))
        indices = []
        for p in existing:
            m = re.match(r"colorized_(\d+)\.png$", p.name)
            if m:
                indices.append(int(m.group(1)))
        next_start = max(indices, default=-1) + 1

        input_paths, output_paths = [], []
        for i, upload in enumerate(files):
            verify_image_type(upload)
            ext =  ".png"
            in_path = temp_dir / f"input_{i}{ext}"
            out_index = next_start + i
            out_path = results_dir / f"colorized_{out_index}.png"

            await save_upload_file_async_chunked(upload, in_path, limit=MAX_UPLOAD_BYTES)

            input_paths.append(in_path)
            output_paths.append(out_path)

    
        futures = [
            executor.submit(process_image_sync, in_p, out_p)
            for in_p, out_p in zip(input_paths, output_paths)
        ]

        processed = 0
        result_urls = []
        for fut, out_p in zip(futures, output_paths):
            try:
                success = await asyncio.wrap_future(fut)
                logging.info(f"Processed {out_p.name}: {success}, exists={out_p.exists()}")
                if success and out_p.exists():
                    processed += 1
                    result_urls.append(f"/api/result/{sessionToken}/{out_p.name}")
            except Exception as e:
                logging.error(f"Error during processing {out_p.name}: {e}")

        if processed == 0:
            raise HTTPException(500, "Failed to process any images")

        return ProcessingResult(
            success=True,
            message=f"Successfully processed {processed} of {len(files)} images",
            session_token=sessionToken,
            processed_count=processed,
            colorized_images=result_urls
        )

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Unexpected error in colorize_images: {e}", exc_info=True)
        raise HTTPException(500, f"Processing failed: {e}")
    finally:
       
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/api/result/{session_token}/{filename}")
async def get_result(session_token: str, filename: str):
    """Serve processed image results"""
    result_path = RESULTS_DIR / session_token / filename
    
    if not result_path.exists():
        raise HTTPException(status_code=404, detail="Result not found")
    
    return FileResponse(result_path, media_type="image/png")

@app.get("/api/results/{session_token}")
async def list_results(session_token: str):
    """List all results for a session"""
    session_dir = RESULTS_DIR / session_token
    
    if not session_dir.exists():
        raise HTTPException(status_code=404, detail="Session not found")
    
    results = []
    for file_path in session_dir.glob("*.png"):
        results.append({
            "filename": file_path.name,
            "url": f"/api/result/{session_token}/{file_path.name}",
            "size": file_path.stat().st_size,
            "created": file_path.stat().st_mtime
        })
    
    return {"session_token": session_token, "results": results}

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "colorization_available": colorizer is not None,
        "model_loaded": colorizer is not None,
        "max_files_per_session": MAX_FILES_PER_SESSION,
        "redis": bool(_redis),
    }

@app.post("/admin/cleanup")
async def cleanup_old_files(max_age_hours: int = 2):
    now = time.time()
    cutoff = now - max_age_hours * 3600
    cleaned = 0
    for d in RESULTS_DIR.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            cleaned += 1
    # temp dirs
    for d in TEMP_DIR.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            cleaned += 1
    return {"message": f"Cleaned up {cleaned} old sessions"}

@app.post("/api/predict")
async def predict_bin(request: Request, image: UploadFile = File(...)):
    start=time.perf_counter()
    t0=time.time()

    key = make_rate_key(request)
    try:
        await check_limits(_redis, key,inc=1)
        RL_ALLOWED.labels(scope="predict_bin").inc()
    except HTTPException as e:
        if e.status_code == 429:
            RL_BLOCKED.labels(scope="predict_bin").inc()
        raise

    verify_image_type(image)
    raw = await _read_limited(image, MAX_UPLOAD_BYTES)

    arr = np.frombuffer(raw, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "Could not decode image")
    start = time.perf_counter()
    async with sem:  
        out = await _colorize_np_bgr(img)
    COLORIZE_SECONDS.observe(time.perf_counter() - start)
    png_bytes = await _encode_png(out)

    
    dt_ms = int((time.time() - t0) * 1000)
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={
            "Cache-Control": "no-store",
            "X-Process-Time-ms": str(dt_ms),
        },
    )


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)