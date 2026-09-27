"use client";

import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport, type UIMessage } from "ai";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Attachment, ChatState, ChatSummary, ChatTurn, JobView, ModelOptions, Providers, Settings, TurnData } from "@/lib/api";
import JobPanel from "./JobPanel";

// Assistant replies are markdown (bold, lists, code); users' own text stays as typed.
function Md({ text }: { text: string }) {
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
}

// The director asked a question with choices: numbered buttons the customer can click; typing still works.
function Options({ q, onPick, busy }: { q: { question: string; options: string[] }; onPick: (text: string) => void; busy: boolean }) {
  return (
    <div className="options">
      {q.options.map((o, i) => (
        <button key={i} type="button" className="option" disabled={busy} onClick={() => onPick(`${i + 1}. ${o}`)}>
          <span className="num">{i + 1}</span> {o}
        </button>
      ))}
      <span className="dim">or type your own answer</span>
    </div>
  );
}

const SETTINGS_KEY = "mastersmith.settings";

function loadSettings(): Settings {
  try {
    return JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}") as Settings;
  } catch {
    return {};
  }
}

const ALL_MODELS = "__all__";

function ModelPicker({
  options,
  settings,
  onChange,
  onLoadAll,
  busy,
}: {
  options: ModelOptions | null;
  settings: Settings;
  onChange: (s: Settings) => void;
  onLoadAll: () => void;
  busy: boolean;
}) {
  if (!options) return null;
  const vendor = settings.seed_vendor || options.defaults.seed_vendor;
  const director = settings.director_model || options.defaults.director_model;
  const v = options.seed_vendors.find((x) => x.key === vendor);
  const known = options.director_models.some((m) => m.id === director);
  const d = options.director_models.find((m) => m.id === director);
  const directorTitle =
    d && d.in_per_m !== null && d.out_per_m !== null
      ? `$${d.in_per_m.toFixed(2)} in / $${d.out_per_m.toFixed(2)} out per 1M tokens · pictures: ${options.pictures.concept}, vision: ${options.pictures.vision}`
      : `Pictures: ${options.pictures.concept} (hard surfaces: ${options.pictures.concept_hard_surface}); vision: ${options.pictures.vision}`;
  return (
    <span className="models">
      <label title={v ? `${v.note}${v.usd !== null ? ` · about $${v.usd.toFixed(2)} a mesh` : ""}` : "mesh vendor"}>
        Mesh
        <select value={vendor} disabled={busy} onChange={(e) => onChange({ ...settings, seed_vendor: e.target.value })}>
          {options.seed_vendors.map((x) => (
            <option key={x.key} value={x.key}>
              {x.label}
              {x.usd !== null ? ` · $${x.usd.toFixed(2)}` : ""}
            </option>
          ))}
        </select>
      </label>
      <label title="The model that draws the reference picture, the extra angles and the assembly part pictures for this session">
        Pictures
        <select
          value={settings.picture_model || options.defaults.picture_model}
          disabled={busy}
          onChange={(e) => onChange({ ...settings, picture_model: e.target.value })}
        >
          {options.picture_models.map((m) => (
            <option key={m.id} value={m.id}>
              {m.name} · ${m.usd.toFixed(2)}
              {m.default ? " (default)" : ""}
            </option>
          ))}
        </select>
      </label>
      <label title={directorTitle}>
        Director
        <select
          value={director}
          disabled={busy}
          onChange={(e) => {
            if (e.target.value === ALL_MODELS) onLoadAll();
            else onChange({ ...settings, director_model: e.target.value });
          }}
        >
          {!known && <option value={director}>{director}</option>}
          {options.director_models.map((m) => (
            <option key={m.id} value={m.id}>
              {m.label}
            </option>
          ))}
          {!options.all_models && <option value={ALL_MODELS}>All vision models with prices…</option>}
        </select>
      </label>
    </span>
  );
}

type SmithMessage = UIMessage<unknown, { turn: TurnData }>;

type Me = { user: string; balance: number; local_mode: boolean; providers?: Providers | null; error?: string };

