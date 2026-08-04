import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from fastapi import FastAPI, Header, HTTPException
from fastapi.background import BackgroundTasks
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from yt_dlp import YoutubeDL

app = FastAPI(title="MusicPocket SpotDL Companion", docs_url=None, redoc_url=None)
logger = logging.getLogger("musicpocket.cloud_companion")
conversion_slot = asyncio.Semaphore(max(1, int(os.getenv("SPOTDL_CONCURRENCY", "1"))))
allowed_hosts = ("youtube.com", "youtu.be", "spotify.com")
jobs: dict[str, dict] = {}
job_tasks: set[asyncio.Task] = set()
relay_jobs: dict[str, dict] = {}
local_companion_url: str | None = None
local_companion_seen = 0.0


class ConvertRequest(BaseModel):
    url: str


class RegisterRequest(BaseModel):
    url: str


def proxy_url() -> str | None:
    value = os.getenv("SPOTDL_PROXY_URL", "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("SPOTDL_PROXY_URL must be a valid HTTP or HTTPS proxy URL.")
    return value


def search_youtube(query: str, limit: int) -> list[dict]:
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "playlistend": limit,
        "socket_timeout": 15,
    }
    proxy = proxy_url()
    if proxy:
        options["proxy"] = proxy
    with YoutubeDL(options) as downloader:
        data = downloader.extract_info(f"ytsearch{limit}:{query}", download=False)

    results = []
    for entry in (data or {}).get("entries", []):
        if not entry or not entry.get("id") or not entry.get("title"):
            continue
        video_id = str(entry["id"])
        thumbnails = entry.get("thumbnails") or []
        thumbnail = str(entry.get("thumbnail") or (thumbnails[-1].get("url") if thumbnails else ""))
        duration = entry.get("duration")
        results.append({
            "id": video_id,
            "title": str(entry["title"])[:180],
            "artist": str(entry.get("uploader") or entry.get("channel") or "YouTube")[:120],
            "duration": int(duration) if isinstance(duration, (int, float)) and duration > 0 else None,
            "thumbnail": thumbnail,
            "url": f"https://www.youtube.com/watch?v={video_id}",
        })
    return results


def is_allowed_url(value: str) -> bool:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and any(host == allowed or host.endswith(f".{allowed}") for allowed in allowed_hosts)
    )


def authorize(value: str | None) -> None:
    expected = os.getenv("SPOTDL_API_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="The SpotDL service is not configured.")
    if value != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Unauthorized.")


def normalize_companion_url(value: str) -> str:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host.endswith(".trycloudflare.com"):
        raise HTTPException(status_code=400, detail="Use a valid MusicPocket tunnel address.")
    return f"{parsed.scheme}://{parsed.netloc}"


def active_companion_url() -> str | None:
    if local_companion_url and time.monotonic() - local_companion_seen < 120:
        return local_companion_url
    return None


def companion_json(
    base_url: str,
    path: str,
    token: str,
    *,
    method: str = "GET",
    body: dict | None = None,
):
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(3):
        request = Request(
            f"{base_url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "MusicPocket-Relay/1.0",
            },
        )
        try:
            with urlopen(request, timeout=35) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            response_body = error.read()
            if error.code in (502, 503, 504) and attempt < 2:
                time.sleep(2)
                continue
            try:
                failure = json.loads(response_body.decode())
                detail = str(failure.get("detail") or "SpotDL could not complete this request.")
            except Exception:
                detail = "SpotDL could not complete this request."
            raise HTTPException(status_code=error.code, detail=detail[:240]) from error
        except Exception as error:
            if attempt < 2:
                time.sleep(2)
                continue
            raise HTTPException(
                status_code=502,
                detail="The Windows companion is offline. Turn on the PC, wait a moment, and try again.",
            ) from error

    raise HTTPException(status_code=502, detail="The Windows companion is offline.")


def open_companion_file(base_url: str, path: str, token: str, timeout: int = 120):
    request = Request(
        f"{base_url}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "audio/mpeg, application/json",
            "User-Agent": "MusicPocket-Relay/1.0",
        },
    )
    try:
        return urlopen(request, timeout=timeout)
    except HTTPError as error:
        body = error.read()
        try:
            failure = json.loads(body.decode())
            detail = str(failure.get("detail") or "SpotDL could not return this track.")
        except Exception:
            detail = "SpotDL could not return this track."
        raise HTTPException(status_code=error.code, detail=detail[:240]) from error
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail="The Windows companion is offline. Turn on the PC, wait a moment, and try again.",
        ) from error


