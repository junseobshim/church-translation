# Cloudflare Worker: waiting page

Serves a "Waiting for transcription…" page on `https://live.rctranslation.org/` whenever the tunnel has no healthy origin (i.e. `soniox_claude.py` isn't running). Auto-reloads into live captions as soon as the tunnel comes back online.

## Deploy (one-time)

```bash
npm install -g wrangler
wrangler login                # sign in to the same Cloudflare account that owns the tunnel
cd worker
wrangler deploy
```

After deploy, nothing more to do — the Worker lives on Cloudflare's edge 24/7. Any device that runs `soniox_claude.py` (with the `cloudflared` tunnel credentials set up) will be detected as the origin automatically.

## Updating the waiting page

Edit `src/index.js`, then re-run `wrangler deploy`.