// The two accounts everything is billed to, with what each has left, linking to where you top them up.
function Accounts({ p }: { p: Providers | null | undefined }) {
  if (!p) return null;
  const cell = (label: string, href: string, v: { usd: number } | null, err?: string, extra?: string) => (
    <a
      className={`account${!v || v.usd < 2 ? " low" : ""}`}
      href={href}
      target="_blank"
      rel="noreferrer"
      title={err ? `${label}: ${err}` : `${label} balance${extra ? ` · ${extra}` : ""} · opens your ${label} billing page`}
    >
      {label} {v ? `$${v.usd.toFixed(2)}` : "?"}
    </a>
  );
  const month = p.openrouter?.key_usage_month_usd;
  return (
    <span className="accounts">
      {cell("fal.ai", "https://fal.ai/dashboard/billing", p.fal, p.errors?.fal)}
      {" · "}
      {cell("OpenRouter", "https://openrouter.ai/settings/credits", p.openrouter, p.errors?.openrouter, month !== undefined ? `$${month.toFixed(2)} used this month` : undefined)}
    </span>
  );
}

function newSessionId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : String(Date.now());
}

// A saved chat's turns become the messages the chat renders, the same shape a live turn produces.
function messagesFromTurns(turns: ChatTurn[]): SmithMessage[] {
  const out: SmithMessage[] = [];
  turns.forEach((t, i) => {
    const label = t.attachments?.length ? `${t.user}\n\n📎 ${t.attachments.map((a) => a.name).join(", ")}` : t.user;
    out.push({ id: `u-${i}`, role: "user", parts: [{ type: "text", text: label }] });
    const data: TurnData = { ...t.turn, tools: t.turn.tools ?? [], pictures: t.turn.pictures ?? [], pictures_kind: t.turn.pictures_kind ?? null };
    out.push({ id: `a-${i}`, role: "assistant", parts: [{ type: "text", text: t.reply }, { type: "data-turn", data }] });
  });
  return out;
}

function jobIdsInMessages(messages: SmithMessage[]): string[] {
  const ids: string[] = [];
  for (const m of messages) {
    for (const p of m.parts) {
      if (p.type !== "data-turn") continue;
      if (p.data.last_job && !ids.includes(p.data.last_job)) ids.push(p.data.last_job);
      for (const t of p.data.tools ?? []) {
        const hit = /"job_id":\s*"(\d{8}_\d{6}_[0-9a-f]{6})"/.exec(t.result || "");
        if (hit && !ids.includes(hit[1])) ids.push(hit[1]);
      }
    }
  }
  return ids;
}

function when(ts: number | null): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

type Loaded = { messages: SmithMessage[]; jobs: string[]; jobViews: Record<string, JobView>; settings?: Settings };

