/**
 * Cloudflare Worker — keeps the Smart Property project's free services awake.
 *
 * A Cron Trigger pings the two Hugging Face Spaces and Supabase so they never
 * idle-sleep (HF Spaces sleep after ~48h of inactivity; Supabase free projects
 * pause after ~7 days). Pinging every 12h leaves a comfortable margin.
 *
 * (Dronacharya has its own separate worker in that project's repo.)
 *
 * All values below are PUBLIC:
 *  - HF /api/health is a public endpoint.
 *  - The Supabase key is the anon (publishable) key already shipped in the
 *    frontend bundle and protected by Row Level Security.
 * So there are no secrets to configure — just paste + add the cron trigger.
 */

const HF_SPACES = [
  "https://asashit-smart-property-backend.hf.space/api/health",
  "https://asashit-smart-property-segformer.hf.space/api/health",
];

const SUPABASE_URL = "https://lmvsqjoqxvugcyeyiavx.supabase.co";
const SUPABASE_ANON =
  "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImxtdnNxam9xeHZ1Z2N5ZXlpYXZ4Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODI5ODQ1NzMsImV4cCI6MjA5ODU2MDU3M30.1FGM07Q-38xd2GLGYWw522jzaqBfBsj76Rjf4bOYgyA";

async function ping(url, opts = {}) {
  const t0 = Date.now();
  try {
    // cacheTtl:0 so we always hit origin (a cached response wouldn't count as activity)
    const r = await fetch(url, { ...opts, cf: { cacheTtl: 0 } });
    return { url, status: r.status, ms: Date.now() - t0 };
  } catch (e) {
    return { url, status: "ERR", error: String(e).slice(0, 140), ms: Date.now() - t0 };
  }
}

async function keepAlive() {
  const jobs = HF_SPACES.map((u) => ping(u));
  // A tiny query keeps the Supabase project active.
  jobs.push(
    ping(`${SUPABASE_URL}/rest/v1/activity_logs?select=id&limit=1`, {
      headers: { apikey: SUPABASE_ANON, Authorization: `Bearer ${SUPABASE_ANON}` },
    })
  );
  const results = await Promise.all(jobs);
  results.forEach((r) =>
    console.log(`[keepalive] ${r.status} ${r.ms}ms ${r.url}${r.error ? " " + r.error : ""}`)
  );
  return results;
}

export default {
  // Runs automatically on the cron schedule defined in wrangler.toml.
  async scheduled(event, env, ctx) {
    ctx.waitUntil(keepAlive());
  },

  // Manual trigger: open the worker's URL in a browser to test and see results.
  async fetch() {
    const results = await keepAlive();
    return new Response(
      JSON.stringify({ ok: true, ranAt: new Date().toISOString(), results }, null, 2),
      { headers: { "content-type": "application/json", "access-control-allow-origin": "*" } }
    );
  },
};
