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

## 4. Auto-prune old rows (keep the free tier tiny)

So the table can never grow unbounded, this trigger keeps only the **newest 200
rows** and deletes older ones automatically on every insert. It runs
`security definer` (as the table owner), so pruning works **without** giving the
anon key any delete permission — RLS stays locked to insert/select.

```sql
-- keeps the newest N activity rows; deletes the rest on every insert
create or replace function public.prune_activity_logs()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  delete from public.activity_logs
  where id in (
    select id from public.activity_logs
    order by created_at desc, id desc
    offset 200                       -- change 200 to keep more/fewer rows
  );
  return null;
end;
$$;

drop trigger if exists trg_prune_activity_logs on public.activity_logs;
create trigger trg_prune_activity_logs
  after insert on public.activity_logs
  for each statement
  execute function public.prune_activity_logs();
```

At ~1 KB/row this caps the table at well under 1 MB — a rounding error against
the 500 MB free quota — while keeping enough history for Data Logs and DSS
reports. Lower `200` to `100` if you want it even leaner.

## Notes
- The anon key is safe to ship in the frontend **only** because RLS restricts it
  to insert/select on this one table. Don't add broader policies to it.
- localStorage keeps mirroring the last 300 entries as an offline fallback.
- The keep-alive query (every 12 h) and the pruning trigger together keep the
  project both **awake** and **small** — no free-tier limit is ever approached.
- To wipe demo data: `delete from public.activity_logs;` in the SQL editor.