export default function Chat() {
  const [chats, setChats] = useState<ChatSummary[]>([]);
  const [sessionId, setSessionId] = useState<string>(() => newSessionId());
  const [loaded, setLoaded] = useState<Loaded>({ messages: [], jobs: [], jobViews: {} });
  const [sidebar, setSidebar] = useState(true);
  const [opening, setOpening] = useState<string | null>(null);

  const refreshChats = useCallback(() => {
    fetch("/api/chats", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : []))
      .then((list: ChatSummary[]) => setChats(list))
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    refreshChats();
  }, [refreshChats]);

  async function openChat(id: string) {
    setOpening(id);
    try {
      const r = await fetch(`/api/chats/${encodeURIComponent(id)}`, { cache: "no-store" });
      if (!r.ok) return;
      const st = (await r.json()) as ChatState;
      const jobViews: Record<string, JobView> = {};
      for (const j of st.job_views ?? []) jobViews[j.id] = j;
      setLoaded({ messages: messagesFromTurns(st.turns ?? []), jobs: st.jobs ?? [], jobViews, settings: st.settings });
      setSessionId(st.id);
    } finally {
      setOpening(null);
    }
  }

  function newChat() {
    setLoaded({ messages: [], jobs: [], jobViews: {} });
    setSessionId(newSessionId());
  }

  async function deleteChat(id: string) {
    if (!confirm("Forget this chat? The models it built and their files stay.")) return;
    await fetch(`/api/chats/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (id === sessionId) newChat();
    refreshChats();
  }

  return (
    <div className={`app${sidebar ? "" : " no-sidebar"}`}>
      <aside className="sidebar">
        <div className="sidebar-top">
          <button onClick={newChat}>New chat</button>
          <button className="ghost small" onClick={() => setSidebar(false)} title="hide">
            ‹
          </button>
        </div>
        <div className="chat-list">
          {chats.length === 0 && <p className="dim">No saved chats yet. Every chat is kept, with the models it builds.</p>}
          {chats.map((c) => (
            <div key={c.id} className={`chat-row${c.id === sessionId ? " current" : ""}${opening === c.id ? " opening" : ""}`}>
              <button className="chat-open" onClick={() => openChat(c.id)} title={c.id}>
                <span className="chat-title">{c.title}</span>
                <span className="dim chat-meta">
                  {when(c.updated)} · {c.turns} turn{c.turns === 1 ? "" : "s"}
                  {c.jobs.length ? ` · ${c.jobs.length} job${c.jobs.length === 1 ? "" : "s"}` : ""}
                </span>
              </button>
              <button className="chat-del" onClick={() => deleteChat(c.id)} title="forget this chat">
                ×
              </button>
            </div>
          ))}
        </div>
      </aside>
      {!sidebar && (
        <button className="ghost small sidebar-show" onClick={() => setSidebar(true)} title="chats">
          ›
        </button>
      )}
      <ChatSession
        key={sessionId}
        sessionId={sessionId}
        initialMessages={loaded.messages}
        initialJobs={loaded.jobs}
        jobViews={loaded.jobViews}
        initialSettings={loaded.settings}
        onTurn={refreshChats}
      />
    </div>
  );
}

function ChatSession({
  sessionId,
  initialMessages,
  initialJobs,
  jobViews,
  initialSettings,
  onTurn,
}: {
  sessionId: string;
  initialMessages: SmithMessage[];
  initialJobs: string[];
  jobViews: Record<string, JobView>;
  initialSettings?: Settings;
  onTurn: () => void;
}) {
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<(Attachment & { preview?: string })[]>([]);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [me, setMe] = useState<Me | null>(null);
  const [options, setOptions] = useState<ModelOptions | null>(null);
  const [settings, setSettings] = useState<Settings>({});
  const [selectedJob, setSelectedJob] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    // after mount (never during render, so the server and client agree): the saved chat's settings, else the browser's
    const wanted = initialSettings && Object.keys(initialSettings).length ? initialSettings : loadSettings();
    Promise.resolve().then(() => setSettings(wanted));
    fetch("/api/models", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((o: ModelOptions | null) => setOptions(o))
      .catch(() => setOptions(null));
  }, [initialSettings]);

  function loadAllModels() {
    fetch("/api/models?all=1", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((o: ModelOptions | null) => o && setOptions(o))
      .catch(() => undefined);
  }

  function changeSettings(s: Settings) {
    setSettings(s);
    try {
      localStorage.setItem(SETTINGS_KEY, JSON.stringify(s));
    } catch {
      /* private window */
    }
  }

  const transport = useMemo(() => new DefaultChatTransport<SmithMessage>({ api: "/api/chat" }), []);
  const { messages, sendMessage, status, error } = useChat<SmithMessage>({
    id: sessionId,
    transport,
    messages: initialMessages,
    onFinish: () => onTurn(),
  });
  const busy = status === "submitted" || status === "streaming";
  // jobs queued from outside this page (an agent driving the session over the API or MCP): the saved chat is
  // re-read every few seconds and any job id it has that this page has not seen joins the strip
  const [outsideJobs, setOutsideJobs] = useState<string[]>([]);
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const r = await fetch(`/api/chats/${encodeURIComponent(sessionId)}`, { cache: "no-store" });
        if (!alive || !r.ok) return;
        const st = (await r.json()) as ChatState;
        const ids = st.jobs ?? [];
        if (alive && ids.length) setOutsideJobs((prev) => (ids.every((id) => prev.includes(id)) ? prev : ids));
      } catch {
        /* the API is away; try again next tick */
      }
    };
    const timer = setInterval(tick, 8000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [sessionId]);

  useEffect(() => {
    fetch("/api/me", { cache: "no-store" })
      .then((r) => r.json())
      .then(setMe)
      .catch((e) => setMe({ user: "?", balance: 0, local_mode: true, error: String(e) }));
  }, []);

  // every job this chat made (saved ones first, then anything new this session); the panel follows the newest unless
  // the customer picked one from the strip
  const latest = useMemo(() => {
    let balance: number | null = null;
    let providers: Providers | null = null;
    for (let i = messages.length - 1; i >= 0; i--) {
      const turn = messages[i].parts.find((p) => p.type === "data-turn");
      if (turn && turn.type === "data-turn") {
        balance = turn.data.balance;
        providers = turn.data.providers;
        break;
      }
    }
    const jobs = [...initialJobs];
    for (const id of jobIdsInMessages(messages)) if (!jobs.includes(id)) jobs.push(id);
    for (const id of outsideJobs) if (!jobs.includes(id)) jobs.push(id);
    return { balance, providers, jobs };
  }, [messages, initialJobs, outsideJobs]);
  const jobId = selectedJob && latest.jobs.includes(selectedJob) ? selectedJob : latest.jobs[latest.jobs.length - 1] ?? null;
  const balance = latest.balance ?? me?.balance ?? 0;
  const accounts = latest.providers ?? me?.providers;

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function upload(files: FileList | null) {
    if (!files?.length) return;
    setUploading(true);
    try {
      for (const f of Array.from(files)) {
        const fd = new FormData();
        fd.append("file", f, f.name);
        const r = await fetch("/api/upload", { method: "POST", body: fd });
        const j = (await r.json()) as Attachment & { detail?: string; error?: string };
        if (!r.ok) {
          alert(j.detail || j.error || `upload failed (${r.status})`);
          continue;
        }
        const preview = f.type.startsWith("image/") ? URL.createObjectURL(f) : undefined;
        setAttachments((a) => [...a, { path: j.path, name: j.name, kind: j.kind, bytes: j.bytes, preview }]);
      }
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  async function send() {
    const t = text.trim();
    if ((!t && attachments.length === 0) || busy) return;
    const msg = t || (attachments.some((a) => a.kind === "mesh") ? "Import this model." : "Use these pictures.");
    const label = attachments.length ? `${msg}\n\n📎 ${attachments.map((a) => a.name).join(", ")}` : msg;
    setText("");
    const sent = attachments.map(({ path, name, kind }) => ({ path, name, kind }));
    setAttachments([]);
    await sendMessage({ text: label }, { body: { attachments: sent, settings } });
  }

  return (
    <div className="shell">
      <header className="top">
        <div className="brand">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img className="logo" src="/logo.png" alt="" width={48} height={48} />
          <div>
            <h1>Master Smith</h1>
            <p className="dim">Prompt in, game-ready 3D model out. Describe an asset, or attach a model to finish it.</p>
          </div>
        </div>
        <div className="me">
          <ModelPicker options={options} settings={settings} onChange={changeSettings} onLoadAll={loadAllModels} busy={busy} />
          {me?.error ? (
            <span className="error-inline">API offline: {me.error}</span>
          ) : me ? (
            <span className="dim">
              <Accounts p={accounts} />
              {accounts ? " · " : ""}
              {me.user}
              {me.local_mode ? " (local)" : ""} · spent here ${(-balance / 100).toFixed(2)}
            </span>
          ) : null}
        </div>
      </header>

      <main className="stack">
        <JobPanel jobId={jobId} jobs={latest.jobs} jobViews={jobViews} onSelect={setSelectedJob} />
        <section className="chat">
          <div className="log">
            {messages.length === 0 && (
              <div className="msg assistant">
                <p>
                  Tell me what to build: what it is, its materials and colours, how big it is, and which engine. I will
                  write the brief, draw the reference pictures for you to approve, and buy the mesh when you say go.
                  Attach a <code>.glb</code>, <code>.fbx</code>, <code>.obj</code> or <code>.blend</code> to finish a
                  model you already have. Every chat is saved with the models it builds; find them on the left.
                </p>
              </div>
            )}
            {messages.map((m, mi) => (
              <div key={m.id} className={`msg ${m.role}`}>
                {m.parts.map((p, i) => {
                  if (p.type === "text") return m.role === "assistant" ? <Md key={i} text={p.text} /> : <p key={i}>{p.text}</p>;
                  if (p.type === "data-turn")
                    return (
                      <div key={i}>
                        {p.data.question && mi === messages.length - 1 && (
                          <Options
                            q={p.data.question}
                            busy={busy}
                            onPick={(answer) => sendMessage({ text: answer }, { body: { attachments: [], settings } })}
                          />
                        )}
                        {p.data.pictures?.length > 0 && (p.data.pictures_kind ?? "reference") === "reference" && (
                          <Pictures
                            urls={p.data.pictures}
                            onApprove={() =>
                              sendMessage({ text: "Go: build from this picture." }, { body: { attachments: [], settings } })
                            }
                            onChange={() => document.querySelector<HTMLTextAreaElement>(".composer textarea")?.focus()}
                            busy={busy}
                          />
                        )}
                        <TurnCard turn={p.data} />
                      </div>
                    );
                  return null;
                })}
              </div>
            ))}
            {busy && <div className="msg assistant dim">thinking…</div>}
            {error && <div className="msg error">{error.message}</div>}
            <div ref={bottom} />
          </div>

          <form
            className={`composer${dragging ? " dragging" : ""}`}
            onSubmit={(e) => {
              e.preventDefault();
              send();
            }}
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              upload(e.dataTransfer.files);
            }}
          >
            {attachments.length > 0 && (
              <div className="chips">
                {attachments.map((a) => (
                  <span key={a.path} className="chip">
                    {a.preview ? (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img src={a.preview} alt="" />
                    ) : (
                      "🧊"
                    )}{" "}
                    {a.name}
                    <button type="button" onClick={() => setAttachments((x) => x.filter((y) => y.path !== a.path))}>
                      ×
                    </button>
                  </span>
                ))}
              </div>
            )}
            <div className="row">
              <input
                ref={fileInput}
                type="file"
                multiple
                accept="image/*,.png,.jpg,.jpeg,.jfif,.webp,.bmp,.gif,.tif,.tiff,.avif,.heic,.glb,.gltf,.fbx,.obj,.blend"
                onChange={(e) => upload(e.target.files)}
                hidden
                id="file"
              />
              <button type="button" className="ghost" onClick={() => fileInput.current?.click()} disabled={uploading || busy}>
                {uploading ? "uploading…" : "Attach"}
              </button>
              <textarea
                value={text}
                onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    send();
                  }
                }}
                onPaste={(e) => {
                  const files = Array.from(e.clipboardData.items)
                    .filter((it) => it.kind === "file")
                    .map((it) => it.getAsFile())
                    .filter((f): f is File => !!f);
                  if (files.length) {
                    e.preventDefault();
                    const dt = new DataTransfer();
                    files.forEach((f) => dt.items.add(f));
                    upload(dt.files);
                  }
                }}
                placeholder="a weathered oak ammunition crate with rope handles and black stencils, 1.2 m, Unreal — or drop / paste pictures and model files here"
                rows={2}
              />
              <button type="submit" disabled={busy || uploading}>
                Send
              </button>
            </div>
          </form>
        </section>
      </main>
    </div>
  );
}

function Pictures({
  urls,
  onApprove,
  onChange,
  busy,
}: {
  urls: { label: string; url: string }[];
  onApprove: () => void;
  onChange: () => void;
  busy: boolean;
}) {
  return (
    <div className="pictures">
      <div className="pictures-row">
        {urls.map((p, i) => {
          const src = p.url.replace(/^\/v1\//, "/api/");
          const caption = i === 0 ? "reference" : p.label.replace(/^orthographic /, "");
          return (
            <figure key={p.url}>
              <a href={src} target="_blank" rel="noreferrer">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={src} alt={p.label} title={p.label} />
              </a>
              <figcaption className="dim">
                {caption}{" "}
                <a href={src} download={src.split("/").pop()} title="save this picture">
                  ⤓
                </a>
              </figcaption>
            </figure>
          );
        })}
      </div>
      <div className="pictures-actions">
        <button type="button" onClick={onApprove} disabled={busy}>
          Build from this
        </button>
        <button type="button" className="ghost" onClick={onChange} disabled={busy}>
          Change something…
        </button>
        <span className="dim">
          The mesh is bought only after you approve the picture.
        </span>
      </div>
    </div>
  );
}

function TurnCard({ turn }: { turn: TurnData }) {
  const b = turn.brief;
  const tools = (turn.tools ?? []).filter((t) => t.name !== "balance");
  if (!b && tools.length === 0) return null;
  return (
    <details className="turn">
      <summary>
        {b ? `Brief: ${String(b.name)} · ${String(b.category)} · ${String(b.style)} · ${Number(b.tri_budget).toLocaleString()} tris · ${Number(b.size_m) > 0 ? `${b.size_m} m` : "source size"}` : "tools"}
        {turn.last_job ? ` · job ${turn.last_job}` : ""}
        {` · chat $${(turn.chat_cost_usd ?? 0).toFixed(4)}`}
      </summary>
      {tools.map((t, i) => (
        <div key={i} className="tool">
          <code>{t.name}</code> <span className="dim">{JSON.stringify(t.args).slice(0, 240)}</span>
          <div className="dim mono">{t.result}</div>
        </div>
      ))}
    </details>
  );
}
