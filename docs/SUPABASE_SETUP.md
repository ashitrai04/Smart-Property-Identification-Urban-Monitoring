# Supabase Setup (Activity Logs)

The app works without Supabase (logs are kept in the browser's localStorage).
Adding Supabase makes the activity log **cloud-persisted and shared** — every
segmentation run, change detection, AOI analysis and generated report is stored
centrally and appears in **Data Logs** and **DSS reports** from any browser.

## 1. Create the project
1. Go to <https://supabase.com> → New project (free tier is fine).
2. Pick any region close to your users (e.g. Mumbai `ap-south-1`).

## 2. Create the table (SQL Editor → paste → Run)

```sql
create table if not exists public.activity_logs (
  id          uuid primary key default gen_random_uuid(),
  created_at  timestamptz not null default now(),
  session_id  text,
  type        text not null,          -- "AI Segmentation" | "Change Detection" | "AOI Analysis" | "Boundary Upload" | "Report Generated"
  district    text,
  area        text,                   -- what was analysed (file name, "2.1 km² AOI", "Full District")
  status      text not null default 'Completed',
  metrics     jsonb,                  -- real result numbers (counts / percentages)
  meta        jsonb
);

create index if not exists activity_logs_created_at_idx on public.activity_logs (created_at desc);
create index if not exists activity_logs_district_idx  on public.activity_logs (district);

alter table public.activity_logs enable row level security;

-- The web app uses the anon key: allow it to write and read logs.
create policy "anon insert logs" on public.activity_logs
  for insert to anon with check (true);
create policy "anon read logs" on public.activity_logs
  for select to anon using (true);
```

## 3. Add the keys to the app
Project Settings → **API** → copy *Project URL* and *anon public* key, then add
to `.env` (and to Vercel → Project → Settings → Environment Variables):

```
VITE_SUPABASE_URL=https://<project-ref>.supabase.co
VITE_SUPABASE_ANON_KEY=<anon public key>
```

Redeploy (or restart `npm run dev`). Data Logs will show **Cloud sync: on**.

## Notes
- The anon key is safe to ship in the frontend **only** because RLS restricts it
  to insert/select on this one table. Don't add broader policies to it.
- localStorage keeps mirroring the last 300 entries as an offline fallback.
- To wipe demo data: `delete from public.activity_logs;` in the SQL editor.
