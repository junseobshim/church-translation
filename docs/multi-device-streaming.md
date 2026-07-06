# Multi-device streaming to live.rctranslation.org — design options

> Cross-device routing sketches. **Not implemented** — designs to look at later.
>
> Same-device tunnel leaks are already handled by the `main.py` signal handlers
> and the `launcher.sh` reaps (self-heal on launch + pkill on quit). Those make
> one machine reliable but cannot touch a stale `cloudflared` on a *different*
> machine. This doc is about the cross-device problem.

## The problem being solved

Every device runs `cloudflared` for the **same shared named tunnel**
(`church-live`). Cloudflare treats multiple `cloudflared` instances on one
tunnel as **replicas** — HA ingress for the *same* origin. A request goes to the
geographically-closest replica, and Cloudflare only retries another replica if
the **connection** fails, not if the connection is healthy but its **origin**
(`localhost:8080`) is down. Bare replicas offer "no traffic steering." So a
leaked/idle device whose origin is dead still answers ~half the requests with
502s. There is no way, from the cloud, to make "the device presenting right now"
the authoritative origin.

Three ways out, in increasing order of effort:

| | Approach | Arbitration | Keeps per-device `cloudflared`? | Recurring cost |
|---|---|---|---|---|
| 1 | Per-device tunnels + Worker "live pointer" redirect | Latest claim wins | Yes | No |
| 2 | Per-device tunnels + Cloudflare Load Balancer | Health checks | Yes | **Yes (paid LB add-on)** |
| 3 | Shared origin — devices push to a stateful Worker | "Live = whoever is pushing" | **No — tunnels removed entirely** | Likely (Workers Paid for Durable Objects) |

---

## Option 1 — "Current-live" device pointer (Worker + KV, redirect-based)

### Idea in one line

Give each device its **own** tunnel + hostname, and have the edge Worker point
`live.rctranslation.org` at whichever device most recently **claimed "I'm
live."** Latest-writer-wins; stale devices are simply never routed to.

### How it works

```
device A ── tunnel A ──> a.rctranslation.org   (its own hostname)
device B ── tunnel B ──> b.rctranslation.org
                              │
   KV: live = "b"  <─────── control_server on B calls POST /claim at session start
                              │
viewer ─> live.rctranslation.org/  ─> Worker reads KV.live ─> 302 redirect to b.rctranslation.org/?<same query>
                              │
viewer then polls  b.rctranslation.org/api/latest  directly (bypasses Worker → no free-tier hit)
```

