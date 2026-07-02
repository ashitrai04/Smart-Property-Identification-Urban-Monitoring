// Supabase client — optional. The app works fully without it (activity logs
// fall back to localStorage); adding the two env vars turns on cloud sync.
//   VITE_SUPABASE_URL=https://<project-ref>.supabase.co
//   VITE_SUPABASE_ANON_KEY=<anon public key>
// Schema + policies: see docs/SUPABASE_SETUP.md
import { createClient } from "@supabase/supabase-js";

const url = import.meta.env.VITE_SUPABASE_URL;
const key = import.meta.env.VITE_SUPABASE_ANON_KEY;

export const supabase = url && key ? createClient(url, key) : null;
export const supabaseEnabled = !!supabase;
