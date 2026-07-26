import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="MusicPocket SpotDL Companion", docs_url=None, redoc_url=None)
conversion_slot = asyncio.Semaphore(max(1, int(os.getenv("SPOTDL_CONCURRENCY", "1"))))
allowed_hosts = ("youtube.com", "youtu.be", "spotify.com")
local_companion_url: str | None = None
local_companion_seen = 0.0


class ConvertRequest(BaseModel):
    url: str


class RegisterRequest(BaseModel):
    url: str


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


def check_companion(base_url: str) -> None:
    request = Request(f"{base_url}/health", headers={"User-Agent": "MusicPocket-Relay/1.0"})
    try:
        with urlopen(request, timeout=15) as response:
            if response.status != 200:
                raise HTTPException(status_code=502, detail="The Windows companion is not reachable.")
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=502, detail="The Windows companion is not reachable.") from error


def open_companion_conversion(base_url: str, payload: ConvertRequest, token: str):
    for attempt in range(3):
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
            if error.code in (502, 503, 504) and attempt < 2:
                time.sleep(2)
                continue
            try:
                failure = json.loads(body.decode())
                detail = str(failure.get("detail") or "SpotDL could not convert this track.")
            except Exception:
                detail = "SpotDL could not convert this track."
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


def stream_companion(response):
    try:
        while chunk := response.read(64 * 1024):
            yield chunk
    finally:
        response.close()


def resolve_spotdl_query(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host != "youtu.be" and host != "youtube.com" and not host.endswith(".youtube.com"):
        return url

    endpoint = "https://www.youtube.com/oembed?" + urlencode({"url": url, "format": "json"})
    request = Request(endpoint, headers={"User-Agent": "MusicPocket/1.0"})
    try:
        with urlopen(request, timeout=12) as response:
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


def run_spotdl(url: str, output_directory: Path) -> Path:
    query = resolve_spotdl_query(url)
    command = [
        "spotdl",
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
        if "sign in to confirm" in lowered or "not a bot" in lowered:
            message = "YouTube temporarily blocked this cloud server. Try again later or upload the audio file directly."
        elif "no results found" in lowered:
            message = "SpotDL could not find a matching audio result for this track."
        raise HTTPException(status_code=422, detail=message[:240])
    if len(files) > 1:
        raise HTTPException(status_code=400, detail="Paste one track link, not a playlist or album.")
    if files[0].stat().st_size > 75 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="The converted track is larger than 75 MB.")
    return files[0]


@app.get("/health")
async def health():
    return {
        "ok": True,
        "windows_companion": bool(
            local_companion_url and time.monotonic() - local_companion_seen < 120
        ),
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


@app.post("/v1/convert")
async def convert(
    payload: ConvertRequest,
    authorization: str | None = Header(default=None),
):
    authorize(authorization)
    if not is_allowed_url(payload.url):
        raise HTTPException(status_code=400, detail="Use a valid YouTube, YouTube Music, or Spotify URL.")

    if not local_companion_url or time.monotonic() - local_companion_seen >= 120:
        raise HTTPException(
            status_code=503,
            detail="The Windows companion is offline. Turn on the PC, wait a moment, and try again.",
        )

    token = os.getenv("SPOTDL_API_TOKEN", "")
    async with conversion_slot:
        upstream = await asyncio.to_thread(
            open_companion_conversion,
            local_companion_url,
            payload,
            token,
        )

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