1. **Per-device tunnels/hostnames.** Each device gets its own tunnel UUID and a
   hostname (`a.rctranslation.org`, `b.rctranslation.org`, …). This is what
   makes a specific device addressable — replicas of one shared UUID are *not*
   individually addressable (confirmed in Cloudflare's LB docs).
2. **A claim on start.** When a session starts (`control_server` `/api/start`,
   or `main.py` at launch), the device calls an authenticated Worker endpoint
   (`POST /claim` with a shared secret) that writes `live = "<device-id>"` to
   Workers KV.
3. **Redirect at the edge.** The existing `live.rctranslation.org/*` Worker
   route reads `KV.live` on a root page load and issues a `302` to that device's
   hostname, preserving the query string (`?mode=…&lang=…&hideStatus=1`).
4. **Polling stays direct.** After the redirect the viewer polls
   `b.rctranslation.org/api/latest`, which hits device B's tunnel directly — so
   the `/api/*` free-tier exemption still holds (no Worker invocation per poll).

### What changes

- **Infra:** one tunnel + one DNS hostname per device; a Workers KV namespace.
- **Worker (`worker/src/index.js`):** on `/`, read `KV.live`, redirect to the
  device host (fall back to the waiting page if unset/empty). Add a small
  authenticated `POST /claim` handler.
- **App:** `control_server`/`main.py` fires one `POST /claim` at session start
  (and optionally clears it on clean stop).
- Each device's `--tunnel` name (currently hardcoded default `church-live`)
  becomes per-device; the `pkill` pattern in `launcher.sh` would scope to that
  device's tunnel name.

### Pros

- Stale `cloudflared` on any other device is **never in the routing path** — the
  cross-device "half the time" problem disappears without needing to kill remote
  processes.
- Preserves the free-tier polling design (polls bypass the Worker).
- Modest infra; reuses the Worker that already exists.

### Cons / tradeoffs

- Per-device tunnel + hostname setup (more onboarding per volunteer machine).
- The address bar changes to the device hostname after the redirect (cosmetic;
  fine for ProPresenter/projection, which follow redirects).
- `/claim` needs a shared secret so a random client can't hijack the pointer.
- Near-simultaneous starts on two devices → last claim wins (probably fine for
  one operator, worth a thought for two).

### Open questions / gotchas

- **Handoff while a viewer is watching:** once redirected to
  `b.rctranslation.org`, an open viewer is pinned to B's host; if the live
  device later switches to C, that viewer won't follow until it reloads.
  Options: front each device hostname with the same waiting-room Worker (so B
  going down shows the waiting page and auto-reloads, which re-hits `live.` and
  re-redirects), or add a lightweight "am I still live?" check in the caption
  page.
- Decide whether `/claim` is best fired from `control_server` (knows session
  start/stop) vs `main.py` (knows the tunnel actually came up).

---

## Option 2 — Per-device tunnels + Cloudflare Load Balancer with health checks

### Idea in one line

Give each device its own tunnel UUID and put a **Cloudflare Load Balancer** in
front of `live.rctranslation.org` with **health checks** on each device's
origin — dead origins drop out of rotation on their own.

### How it works

```
device A ── tunnel A (uuid-A) ──┐
device B ── tunnel B (uuid-B) ──┤─>  LB pool (health-checked)  ─>  live.rctranslation.org
device C ── tunnel C (uuid-C) ──┘
                                     health check hits /api/latest (or /healthz)
                                     on each origin; unhealthy origin = removed
```

1. **Per-device tunnels.** Each device gets a distinct tunnel UUID. This is
   required: Cloudflare's LB docs state it "treats all replicas of the same
   tunnel UUID as a single endpoint. For granular traffic steering … connect
   each host using a different tunnel UUID so the load balancer can address
   them independently."
2. **Load Balancer + pool.** Create an LB for `live.rctranslation.org` whose
   pool endpoints are the per-device tunnel origins.
3. **Health checks.** Point monitors at a caption endpoint (`/api/latest`
   returns 200 with data when live; a dedicated `/healthz` would be cleaner). A
   device with a dead/absent origin fails its check and is pulled from rotation
   automatically.
4. **Steering.** With one device live at a time, only its origin is healthy →
   all traffic goes to it. Could also configure priority/failover ordering.

### What changes

- **Infra:** per-device tunnel UUID; a Cloudflare **Load Balancer** (paid
  add-on) with a pool + monitor.
- **App:** optionally add a cheap `/healthz` route to `main.py`'s caption server
  that returns 200 only while a session is actively producing lines (so
  "process up but no captions" can be treated as unhealthy if desired).
- Per-device `--tunnel` name; `launcher.sh` `pkill` pattern scoped per device.

### Pros

- **Automatic, health-based** failover — no custom arbitration code, no shared
  secret.
- Handles messy states well (multiple devices up, flapping, etc.).
- Closest to a "standard" HA setup; well-trodden Cloudflare path.

### Cons / tradeoffs

- **Cloudflare Load Balancing is a paid subscription** (separate from Workers).
  For a single-service-at-a-time church stream this may be more than needed.
- Health-check interval + threshold add **failover latency** (seconds up to ~a
  minute) before a newly-dead origin is pulled — viewers may see brief gaps at
  handoff.
- Per-device tunnel onboarding (same as Option 1).
- Need to confirm the exact wiring of a **Cloudflare Tunnel as an LB pool
  origin** (origin type / private hostname) against current docs before
  committing.

### Open questions / gotchas

- What's the health signal? Process-up isn't enough (a live `cloudflared` with a
  dead caption server is the exact failure mode). A `/healthz` that reflects
  "captions flowing" is more honest than checking `/api/latest` for 200.
- Session affinity generally not needed (captions are stateless polls), but
  worth a glance if switching mid-service.

---

## Option 3 — Shared origin (Worker-backed store), no per-device tunnels

> The "remove the whole problem class" option. Bigger rewrite, cleanest end
> state. Same-device leak fixes become moot because there is no per-device
> `cloudflared` at all.

### Idea in one line

Devices **push** caption lines up to the Worker (a small cloud store); viewers
read from the Worker. "Which device is live" is simply "whoever is currently
pushing."

### How it works

```
device (any) ──POST /api/push {line}──>  Worker  ──>  store (Durable Object / KV / D1)
                                            │
viewer ─> live.rctranslation.org/  ─>  Worker serves caption page
viewer ─> /api/latest (or WebSocket/SSE) ─> Worker streams from the store
```

1. **Push instead of serve.** `main.py` stops serving captions locally and
   tunneling; instead it `POST`s each finalized line (transcription +
   translations) to an authenticated Worker endpoint (`/api/push`, shared
   secret).
2. **Stateful Worker.** The Worker keeps the recent rolling window in a
   **Durable Object** (best: ordered, low-latency, single-writer, supports
   WebSocket push) or KV/D1 (simpler but see limits below).
3. **Viewers read from the edge.** The caption page is served by the Worker;
   updates come via **WebSocket/SSE push** (ideal — kills the polling storm) or
   by polling `/api/latest` at the Worker.
4. **No tunnels.** No `cloudflared` anywhere; `launcher.sh`, the signal
   handling, and the whole tunnel lifecycle can be deleted. Any device with
   internet can present.

### What changes

- **App:** replace the local caption HTTP server + `cloudflared` launch in
  `main.py` with an HTTPS push client. Drop the tunnel plumbing and
  `launcher.sh` reaps.
- **Worker:** becomes stateful — push endpoint, storage, and a read path
  (WebSocket/SSE or polling). Auth for pushes.
- **Viewer page:** ideally switch from polling to WebSocket/SSE.

### Pros

- **Removes the entire leak class** — nothing to orphan, no per-device tunnel
  setup, no signal/launcher hacks needed.
- **Trivial multi-device:** any device can present; switching is instant; no
  arbitration logic.
- Works from any network with internet, not just tunnel-configured machines.
- Natural evolution of the `worker/` that already lives on the edge 24/7.

### Cons / tradeoffs

- **Biggest rewrite** of the three (caption transport + stateful Worker + viewer
  transport).
- **Latency:** every caption now makes a cloud round-trip on push *and* on read
  (vs today's localhost serve). Usually fine, but it's added hops on a live
  feed.
- **Cost / limits — verify current Cloudflare plan terms (fast-moving; don't
  trust memory):**
  - **Durable Objects** (the right tool here) have historically required the
    **Workers Paid** plan (~$5/mo). Confirm current availability/pricing.
  - **KV** has per-key **write-rate limits** (~1 write/sec sustained per key) —
    likely too slow for rapid caption updates unless batched; also
    eventually-consistent.
  - If you keep **polling** instead of WebSocket/SSE, `/api/latest` now hits the
    **Worker** (~6 req/s per viewer), consuming the Workers free-tier request
    budget — the exact thing the current `/api/*` tunnel exemption avoids. Push
    (WS/SSE) sidesteps this and is the recommended path.

### Open questions / gotchas

- Durable Object vs KV vs D1 for the rolling window (DO is the natural fit;
  confirm plan/cost).
- WebSocket/SSE vs polling for viewers (WS with DO hibernation is cleanest and
  cheapest at scale).
- Auth model for `/api/push` (shared secret vs signed token).
- Migration: could run alongside the current tunnel setup during transition.

---

## Choosing between them

- **Option 1** is the cheapest path to correct routing: simpler infra than
  Option 2 (no paid Load Balancer), but arbitration is "latest claim" rather
  than health-based, and the leak *class* still exists on each box — just routed
  around.
- **Option 2** buys robust, health-based arbitration for the price of the LB
  subscription and slower failover.
- **Option 3** is the only one that *eliminates* the problem instead of routing
  around it — highest effort, likely a small recurring cost, cleanest long-term
  architecture. It also naturally fixes the viewer-scaling issues (polling
  storm through a single-threaded local server) that the tunnel options leave
  in place.
