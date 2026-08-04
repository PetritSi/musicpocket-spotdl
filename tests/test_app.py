import asyncio
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.background import BackgroundTasks

import app as companion


TOKEN = "test-token"
AUTHORIZATION = f"Bearer {TOKEN}"


class CloudCompanionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"SPOTDL_API_TOKEN": TOKEN})
        self.environment.start()
        companion.jobs.clear()
        companion.job_tasks.clear()
        companion.relay_jobs.clear()
        companion.local_companion_url = None
        companion.local_companion_seen = 0.0
        self.temp_directory = patch.object(
            companion.tempfile,
            "mkdtemp",
            return_value=str(Path(__file__).parent),
        )
        self.remove_directory = patch.object(companion.shutil, "rmtree")
        self.temp_directory.start()
        self.remove_directory.start()

    def tearDown(self):
        companion.jobs.clear()
        companion.relay_jobs.clear()
        companion.local_companion_url = None
        companion.local_companion_seen = 0.0
        self.remove_directory.stop()
        self.temp_directory.stop()
        self.environment.stop()

    async def test_health_reports_cloud_mode(self):
        payload = await companion.health()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "cloud")
        self.assertEqual(payload["active_jobs"], 0)

    async def test_job_converts_without_a_windows_companion(self):
        def fake_spotdl(_url: str, output_directory: Path) -> Path:
            return Path(__file__).parent / "Example Artist - Example Song.mp3"

        with patch.object(companion, "run_spotdl", fake_spotdl):
            created = await companion.create_job(
                companion.ConvertRequest(url="https://www.youtube.com/watch?v=example"),
                AUTHORIZATION,
            )
            job_id = created["job_id"]

            for _ in range(100):
                status = await companion.job_status(job_id, AUTHORIZATION)
                if status["status"] in {"ready", "error"}:
                    break
                await asyncio.sleep(0.01)

        self.assertEqual(status["status"], "ready", status)
        self.assertEqual(status["title"], "Example Artist - Example Song")
        response = await companion.job_file(job_id, BackgroundTasks(), AUTHORIZATION)
        self.assertEqual(response.media_type, "audio/mpeg")
        self.assertEqual(response.headers["x-musicpocket-title"], "Example%20Artist%20-%20Example%20Song")

    async def test_registered_windows_companion_handles_search_and_jobs(self):
        await companion.register(
            companion.RegisterRequest(url="https://personal-music.trycloudflare.com"),
            AUTHORIZATION,
        )

        job_id = "a" * 32
        with patch.object(
            companion,
            "companion_json",
            side_effect=[
                (200, {"results": [{"id": "video", "title": "Example Song"}]}),
                (202, {"job_id": job_id, "status": "queued"}),
                (200, {"job_id": job_id, "status": "ready"}),
            ],
        ) as relay:
            search = await companion.youtube_search("Example Song", 5, AUTHORIZATION)
            created = await companion.create_job(
                companion.ConvertRequest(url="https://open.spotify.com/track/example"),
                AUTHORIZATION,
            )
            status = await companion.job_status(job_id, AUTHORIZATION)

        health = await companion.health()
        self.assertEqual(health["mode"], "relay")
        self.assertTrue(health["windows_companion"])
        self.assertEqual(search["results"][0]["title"], "Example Song")
        self.assertEqual(created["job_id"], job_id)
        self.assertEqual(status["status"], "ready")
        self.assertIn("/v1/search?", relay.call_args_list[0].args[1])
        self.assertEqual(relay.call_args_list[1].args[1], "/v1/jobs")
        self.assertEqual(relay.call_args_list[2].args[1], f"/v1/jobs/{job_id}")

    async def test_rejects_invalid_or_unauthorized_jobs(self):
        with self.assertRaises(HTTPException) as unauthorized:
            await companion.create_job(
                companion.ConvertRequest(url="https://www.youtube.com/watch?v=example"),
                "Bearer wrong-token",
            )
        self.assertEqual(unauthorized.exception.status_code, 401)

        with self.assertRaises(HTTPException) as invalid:
            await companion.create_job(
                companion.ConvertRequest(url="https://example.com/not-music"),
                AUTHORIZATION,
            )
        self.assertEqual(invalid.exception.status_code, 400)

    def test_unavailable_youtube_source_uses_search_fallback(self):
        fallback_file = Path(__file__).parent / "Example Artist - Example Song.mp3"
        failed_download = subprocess.CompletedProcess(
            args=["spotdl"],
            returncode=1,
            stdout="",
            stderr=(
                "Video unavailable. This video is not available\n"
                "YT-DLP download error - https://www.youtube.com/watch?v=unavailable"
            ),
        )

        with (
            patch.object(companion, "resolve_spotdl_query", return_value="Artist - Song"),
            patch.object(companion.subprocess, "run", return_value=failed_download),
            patch.object(companion, "download_youtube_fallback", return_value=fallback_file) as fallback,
        ):
            result = companion.run_spotdl(
                "https://www.youtube.com/watch?v=unavailable",
                Path(__file__).parent,
            )

        self.assertEqual(result, fallback_file)
        fallback.assert_called_once_with(
            "Artist - Song",
            Path(__file__).parent,
            "https://www.youtube.com/watch?v=unavailable",
        )

    def test_proxy_is_applied_to_spotdl_and_youtube_search(self):
        proxy = "http://proxy-user:proxy-password@proxy.example:1234"
        successful_download = subprocess.CompletedProcess(
            args=["spotdl"],
            returncode=0,
            stdout="",
            stderr="",
        )
        captured_options = {}

        class FakeYoutubeDL:
            def __init__(self, options):
                captured_options.update(options)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def extract_info(self, *_args, **_kwargs):
                return {"entries": []}

        with (
            patch.dict(os.environ, {"SPOTDL_PROXY_URL": proxy}),
            patch.object(companion, "resolve_spotdl_query", return_value="Artist - Song"),
            patch.object(companion.subprocess, "run", return_value=successful_download) as run,
        ):
            companion.run_spotdl(
                "https://www.youtube.com/watch?v=available",
                Path(__file__).parent,
            )
            with patch.object(companion, "YoutubeDL", FakeYoutubeDL):
                companion.search_youtube("Artist Song", 3)

        command = run.call_args.args[0]
        proxy_index = command.index("--proxy")
        self.assertEqual(command[proxy_index + 1], proxy)
        self.assertEqual(captured_options["proxy"], proxy)

    def test_rejects_invalid_proxy_configuration(self):
        with patch.dict(os.environ, {"SPOTDL_PROXY_URL": "not-a-proxy"}):
            with self.assertRaisesRegex(RuntimeError, "valid HTTP or HTTPS proxy URL"):
                companion.proxy_url()


if __name__ == "__main__":
    unittest.main()
