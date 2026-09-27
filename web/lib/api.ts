// Server-side calls to the Master Smith Python API. The browser never talks to it directly: every request goes
// through a route handler here, so the API key (if you created one) stays on the server.
const BASE = (process.env.MASTERSMITH_API_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");

export function apiHeaders(extra?: HeadersInit): Headers {
  const h = new Headers(extra);
  if (process.env.MASTERSMITH_API_KEY) h.set("Authorization", `Bearer ${process.env.MASTERSMITH_API_KEY}`);
  return h;
}

export function api(path: string, init: RequestInit = {}): Promise<Response> {
  return fetch(BASE + path, { ...init, headers: apiHeaders(init.headers), cache: "no-store" });
}

export async function apiJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const r = await api(path, init);
  if (!r.ok) throw new Error(`${path}: ${r.status} ${(await r.text()).slice(0, 300)}`);
  return (await r.json()) as T;
}

export type Attachment = { path: string; name: string; kind: "image" | "mesh"; bytes?: number };

export type Providers = {
  fal: { usd: number } | null;
  openrouter: {
    usd: number;
    bought_usd?: number;
    key_usage_today_usd?: number;
    key_usage_week_usd?: number;
    key_usage_month_usd?: number;
    key_limit_usd?: number;
    key_limit_remaining_usd?: number;
  } | null;
  errors: Record<string, string>;
  checked: number;
};

export type ModelOptions = {
  seed_vendors: { key: string; label: string; model: string; multiview: boolean; note: string; usd: number | null }[];
  director_models: { id: string; label: string; in_per_m: number | null; out_per_m: number | null; context?: number | null }[];
  all_models?: boolean;
  picture_models: { id: string; label: string; name: string; usd: number; default: boolean }[];
  pictures: Record<string, string>;
  defaults: { seed_vendor: string; director_model: string; picture_model: string };
};

export type Settings = { seed_vendor?: string; director_model?: string; picture_model?: string };

export type TurnData = {
  settings: Settings | null;      // what the turn ran with
  brief: Record<string, unknown> | null;
  last_job: string | null;
  balance: number;
  providers: Providers | null;
  pictures: { label: string; url: string }[];   // pictures shown this turn: the reference angles to approve
  pictures_kind: "reference" | null;
  question: { question: string; options: string[] } | null;   // the director asked; options render as numbered buttons
  reference_job: string | null;   // the approved reference job the next build will seed from
  chat_cost_usd: number;
  tools: { name: string; args: unknown; result: string }[];
};

export type ChatSummary = {
  id: string;
  title: string;
  created: number | null;
  updated: number | null;
  turns: number;
  jobs: string[];
  last_job_id: string | null;
  name: string | null;
};

export type ChatTurn = {
  at: number;
  user: string;
  attachments: Attachment[];
  reply: string;
  turn: Omit<TurnData, "tools"> & { tools?: TurnData["tools"] };
};

export type ChatState = ChatSummary & {
  turns: ChatTurn[];
  spec: Record<string, unknown> | null;
  settings: Settings;
  reference: Record<string, unknown> | null;
  job_views: JobView[];
};

export type JobView = {
  id: string;
  status: "queued" | "running" | "done" | "failed" | "refused";
  kind: string;
  spec: Record<string, unknown>;
  created: number;
  started: number | null;
  finished: number | null;
  error: string | null;
  log: string[];
  summary: {
    lods?: { triangles: number }[] | null;
    dimensions_m?: number[] | null;
    glass?: unknown;
    review?: { score?: number; verdict?: string; issues?: string[] } | null;
    gate?: { ok: boolean; warnings: string[] } | null;
    bill?: { usd_cost?: number; credits_charged?: number; balance?: number } | null;
    rig?: Record<string, unknown>;
  };
  files: string[];
  previews: string[];
  pictures: string[];           // reference views and customer pictures of the job (downloadable)
  glb: string | null;
};
