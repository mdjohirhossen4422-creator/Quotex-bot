# Deploy the collector on Render

Goal: run the live collector continuously on Render and keep the candle history.
This is a **data-collection** deployment. It never trades and sends nothing.

## Read this first (honest)

- **Render has no free background worker.** Web services have a free tier, but a
  Free *web* service **spins down after 15 minutes without inbound traffic** —
  which kills the websocket feed. A background worker runs continuously but
  starts at **$7/month** (0.5 CPU / 512 MB). Add **$0.25/month** for a 1 GB disk.
- **Pick a US region.** The feed works from US datacenter IPs (verified: this
  project's Google Cloud US host connects fine). The blocked egress we saw on
  Botkeep was **EU (Warsaw)**. Render regions: `oregon` (default), `ohio`,
  `virginia`, `frankfurt`, `singapore` — prefer a US one.
- If even the US region is blocked for you, set `QX_PROXY` (see below).

## Option A — Blueprint (one file, recommended)

1. Push this repo to GitHub (already done for this project).
2. Render Dashboard → **New → Blueprint**.
3. Pick the repo, and set **Blueprint Path** to `deploy/render/render.yaml`.
4. Render reads the file and creates the `quotex-collector` **worker** with a
   1 GB disk mounted at `/data` and candle-only mode already set.
5. Apply. Watch the **Logs** tab for the first heartbeat:

   ```
   [..] conn=True pairs=91 ticks_new=.. candles_new=.. dup=0 reconnects=0 rss=..MB
   ```

   `conn=True` can take ~90 s (slow handshake). `dup=0` must hold.

## Option B — Manual (dashboard clicks)

1. **New → Background Worker**, connect the repo.
2. Runtime: **Docker**.
   - Dockerfile Path: `deploy/render/Dockerfile.collector`
   - Docker Build Context Directory: `.` (repo root — the Dockerfile copies
     modules from the root)
3. Region: a **US** one. Plan: **Starter** (0.5 CPU / 512 MB, $7/mo).
4. **Advanced → Add Disk**: name `collector-data`, mount path `/data`, 1 GB.
5. Environment:
   | key | value |
   | --- | --- |
   | `CANDLE_DB` | `/data/candles.db` |
   | `COLLECTOR_STATE` | `/data/collector_state.json` |
   | `COLLECTOR_STORE_TICKS` | `0` (candle-only, ~63 MB/day) |
   | `QX_PROXY` | *(only if this region is blocked)* e.g. `socks5://user:pass@host:1080` |
6. Start command (leave default): `python live_collector.py --no-ticks`

## Verify it is really collecting

- **Logs**: the heartbeat line increments `ticks_new` / `candles_new` and keeps
  `dup=0`. `conn=True` with `pairs=91`.
- **Disk**: the **Disk** tab shows `/data` growing (tens of MB/day).
- A `403 Forbidden` loop instead means the region is geo-blocked → set `QX_PROXY`.

## What survives a redeploy

Only files under `/data` (the disk). That is `candles.db` + its WAL, the state
file, and the daily reports. Everything else is ephemeral, which is fine — the
code comes from the image on every deploy.

## Cost summary

| item | cost |
| --- | --- |
| Background worker, 0.5 CPU / 512 MB | **$7 / month** |
| Persistent disk, 1 GB | **$0.25 / month** |
| Bandwidth | well under the included 5 GB (the feed is small) |

So **≈ $7.25/month** for continuous, geo-safe collection with durable storage.
That is more reliable than Botkeep (EU, blocked) and than an Android phone
(Doze/sleep kills the service).

## Alternative: the dashboard on Render (free, but sleeps)

The repo-root `render.yaml` deploys the **dashboard** web service instead. It is
free to try but its Free instance **sleeps after 15 min idle**, dropping the
feed; set it to a paid plan for anything continuous. For pure collection, the
worker in this folder is the right shape.
