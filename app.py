import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Header, HTTPException
from fastapi.background import BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="MusicPocket SpotDL Companion", docs_url=None, redoc_url=None)
conversion_slot = asyncio.Semaphore(max(1, int(os.getenv("SPOTDL_CONCURRENCY", "1"))))
allowed_hosts = ("youtube.com", "youtu.be", "spotify.com")


class ConvertRequest(BaseModel):
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


def run_spotdl(url: str, output_directory: Path) -> Path:
    command = [
        "spotdl",
        "download",
        url,
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
    return {"ok": True}


@app.post("/v1/convert")
async def convert(
    payload: ConvertRequest,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
):
    authorize(authorization)
    if not is_allowed_url(payload.url):
        raise HTTPException(status_code=400, detail="Use a valid YouTube, YouTube Music, or Spotify URL.")

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
