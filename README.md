# MusicPocket cloud companion

This private service runs SpotDL, yt-dlp, Deno, and FFmpeg in the cloud so
MusicPocket can search and import music from a phone without a Windows PC.
Deploy the folder as a single Docker service on Render, Railway, Fly.io, or a
small VPS.

Set one required environment variable on the container:

```text
SPOTDL_API_TOKEN=a-long-random-secret
```

Optional:

```text
SPOTDL_CONCURRENCY=1
```

Then configure MusicPocket with the same token and the container endpoint:

```text
SPOTDL_API_URL=https://your-service.example/v1/convert
SPOTDL_API_TOKEN=a-long-random-secret
```

The service provides:

- `GET /health`
- `GET /v1/search`
- `POST /v1/jobs`
- `GET /v1/jobs/{job_id}`
- `GET /v1/jobs/{job_id}/file`
- `POST /v1/convert` for backwards compatibility

It accepts one YouTube, YouTube Music, or Spotify track URL, rejects unrelated
hosts, limits conversions to 75 MB, and deletes temporary files after delivery.
Use one container instance so its short-lived job state remains consistent.

For truly continuous availability, use a service plan that does not sleep when
idle. A sleeping free instance can still work, but the first request may need
time to wake up.
