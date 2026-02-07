import asyncio
import json
import logging
import os
import secrets
import shutil
import subprocess
import uuid
from contextlib import asynccontextmanager
from typing import Literal, Optional

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Query, Security, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, HttpUrl

from core.config import settings
from core.job_store import cleanup_old_jobs, create_job, get_job
from services.auth_service import exchange_code, get_auth_url, is_drive_authorized
from services.orchestrator import manual_cut_pipeline, process_video_pipeline

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(api_key: str = Security(_api_key_header)) -> str:
    if not api_key or not secrets.compare_digest(api_key, settings.api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return api_key


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------
class ProcessingOptions(BaseModel):
    layout: Literal["blur_zoom", "vertical", "horizontal", "blur"] = Field(
        "blur_zoom",
        description="Video layout preset: blur_zoom (default), vertical, horizontal, blur",
    )
    max_clips: int = Field(
        1, ge=1, le=10,
        description="Number of cortes to generate (requires video > 10 min for multi-clip)",
    )
    zoom_level: int = Field(
        1400, ge=500, le=3000,
        description="Foreground zoom scale width in pixels (default 1400)",
    )
    fade_duration: float = Field(
        1.0, ge=0.0, le=5.0,
        description="Crossfade duration in seconds between segments (default 1.0)",
    )
    width: int = Field(
        1080, ge=360, le=3840,
        description="Output video width in pixels (default 1080)",
    )
    height: int = Field(
        1920, ge=360, le=3840,
        description="Output video height in pixels (default 1920)",
    )
    mirror: bool = Field(
        False,
        description="Horizontal flip (mirror) the video — useful for copyright avoidance",
    )


class ProcessRequest(BaseModel):
    file_id: str = Field(..., description="Google Drive file ID of the source video")
    webhook_url: HttpUrl = Field(
        ..., description="URL to POST results to upon completion"
    )
    drive_folder_id: Optional[str] = Field(
        None,
        description="Google Drive folder ID for uploads. If omitted, uploads to Drive root.",
    )
    gemini_prompt_instruction: Optional[str] = Field(
        None,
        description="Optional custom instruction appended to the Gemini analysis prompt",
    )
    options: Optional[ProcessingOptions] = Field(
        None,
        description="Optional processing options. If omitted, defaults are used.",
    )


class ProcessAcceptedResponse(BaseModel):
    job_id: str
    status: str = "accepted"
    message: str


class ManualClip(BaseModel):
    start: float = Field(..., ge=0, description="Start time in seconds")
    end: float = Field(..., gt=0, description="End time in seconds")
    title: Optional[str] = Field(None, description="Optional title for this clip")


class ManualCutRequest(BaseModel):
    file_id: str = Field(..., description="Google Drive file ID of the source video")
    webhook_url: HttpUrl = Field(
        ..., description="URL to POST results to upon completion"
    )
    drive_folder_id: Optional[str] = Field(
        None,
        description="Google Drive folder ID for uploads. If omitted, uploads to Drive root.",
    )
    clips: list[ManualClip] = Field(
        ..., min_length=1, max_length=20,
        description="Array of clips with start/end timestamps (seconds)",
    )
    options: Optional[ProcessingOptions] = Field(
        None,
        description="Optional processing options (layout, mirror, etc). If omitted, defaults are used.",
    )


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown: shared httpx client + validation checks."""
    os.makedirs(settings.temp_dir, exist_ok=True)

    # Cleanup leftover temp dirs from previous crashes/restarts
    for entry in os.listdir(settings.temp_dir):
        entry_path = os.path.join(settings.temp_dir, entry)
        if os.path.isdir(entry_path) and entry.startswith("job-"):
            try:
                shutil.rmtree(entry_path)
                logger.info(f"Cleaned up leftover temp dir: {entry_path}")
            except Exception as e:
                logger.warning(f"Failed to clean up {entry_path}: {e}")

    result = subprocess.run(
        [settings.ffmpeg_path, "-version"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error("FFmpeg not found or not working")
        raise RuntimeError("FFmpeg is not available")
    logger.info("FFmpeg check passed")

    if not os.path.exists(settings.google_drive_token_json):
        logger.warning(
            f"Drive token not found: {settings.google_drive_token_json}. "
            f"Authorize via browser: {settings.app_base_url}/auth/drive?key=YOUR_API_KEY "
            f"or run 'python scripts/auth_drive.py' locally."
        )

    app.state.http_client = httpx.AsyncClient(timeout=settings.webhook_timeout)

    # Periodic cleanup of expired jobs (every hour)
    async def _job_cleanup_loop():
        while True:
            await asyncio.sleep(3600)
            try:
                cleanup_old_jobs()
            except Exception as e:
                logger.warning(f"Job cleanup error: {e}")

    cleanup_task = asyncio.create_task(_job_cleanup_loop())
    logger.info("Application started")

    yield

    cleanup_task.cancel()
    await app.state.http_client.aclose()
    logger.info("Application shut down")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title=settings.app_title,
    version=settings.app_version,
    lifespan=lifespan,
)


@app.get("/", response_model=HealthResponse)
async def health_check():
    return HealthResponse(status="ok", version=settings.app_version)


@app.post("/v1/process", response_model=ProcessAcceptedResponse, status_code=202)
async def process_video(
    request: ProcessRequest,
    background_tasks: BackgroundTasks,
    _key: str = Depends(verify_api_key),
):
    if not request.file_id.strip():
        raise HTTPException(status_code=422, detail="file_id cannot be empty")

    # --- Pre-flight checks: fail fast before accepting the job ---
    if not settings.gemini_api_key:
        raise HTTPException(
            status_code=503,
            detail="Servico indisponivel: chave da API Gemini nao configurada (GEMINI_API_KEY).",
        )

    if not os.path.exists(settings.google_drive_token_json):
        auth_url = f"{settings.app_base_url.rstrip('/')}/auth/drive?key=YOUR_API_KEY"
        raise HTTPException(
            status_code=503,
            detail="Servico indisponivel: Google Drive nao autorizado. "
            f"Envie o client_secret.json via POST /v1/upload-credentials "
            f"e autorize em {auth_url}",
        )

    job_id = str(uuid.uuid4())

    # Register job in the store
    create_job(job_id=job_id, file_id=request.file_id, webhook_url=str(request.webhook_url))

    # Use provided options or defaults
    opts = request.options or ProcessingOptions()

    background_tasks.add_task(
        process_video_pipeline,
        job_id=job_id,
        file_id=request.file_id,
        webhook_url=str(request.webhook_url),
        gemini_prompt_instruction=request.gemini_prompt_instruction,
        drive_folder_id=request.drive_folder_id,
        options=opts,
        http_client=app.state.http_client,
    )

    return ProcessAcceptedResponse(
        job_id=job_id,
        message=f"Video processing started. Results will be sent to {request.webhook_url}",
    )


@app.get("/v1/status/{job_id}")
async def get_job_status(job_id: str, _key: str = Depends(verify_api_key)):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


@app.post("/v1/manual-cut", response_model=ProcessAcceptedResponse, status_code=202)
async def manual_cut(
    request: ManualCutRequest,
    background_tasks: BackgroundTasks,
    _key: str = Depends(verify_api_key),
):
    """Manual video cutting — no AI analysis, user provides exact timestamps."""
    if not request.file_id.strip():
        raise HTTPException(status_code=422, detail="file_id cannot be empty")

    # Validate clip timestamps
    for i, clip in enumerate(request.clips):
        if clip.end <= clip.start:
            raise HTTPException(
                status_code=422,
                detail=f"Clip {i + 1}: end ({clip.end}) must be greater than start ({clip.start})",
            )

    # Pre-flight: Drive must be authorized
    if not os.path.exists(settings.google_drive_token_json):
        auth_url = f"{settings.app_base_url.rstrip('/')}/auth/drive?key=YOUR_API_KEY"
        raise HTTPException(
            status_code=503,
            detail="Servico indisponivel: Google Drive nao autorizado. "
            f"Envie o client_secret.json via POST /v1/upload-credentials "
            f"e autorize em {auth_url}",
        )

    job_id = str(uuid.uuid4())
    create_job(job_id=job_id, file_id=request.file_id, webhook_url=str(request.webhook_url))

    opts = request.options or ProcessingOptions()

    background_tasks.add_task(
        manual_cut_pipeline,
        job_id=job_id,
        file_id=request.file_id,
        webhook_url=str(request.webhook_url),
        clips=[{"start": c.start, "end": c.end, "title": c.title} for c in request.clips],
        drive_folder_id=request.drive_folder_id,
        options=opts,
        http_client=app.state.http_client,
    )

    return ProcessAcceptedResponse(
        job_id=job_id,
        message=f"Manual cut started ({len(request.clips)} clips). Results will be sent to {request.webhook_url}",
    )


# ---------------------------------------------------------------------------
# Google Drive credentials upload (for panels without volume access)
# ---------------------------------------------------------------------------
@app.post("/v1/upload-credentials")
async def upload_credentials(
    file: UploadFile = File(...),
    _key: str = Depends(verify_api_key),
):
    """Upload Google OAuth client_secret.json via API (for panel deployments)."""
    # Read and validate JSON content
    content = await file.read()
    if len(content) > 50_000:  # client_secret.json is typically ~1 KB
        raise HTTPException(status_code=400, detail="File too large. Expected a small JSON file.")

    try:
        data = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON file.")

    # Validate it looks like a Google OAuth client secret
    valid_keys = {"installed", "web"}
    if not any(k in data for k in valid_keys):
        raise HTTPException(
            status_code=400,
            detail="Invalid client_secret.json. Expected 'installed' or 'web' key. "
            "Download the correct file from Google Cloud Console > Credentials > OAuth 2.0 Client IDs.",
        )

    # Save to credentials directory with the correct name
    dest = settings.google_drive_client_secret_json
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(content)

    logger.info(f"Client secret uploaded successfully to {dest}")
    return {
        "status": "ok",
        "message": "client_secret.json saved successfully. "
        f"Now authorize Google Drive at: {settings.app_base_url.rstrip('/')}/auth/drive?key=YOUR_API_KEY",
    }


# ---------------------------------------------------------------------------
# Google Drive OAuth (web-based, for panels without terminal)
# ---------------------------------------------------------------------------
@app.get("/auth/drive", response_class=HTMLResponse)
async def auth_drive_page(key: str = Query(None)):
    """Web page to start Google Drive authorization. Requires API key as query param."""
    if not key or not secrets.compare_digest(key, settings.api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key (?key=YOUR_KEY)")

    if is_drive_authorized():
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;text-align:center;padding:60px'>"
            "<h1>Google Drive Already Authorized</h1>"
            "<p>The token.json is valid. You can use the API.</p>"
            "<p><a href='/health'>Health Check</a></p>"
            "</body></html>"
        )

    try:
        auth_url = get_auth_url()
    except FileNotFoundError as e:
        upload_url = f"{settings.app_base_url.rstrip('/')}/v1/upload-credentials"
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;text-align:center;padding:60px'>"
            f"<h1>Setup Required</h1>"
            f"<p>{e}</p>"
            "<p>Download <code>client_secret.json</code> from "
            "<a href='https://console.cloud.google.com/apis/credentials' target='_blank'>"
            "Google Cloud Console</a> and upload it via API:</p>"
            f"<pre>curl -X POST {upload_url} \\\n"
            "  -H \"X-API-Key: YOUR_API_KEY\" \\\n"
            "  -F \"file=@client_secret.json\"</pre>"
            "<h3>Important</h3>"
            "<p>When creating the OAuth client, choose <b>Web application</b> type "
            f"and add this redirect URI:</p>"
            f"<code>{settings.app_base_url.rstrip('/')}/auth/drive/callback</code>"
            "</body></html>",
            status_code=400,
        )

    return RedirectResponse(url=auth_url)


@app.get("/auth/drive/callback")
async def auth_drive_callback(code: str = Query(None), error: str = Query(None)):
    """OAuth2 callback — Google redirects here after user authorizes."""
    if error:
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;text-align:center;padding:60px'>"
            f"<h1>Authorization Failed</h1>"
            f"<p>Google returned an error: {error}</p>"
            "<p><a href='javascript:history.back()'>Try again</a></p>"
            "</body></html>",
            status_code=400,
        )

    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    try:
        result = exchange_code(code)
    except Exception as e:
        logger.exception("OAuth code exchange failed")
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;text-align:center;padding:60px'>"
            f"<h1>Authorization Failed</h1>"
            f"<p>{e}</p>"
            "<p>Make sure the redirect URI in Google Cloud Console matches:<br>"
            f"<code>{settings.app_base_url.rstrip('/')}/auth/drive/callback</code></p>"
            "</body></html>",
            status_code=500,
        )

    return HTMLResponse(
        "<html><body style='font-family:sans-serif;text-align:center;padding:60px'>"
        "<h1>Google Drive Authorized!</h1>"
        "<p>Token saved successfully. The API is now connected to your Google Drive.</p>"
        "<p>You can close this page.</p>"
        "</body></html>"
    )