def open_companion_conversion(base_url: str, payload: ConvertRequest, token: str):
    request = Request(
        f"{base_url}/v1/convert",
        data=json.dumps({"url": payload.url}).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "audio/mpeg, application/json",
            "User-Agent": "MusicPocket-Relay/1.0",
        },
    )
    try:
        return urlopen(request, timeout=7 * 60)
    except HTTPError as error:
        body = error.read()
        try:
            failure = json.loads(body.decode())
            detail = str(failure.get("detail") or "SpotDL could not convert this track.")
        except Exception:
            detail = "SpotDL could not convert this track."
        raise HTTPException(status_code=error.code, detail=detail[:240]) from error
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail="The Windows companion is offline. Turn on the PC, wait a moment, and try again.",
        ) from error


def stream_companion(response):
    try:
        while chunk := response.read(64 * 1024):
            yield chunk
    finally:
        response.close()


def companion_audio_response(upstream):
    headers = {"Cache-Control": "no-store"}
    for name in ("Content-Length", "X-MusicPocket-Filename", "X-MusicPocket-Title"):
        value = upstream.headers.get(name)
        if value:
            headers[name] = value
    return StreamingResponse(
        stream_companion(upstream),
        media_type=(upstream.headers.get("Content-Type") or "audio/mpeg").split(";")[0],
        headers=headers,
    )


