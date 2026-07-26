# MusicPocket SpotDL companion

This private service runs SpotDL and FFmpeg outside the Cloudflare Worker. Deploy the folder as a Docker service on a platform such as Railway, Render, Fly.io, or your own server.

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

The endpoint accepts a single YouTube, YouTube Music, or Spotify track URL. It intentionally rejects unrelated hosts and files larger than 75 MB.
