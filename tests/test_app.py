import asyncio
import os
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


if __name__ == "__main__":
    unittest.main()