def resolve_spotdl_query(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host != "youtu.be" and host != "youtube.com" and not host.endswith(".youtube.com"):
        return url

    endpoint = "https://www.youtube.com/oembed?" + urlencode({"url": url, "format": "json"})
    request = Request(endpoint, headers={"User-Agent": "MusicPocket/1.0"})
    try:
        proxy = proxy_url()
        if proxy:
            opener = build_opener(ProxyHandler({"http": proxy, "https": proxy}))
            response_context = opener.open(request, timeout=12)
        else:
            response_context = urlopen(request, timeout=12)
        with response_context as response:
            metadata = json.load(response)
        title = str(metadata.get("title", "")).strip()
        author = str(metadata.get("author_name", "")).strip()
        if author.endswith(" - Topic"):
            author = author[:-8].strip()
        if title:
            return f"{author} - {title}" if author else title
    except Exception:
        pass
    return url


def youtube_video_id(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        return parsed.path.strip("/").split("/", 1)[0]
    if host == "youtube.com" or host.endswith(".youtube.com"):
        return parse_qs(parsed.query).get("v", [""])[0]
    return ""


def ffmpeg_location() -> str | None:
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    bundled = Path.home() / ".spotdl" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    return str(bundled) if bundled.exists() else None


def download_youtube_fallback(
    query: str,
    output_directory: Path,
    unavailable_url: str,
) -> Path | None:
    unavailable_id = youtube_video_id(unavailable_url)
    candidates = search_youtube(query, 5)
    for candidate in candidates:
        candidate_url = str(candidate.get("url", ""))
        if not candidate_url or youtube_video_id(candidate_url) == unavailable_id:
            continue

        options = {
            "format": "bestaudio/best",
            "outtmpl": str(output_directory / "%(title).160B.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 20,
            "retries": 3,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }],
        }
        ffmpeg = ffmpeg_location()
        if ffmpeg:
            options["ffmpeg_location"] = ffmpeg
        proxy = proxy_url()
        if proxy:
            options["proxy"] = proxy

        before = set(output_directory.glob("*.mp3"))
        try:
            with YoutubeDL(options) as downloader:
                downloader.download([candidate_url])
        except Exception as error:
            logger.warning("YouTube fallback candidate %s failed: %s", candidate_url, error)
            continue

        files = sorted(
            (path for path in output_directory.glob("*.mp3") if path not in before),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if files:
            logger.info("Used YouTube fallback %s for unavailable source %s.", candidate_url, unavailable_url)
            return files[0]
    return None


def run_spotdl(url: str, output_directory: Path) -> Path:
    query = resolve_spotdl_query(url)
    executable = shutil.which("spotdl")
    if not executable:
        local_executable = Path(os.sys.executable).with_name("spotdl.exe")
        executable = str(local_executable) if local_executable.exists() else "spotdl"
    command = [
        executable,
        "download",
        query,
        "--audio",
        "piped",
        "soundcloud",
        "bandcamp",
        "youtube-music",
        "youtube",
        "--format",
        "mp3",
        "--bitrate",
        "128k",
        "--output",
        str(output_directory / "{artists} - {title}.{output-ext}"),
        "--simple-tui",
        "--print-errors",
        "--log-level",
        "ERROR",
    ]
    proxy = proxy_url()
    if proxy:
        command.extend(["--proxy", proxy])
    try:
        result = subprocess.run(
            command,
            cwd=output_directory,
            capture_output=True,
            check=False,
            text=True,
            timeout=7 * 60,
        )
    except subprocess.TimeoutExpired as error:
        raise HTTPException(status_code=504, detail="SpotDL took too long to finish this track.") from error

    files = sorted(output_directory.glob("*.mp3"), key=lambda path: path.stat().st_mtime, reverse=True)
    if result.returncode != 0 or not files:
        output = "\n".join(part for part in (result.stderr, result.stdout) if part).strip()
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        message = lines[-1] if lines else "SpotDL could not convert this track."
        lowered = output.lower()
        logger.warning("SpotDL failed for %s: %s", url, message)
        fallback_file = None
        if youtube_video_id(url) and ("video unavailable" in lowered or "yt-dlp download error" in lowered):
            fallback_file = download_youtube_fallback(query, output_directory, url)
        if fallback_file:
            files = [fallback_file]
        elif "sign in to confirm" in lowered or "not a bot" in lowered:
            message = "YouTube temporarily blocked this cloud server. Try again later or upload the audio file directly."
        elif "no results found" in lowered:
            message = "SpotDL could not find a matching audio result for this track."
        if not fallback_file:
            raise HTTPException(status_code=422, detail=message[:240])
    if len(files) > 1:
        raise HTTPException(status_code=400, detail="Paste one track link, not a playlist or album.")
    if files[0].stat().st_size > 75 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="The converted track is larger than 75 MB.")
    return files[0]


def cleanup_job(job_id: str) -> None:
    job = jobs.pop(job_id, None)
    if job:
        shutil.rmtree(job["work_directory"], ignore_errors=True)


def cleanup_expired_jobs() -> None:
    cutoff = time.time() - 30 * 60
    for job_id, job in list(jobs.items()):
        if job["created_at"] < cutoff and job["status"] != "running":
            cleanup_job(job_id)


async def process_job(job_id: str, url: str) -> None:
    job = jobs[job_id]
    job["status"] = "running"
    try:
        async with conversion_slot:
            output_file = await asyncio.to_thread(
                run_spotdl,
                url,
                job["work_directory"],
            )
        job.update(
            status="ready",
            output_file=output_file,
            file_name=output_file.name[:255],
            title=output_file.stem[:160],
        )
    except HTTPException as error:
        job.update(status="error", detail=str(error.detail)[:240])
        shutil.rmtree(job["work_directory"], ignore_errors=True)
    except Exception:
        logger.exception("Cloud conversion job %s failed unexpectedly.", job_id)
        job.update(status="error", detail="SpotDL could not convert this track.")
        shutil.rmtree(job["work_directory"], ignore_errors=True)


def retain_task(task: asyncio.Task) -> None:
    job_tasks.add(task)
    task.add_done_callback(job_tasks.discard)


@app.get("/health")
async def health():
    companion_url = active_companion_url()
    return {
        "ok": True,
        "mode": "relay" if companion_url else "cloud",
        "windows_companion": bool(companion_url),
        "active_jobs": sum(job["status"] in {"queued", "running"} for job in jobs.values()),
    }


@app.post("/v1/register")
async def register(
    payload: RegisterRequest,
    authorization: str | None = Header(default=None),
):
    authorize(authorization)
    base_url = normalize_companion_url(payload.url)

    global local_companion_url, local_companion_seen
    local_companion_url = base_url
    local_companion_seen = time.monotonic()
    return {"connected": True}


@app.get("/v1/search")
async def youtube_search(
    q: str,
    limit: int = 10,
    authorization: str | None = Header(default=None),
):
    authorize(authorization)
    query = q.strip()[:120]
    if len(query) < 2:
        raise HTTPException(status_code=400, detail="Enter at least two characters.")
    companion_url = active_companion_url()
    if companion_url:
        _, result = await asyncio.to_thread(
            companion_json,
            companion_url,
            f"/v1/search?{urlencode({'q': query, 'limit': max(1, min(limit, 10))})}",
            os.getenv("SPOTDL_API_TOKEN", ""),
        )
        return result
    try:
        results = await asyncio.to_thread(search_youtube, query, max(1, min(limit, 10)))
        return {"results": results}
    except Exception as error:
        raise HTTPException(status_code=502, detail="YouTube search is temporarily unavailable.") from error


@app.post("/v1/jobs", status_code=202)
async def create_job(
    payload: ConvertRequest,
    authorization: str | None = Header(default=None),
):
    authorize(authorization)
    if not is_allowed_url(payload.url):
        raise HTTPException(status_code=400, detail="Use a valid YouTube, YouTube Music, or Spotify URL.")

    companion_url = active_companion_url()
    if companion_url:
        _, result = await asyncio.to_thread(
            companion_json,
            companion_url,
            "/v1/jobs",
            os.getenv("SPOTDL_API_TOKEN", ""),
            method="POST",
            body={"url": payload.url},
        )
        job_id = str(result.get("job_id", ""))
        if not job_id:
            raise HTTPException(status_code=502, detail="The Windows companion returned an invalid job.")
        relay_jobs[job_id] = {
            "base_url": companion_url,
            "created_at": time.time(),
        }
        return {"job_id": job_id, "status": result.get("status", "queued")}

    cleanup_expired_jobs()
    job_id = uuid.uuid4().hex
    jobs[job_id] = {
        "status": "queued",
        "created_at": time.time(),
        "work_directory": Path(tempfile.mkdtemp(prefix="musicpocket-")),
    }
    retain_task(asyncio.create_task(process_job(job_id, payload.url)))
    return {"job_id": job_id, "status": "queued"}


@app.get("/v1/jobs/{job_id}")
async def job_status(
    job_id: str,
    authorization: str | None = Header(default=None),
):
    authorize(authorization)
    relay_job = relay_jobs.get(job_id)
    if relay_job:
        _, result = await asyncio.to_thread(
            companion_json,
            relay_job["base_url"],
            f"/v1/jobs/{job_id}",
            os.getenv("SPOTDL_API_TOKEN", ""),
        )
        return result
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="This conversion job is no longer available.")
    response = {"job_id": job_id, "status": job["status"]}
    if job["status"] == "error":
        response["detail"] = job.get("detail", "SpotDL could not convert this track.")
    if job["status"] == "ready":
        response["file_name"] = job["file_name"]
        response["title"] = job["title"]
    return response


