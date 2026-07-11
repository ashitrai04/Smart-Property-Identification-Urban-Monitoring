# Keep-Alive Worker (Smart Property)

A Cloudflare Worker on a **Cron Trigger** that pings the Smart Property project's
free services every 12 hours so none of them idle-sleep:

| Target | Idles after | Kept alive by |
|--------|-------------|---------------|
| HF Space — backend (`asashit-smart-property-backend`) | ~48 h | `GET /api/health` |
| HF Space — segformer (`asashit-smart-property-segformer`) | ~48 h | `GET /api/health` |
| Supabase project (`lmvsqjoqxvugcyeyiavx`) | ~7 days | `GET /rest/v1/activity_logs?limit=1` |

There are **no secrets to configure** — the HF health endpoints are public and the
Supabase key used is the anon/publishable key (already in the frontend, RLS-protected).

> **Dronacharya has its own separate worker** in that project's repo
> (`DRONACHARYA-RECCE-SYSTEM/keepalive-worker`) — deploy each as its own
> Cloudflare Worker with its own cron.

---

## Deploy — Option A: Dashboard (no CLI, ~2 min)

1. Go to **dash.cloudflare.com → Workers & Pages → Create → Create Worker**.
2. Name it `sp-keepalive`, click **Deploy** (creates a starter), then **Edit code**.
3. Delete the starter code, paste the entire contents of [`src/worker.js`](src/worker.js), and **Deploy**.
4. Open the worker → **Settings → Triggers → Cron Triggers → Add Cron Trigger**.
5. Enter `0 */12 * * *` and **Add**. Done.

**Test it now:** open the worker's `*.workers.dev` URL in a browser — the `fetch`
handler runs the same pings and returns a JSON report of each status + latency.

## Deploy — Option B: Wrangler CLI

```bash
cd keepalive-worker
npx wrangler login      # opens browser to authorize (one time)
npx wrangler deploy     # reads wrangler.toml, sets up the cron automatically
```

To change the schedule, edit `crons` in [`wrangler.toml`](wrangler.toml) and re-run `npx wrangler deploy`.

---

## Cost
Free Workers plan covers this easily: cron triggers are included and this runs
only ~2 times/day (well under the 100k requests/day free limit).

## Verifying it works
- Cloudflare dashboard → the worker → **Logs** (or `npx wrangler tail`) shows a
  `[keepalive] 200 …ms <url>` line per target on each run.
- Or hit the worker URL manually anytime to force a run.