@app.get("/v1/jobs/{job_id}/file")
async def job_file(
    job_id: str,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
):
    authorize(authorization)
    relay_job = relay_jobs.get(job_id)
    if relay_job:
        upstream = await asyncio.to_thread(
            open_companion_file,
            relay_job["base_url"],
            f"/v1/jobs/{job_id}/file",
            os.getenv("SPOTDL_API_TOKEN", ""),
        )
        relay_jobs.pop(job_id, None)
        return companion_audio_response(upstream)
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="This conversion job is no longer available.")
    if job["status"] != "ready":
        raise HTTPException(status_code=409, detail="This conversion is not ready yet.")

    background_tasks.add_task(cleanup_job, job_id)
    return FileResponse(
        job["output_file"],
        media_type="audio/mpeg",
        filename=job["file_name"],
        background=background_tasks,
        headers={
            "X-MusicPocket-Filename": quote(job["file_name"]),
            "X-MusicPocket-Title": quote(job["title"]),
            "Cache-Control": "no-store",
        },
    )


@app.post("/v1/convert")
async def convert(
    payload: ConvertRequest,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
):
    authorize(authorization)
    if not is_allowed_url(payload.url):
        raise HTTPException(status_code=400, detail="Use a valid YouTube, YouTube Music, or Spotify URL.")

    companion_url = active_companion_url()
    if companion_url:
        upstream = await asyncio.to_thread(
            open_companion_conversion,
            companion_url,
            payload,
            os.getenv("SPOTDL_API_TOKEN", ""),
        )
        return companion_audio_response(upstream)

    work_directory = Path(tempfile.mkdtemp(prefix="musicpocket-"))
    try:
        async with conversion_slot:
            output_file = await asyncio.to_thread(run_spotdl, payload.url, work_directory)
        safe_name = output_file.name[:255]
        title = output_file.stem[:160]
        background_tasks.add_task(shutil.rmtree, work_directory, True)
        return FileResponse(
            output_file,
            media_type="audio/mpeg",
            filename=safe_name,
            background=background_tasks,
            headers={
                "X-MusicPocket-Filename": quote(safe_name),
                "X-MusicPocket-Title": quote(title),
                "Cache-Control": "no-store",
            },
        )
    except Exception:
        shutil.rmtree(work_directory, ignore_errors=True)
        raise
