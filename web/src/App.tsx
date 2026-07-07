import { Component, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { api } from "./api";
import {
  DEFAULT_TENANT,
  DEFAULT_WORKSPACE,
  type ConflictCase,
  type MemoryItem,
  type ModelSettings,
  type RecallResult,
  type VaultFile
} from "./types";

type View = "dashboard" | "memory" | "inbox" | "vault" | "graph" | "settings";

const layers = ["core", "semantic", "episodic", "procedural", "reflection", "error", "social"] as const;

export function App() {
  return (
    <ErrorBoundary>
      <Dashboard />
    </ErrorBoundary>
  );
}

class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <div className="fatal-screen">
          <div>
            <b>UI runtime error</b>
            <h1>Dashboard не должен быть пустым</h1>
            <p>{this.state.error.message}</p>
            <button onClick={() => location.reload()}>Reload</button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

function Dashboard() {
  const [view, setView] = useState<View>("dashboard");
  const [tenant, setTenant] = useState(DEFAULT_TENANT);
  const [workspace, setWorkspace] = useState(DEFAULT_WORKSPACE);
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [conflicts, setConflicts] = useState<ConflictCase[]>([]);
  const [vault, setVault] = useState<VaultFile[]>([]);
  const [settings, setSettings] = useState<ModelSettings | null>(null);
  const [selectedMemory, setSelectedMemory] = useState<string | null>(null);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [query, setQuery] = useState("Что важно знать текущему агенту?");
  const [recall, setRecall] = useState<RecallResult[]>([]);
  const [draftMemory, setDraftMemory] = useState("");
  const [status, setStatus] = useState("Готов");
  const [loading, setLoading] = useState(false);

  async function refresh() {
    setLoading(true);
    try {
      const [memoryData, conflictData, vaultData, modelData] = await Promise.all([
        api.memories(workspace, tenant),
        api.conflicts(workspace, tenant),
        api.vault(workspace, tenant),
        api.modelSettings()
      ]);
      setMemories(memoryData.memories);
      setConflicts(conflictData.cases);
      setVault(vaultData.files);
      setSettings(modelData);
      setSelectedMemory((current) => current ?? memoryData.memories[0]?.id ?? null);
      setSelectedFile((current) => current ?? vaultData.files[0]?.path ?? null);
      setStatus("Данные обновлены");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Неизвестная ошибка");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, [tenant, workspace]);

  const activeMemories = memories.filter((item) => item.status === "active");
  const openConflicts = conflicts.filter(isOpenConflict);
  const selected = memories.find((item) => item.id === selectedMemory) ?? memories[0];
  const selectedVault = vault.find((item) => item.path === selectedFile) ?? vault[0];

  const kpis = [
    ["Memories", memories.length.toLocaleString(), `+${activeMemories.length} active`, "db"],
    ["Conflicts", openConflicts.length.toLocaleString(), "need review", "scale"],
    ["Vault files", vault.length.toLocaleString(), "plain text editable", "folder"],
    ["Live status", "Online", settings?.runtime.model_name ?? "runtime", "pulse"]
  ];

  async function runRecall() {
    setStatus("Recall...");
    const response = await api.recall(workspace, tenant, query);
    setRecall(response.results);
    setStatus(`Найдено ${response.results.length} воспоминаний`);
  }

  async function addMemory() {
    if (!draftMemory.trim()) return;
    setStatus("Сохраняю память...");
    await api.retain(workspace, tenant, draftMemory.trim());
    setDraftMemory("");
    await refresh();
  }

  async function runOperation(name: "reflect" | "reindex") {
    setStatus(name === "reflect" ? "Reflection запущен..." : "Пересчитываю embeddings...");
    const result = name === "reflect" ? await api.reflect(workspace, tenant) : await api.reindex(workspace, tenant);
    setStatus(JSON.stringify(result));
    await refresh();
  }

  return (
    <div className="app-shell">
      <Sidebar view={view} setView={setView} conflicts={openConflicts.length} />
      <main className="main">
        <Hero
          tenant={tenant}
          workspace={workspace}
          setTenant={setTenant}
          setWorkspace={setWorkspace}
          loading={loading}
        />
        <section className="kpi-row">
          {kpis.map(([label, value, hint, icon]) => (
            <KpiCard key={label} label={label} value={value} hint={hint} icon={icon} />
          ))}
        </section>

        <section className="content-grid">
          <div className="panel memory-panel">
            <PanelHeader
              title={view === "settings" ? "Model Settings" : "Recent Memories"}
              action={<button onClick={() => void refresh()}>Refresh</button>}
            />
            {view === "settings" ? (
              <SettingsPanel settings={settings} setStatus={setStatus} refresh={refresh} />
            ) : view === "vault" ? (
              <VaultEditor
                files={vault}
                selectedPath={selectedVault?.path}
                tenant={tenant}
                workspace={workspace}
                setSelectedFile={setSelectedFile}
                setStatus={setStatus}
                refresh={refresh}
              />
            ) : view === "inbox" ? (
              <ConflictList conflicts={conflicts} />
            ) : (
              <MemoryList memories={memories} selectedId={selected?.id} onSelect={setSelectedMemory} />
            )}
          </div>

          <div className="panel graph-panel">
            <PanelHeader
              title={view === "memory" ? "Recall" : "Memory Graph"}
              action={<button onClick={() => setView("graph")}>Expand</button>}
            />
            {view === "memory" ? (
              <RecallPanel query={query} setQuery={setQuery} runRecall={runRecall} results={recall} />
            ) : (
              <MemoryGraph memories={memories} selectedId={selected?.id} onSelect={setSelectedMemory} />
            )}
          </div>

          <aside className="panel operations-panel">
            <PanelHeader title="Operations" />
            <div className="operation-list">
              <button className="operation purple" onClick={() => void runOperation("reflect")}>
                <span>✳</span>
                <b>Reflect</b>
                <small>Синтезировать наблюдения</small>
              </button>
              <button className="operation blue" onClick={() => void runOperation("reindex")}>
                <span>⌬</span>
                <b>Reindex</b>
                <small>Пересчитать embeddings</small>
              </button>
              <button className="operation pink" onClick={() => setView("inbox")}>
                <span>▣</span>
                <b>Inbox</b>
                <small>{openConflicts.length} конфликтов</small>
              </button>
            </div>
            <div className="composer">
              <label>Новая память</label>
              <textarea
                value={draftMemory}
                onChange={(event) => setDraftMemory(event.target.value)}
                placeholder="Обычный текст. Embedding будет пересчитан под капотом."
              />
              <button onClick={() => void addMemory()}>Сохранить память</button>
            </div>
          </aside>

          <div className="panel vault-preview">
            <PanelHeader title="Vault Preview" action={<button onClick={() => setView("vault")}>Edit</button>} />
            <VaultPreview files={vault} selectedPath={selectedVault?.path} setSelectedFile={setSelectedFile} />
          </div>

          <div className="panel conflict-panel">
            <PanelHeader title="Conflict Review Inbox" badge={openConflicts.length} />
            <ConflictList conflicts={conflicts.slice(0, 4)} compact />
          </div>

          <aside className="panel activity-panel">
            <PanelHeader title="Activity Log" />
            <ActivityLog memories={memories} conflicts={conflicts} status={status} />
          </aside>
        </section>
      </main>
    </div>
  );
}

function Sidebar({ view, setView, conflicts }: { view: View; setView: (view: View) => void; conflicts: number }) {
  const items: Array<[View, string, string]> = [
    ["dashboard", "Dashboard", "◈"],
    ["memory", "Memory", "✦"],
    ["inbox", "Inbox", "□"],
    ["vault", "Vault", "▤"],
    ["graph", "Graph", "◎"],
    ["settings", "Settings", "⚙"]
  ];
  return (
    <aside className="sidebar" role="navigation">
      <div className="brand"><span className="brand-mark">◌</span><b>UAM</b></div>
      <nav>
        {items.map(([key, label, icon]) => (
          <button key={key} className={view === key ? "active" : ""} onClick={() => setView(key)}>
            <span>{icon}</span>
            {label}
            {key === "inbox" && conflicts > 0 ? <em>{conflicts}</em> : null}
          </button>
        ))}
      </nav>
      <div className="health-card">
        <b>System Health</b>
        <span className="pill green">Healthy</span>
        <small>Version 0.2.1</small>
        <div className="meter"><i style={{ width: "72%" }} /></div>
        <small>CPU 18% · RAM 32%</small>
      </div>
    </aside>
  );
}

function Hero(props: {
  tenant: string;
  workspace: string;
  setTenant: (value: string) => void;
  setWorkspace: (value: string) => void;
  loading: boolean;
}) {
  return (
    <header className="hero">
      <div>
        <p className="eyebrow">Self-hosted · Agent memory plane</p>
        <h1>Universal Agent Memory</h1>
        <p>Единый слой долговременной памяти для OpenClaw, Hermes и других агентов.</p>
      </div>
      <div className="identity-card">
        <label>Tenant</label>
        <input value={props.tenant} onChange={(event) => props.setTenant(event.target.value)} />
        <label>Workspace</label>
        <input value={props.workspace} onChange={(event) => props.setWorkspace(event.target.value)} />
        <span className={props.loading ? "sync loading" : "sync"}>{props.loading ? "Syncing" : "Live"}</span>
      </div>
    </header>
  );
}

function KpiCard({ label, value, hint, icon }: { label: string; value: string; hint: string; icon: string }) {
  return (
    <article className={`kpi icon-${icon}`}>
      <div className="kpi-icon">{icon === "db" ? "▱" : icon === "scale" ? "⚖" : icon === "folder" ? "▰" : "∿"}</div>
      <div>
        <small>{label}</small>
        <strong>{value}</strong>
        <span>{hint}</span>
      </div>
      <svg viewBox="0 0 120 42" aria-hidden="true">
        <path d="M2 35 C18 26 24 38 38 24 S58 30 72 15 S94 22 118 5" />
      </svg>
    </article>
  );
}

function PanelHeader({ title, action, badge }: { title: string; action?: ReactNode; badge?: number }) {
  return (
    <div className="panel-header">
      <h2>{title}{badge !== undefined ? <span>{badge}</span> : null}</h2>
      {action}
    </div>
  );
}

function MemoryList({ memories, selectedId, onSelect }: { memories: MemoryItem[]; selectedId?: string; onSelect: (id: string) => void }) {
  return (
    <div className="memory-list">
      {memories.length === 0 ? <Empty text="Памяти пока нет. Добавь первую справа." /> : null}
      {memories.map((item) => (
        <button key={item.id} className={item.id === selectedId ? "memory-card selected" : "memory-card"} onClick={() => onSelect(item.id)}>
          <div>
            <b>{item.text.slice(0, 92)}{item.text.length > 92 ? "…" : ""}</b>
            <p>{item.kind} · rev {item.revision} · confidence {Math.round(item.confidence * 100)}%</p>
          </div>
          <span className={`tag ${item.layer}`}>{item.layer}</span>
          <span className={`tag ${item.status}`}>{item.status}</span>
        </button>
      ))}
    </div>
  );
}

function RecallPanel({ query, setQuery, runRecall, results }: {
  query: string;
  setQuery: (value: string) => void;
  runRecall: () => Promise<void>;
  results: RecallResult[];
}) {
  return (
    <div className="recall-panel">
      <textarea value={query} onChange={(event) => setQuery(event.target.value)} />
      <button onClick={() => void runRecall()}>Run recall</button>
      <div className="recall-results">
        {results.map((item) => (
          <article key={item.id}>
            <b>{item.layer} · {item.source}</b>
            <p>{item.text}</p>
            <small>score {item.score.toFixed(3)}</small>
          </article>
        ))}
      </div>
    </div>
  );
}

function MemoryGraph({ memories, selectedId, onSelect }: { memories: MemoryItem[]; selectedId?: string; onSelect: (id: string) => void }) {
  const nodes = useMemo(() => {
    const source = memories.slice(0, 18);
    const fallback = source.length ? source : layers.map((layer, index) => ({
      id: layer,
      layer,
      text: `${layer} memory`,
      labels: [],
      status: "active",
      confidence: 0.8,
      revision: 1,
      scope: "workspace",
      kind: "placeholder",
      supersedes_id: null,
      created_at: new Date().toISOString()
    } as MemoryItem));
    return fallback.map((item, index) => {
      const angle = (Math.PI * 2 * index) / Math.max(1, fallback.length);
      const ring = index < 1 ? 0 : 170 + (index % 3) * 38;
      return {
        ...item,
        x: 350 + Math.cos(angle) * ring,
        y: 250 + Math.sin(angle) * ring
      };
    });
  }, [memories]);
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>({});
  const [dragging, setDragging] = useState<string | null>(null);
  const resolved = nodes.map((node) => ({ ...node, ...(positions[node.id] ?? {}) }));
  const center = resolved[0];

  return (
    <svg
      className="graph"
      viewBox="0 0 700 500"
      onPointerMove={(event) => {
        if (!dragging) return;
        const svg = event.currentTarget;
        const point = svg.createSVGPoint();
        point.x = event.clientX;
        point.y = event.clientY;
        const mapped = point.matrixTransform(svg.getScreenCTM()?.inverse());
        setPositions((current) => ({ ...current, [dragging]: { x: mapped.x, y: mapped.y } }));
      }}
      onPointerUp={() => setDragging(null)}
      onPointerLeave={() => setDragging(null)}
      role="img"
      aria-label="Obsidian-style interactive memory graph"
    >
      <defs>
        <radialGradient id="nodeGlow">
          <stop offset="0%" stopColor="#2e9bff" />
          <stop offset="70%" stopColor="#101c59" />
          <stop offset="100%" stopColor="#070b1b" />
        </radialGradient>
      </defs>
      {center ? resolved.slice(1).map((node) => (
        <line key={`${center.id}-${node.id}`} x1={center.x} y1={center.y} x2={node.x} y2={node.y} />
      )) : null}
      {resolved.map((node, index) => (
        <g
          key={node.id}
          className={node.id === selectedId ? "graph-node selected" : "graph-node"}
          transform={`translate(${node.x},${node.y})`}
          onPointerDown={(event) => {
            event.currentTarget.setPointerCapture(event.pointerId);
            setDragging(node.id);
            onSelect(node.id);
          }}
        >
          <circle r={index === 0 ? 68 : 42} />
          <text>{node.text.slice(0, index === 0 ? 42 : 18)}</text>
          <text y="15" className="node-layer">{node.layer}</text>
        </g>
      ))}
    </svg>
  );
}

function VaultPreview({ files, selectedPath, setSelectedFile }: { files: VaultFile[]; selectedPath?: string; setSelectedFile: (path: string) => void }) {
  const file = files.find((item) => item.path === selectedPath) ?? files[0];
  return (
    <div className="vault-preview-grid">
      <div className="file-tree">
        {files.slice(0, 10).map((item) => (
          <button key={item.path} className={item.path === file?.path ? "active" : ""} onClick={() => setSelectedFile(item.path)}>
            {item.path}
          </button>
        ))}
      </div>
      <pre>{stripFrontmatter(file?.content ?? "Vault пуст. Сохрани первую память.")}</pre>
    </div>
  );
}

function VaultEditor(props: {
  files: VaultFile[];
  selectedPath?: string;
  tenant: string;
  workspace: string;
  setSelectedFile: (path: string) => void;
  setStatus: (status: string) => void;
  refresh: () => Promise<void>;
}) {
  const file = props.files.find((item) => item.path === props.selectedPath) ?? props.files[0];
  const [text, setText] = useState(stripFrontmatter(file?.content ?? ""));
  useEffect(() => setText(stripFrontmatter(file?.content ?? "")), [file?.path, file?.content]);
  const canEdit = file && !file.path.includes("embedding") && !file.path.endsWith("index.md");

  async function save(dryRun: boolean) {
    if (!file || !canEdit) return;
    const next = replaceBody(file.content, text);
    const result = await api.importVault(props.workspace, props.tenant, [{ path: file.path, content: next }], dryRun);
    props.setStatus(`${dryRun ? "Dry-run" : "Saved"}: ${result.supersede_count} supersede, ${result.changes.length} changes`);
    if (!dryRun) {
      await api.reindex(props.workspace, props.tenant);
      await props.refresh();
    }
  }

  return (
    <div className="vault-editor">
      <div className="file-rail">
        {props.files.map((item) => (
          <button key={item.path} className={item.path === file?.path ? "active" : ""} onClick={() => props.setSelectedFile(item.path)}>
            {item.path}
          </button>
        ))}
      </div>
      <div className="editor-pane">
        <p className="hint">Редактируй обычный текст памяти. Frontmatter, ревизии и embedding остаются под капотом.</p>
        <textarea value={text} disabled={!canEdit} onChange={(event) => setText(event.target.value)} />
        <div className="actions">
          <button onClick={() => void save(true)}>Dry-run</button>
          <button className="primary" onClick={() => void save(false)}>Сохранить и пересчитать embedding</button>
        </div>
      </div>
    </div>
  );
}

function SettingsPanel({ settings, setStatus, refresh }: {
  settings: ModelSettings | null;
  setStatus: (status: string) => void;
  refresh: () => Promise<void>;
}) {
  const [form, setForm] = useState({
    provider: settings?.desired.provider ?? "openai_compatible",
    model_name: settings?.desired.model_name ?? "jina-embeddings-v2-base-code",
    dimension: settings?.desired.dimension ?? 768,
    base_url: settings?.desired.base_url ?? "http://127.0.0.1:8081/v1",
    api_key: "",
    timeout_seconds: settings?.desired.timeout_seconds ?? 30
  });
  useEffect(() => {
    if (!settings) return;
    setForm((current) => ({
      ...current,
      provider: settings.desired.provider,
      model_name: settings.desired.model_name,
      dimension: settings.desired.dimension,
      base_url: settings.desired.base_url ?? "",
      timeout_seconds: settings.desired.timeout_seconds
    }));
  }, [settings]);

  async function save(testOnly: boolean) {
    const body = {
      ...form,
      dimension: Number(form.dimension),
      timeout_seconds: Number(form.timeout_seconds),
      base_url: form.base_url || null,
      api_key: form.api_key || null
    };
    const result = testOnly ? await api.testModelSettings(body) : await api.saveModelSettings(body);
    setStatus(JSON.stringify(result));
    if (!testOnly) await refresh();
  }

  return (
    <div className="settings-grid">
      {(["provider", "model_name", "base_url", "api_key"] as const).map((key) => (
        <label key={key}>
          {key}
          <input
            type={key === "api_key" ? "password" : "text"}
            value={String(form[key])}
            onChange={(event) => setForm((current) => ({ ...current, [key]: event.target.value }))}
          />
        </label>
      ))}
      <label>
        dimension
        <input type="number" value={form.dimension} onChange={(event) => setForm((current) => ({ ...current, dimension: Number(event.target.value) }))} />
      </label>
      <label>
        timeout_seconds
        <input type="number" value={form.timeout_seconds} onChange={(event) => setForm((current) => ({ ...current, timeout_seconds: Number(event.target.value) }))} />
      </label>
      <div className="settings-summary">
        <b>Runtime</b>
        <p>{settings?.runtime.provider} · {settings?.runtime.model_name} · {settings?.runtime.dimension} dims</p>
        <small>restart_required: {settings?.restart_required ? "да" : "нет"}</small>
      </div>
      <div className="actions">
        <button onClick={() => void save(true)}>Test endpoint</button>
        <button className="primary" onClick={() => void save(false)}>Save model config</button>
      </div>
    </div>
  );
}

function ConflictList({ conflicts, compact = false }: { conflicts: ConflictCase[]; compact?: boolean }) {
  return (
    <div className={compact ? "conflicts compact" : "conflicts"}>
      {conflicts.length === 0 ? <Empty text="Конфликтов нет." /> : null}
      {conflicts.map((item) => (
        <article key={item.id}>
          <b>{conflictTitle(item)}</b>
          <p>{conflictValues(item).join(" vs ")}</p>
          {!compact ? <small>{conflictRationale(item)}</small> : null}
          <span className="pill amber">{item.review_status}</span>
        </article>
      ))}
    </div>
  );
}

function ActivityLog({ memories, conflicts, status }: { memories: MemoryItem[]; conflicts: ConflictCase[]; status: string }) {
  const events = [
    ["✦", status, "just now"],
    ["▱", `${memories.length} memories indexed`, "live"],
    ["⚖", `${conflicts.length} conflicts tracked`, "live"],
    ["▤", "Vault plain-text mode", "ready"],
    ["◎", "Graph nodes draggable", "ready"]
  ];
  return (
    <div className="activity">
      {events.map(([icon, text, time]) => (
        <article key={text}>
          <span>{icon}</span>
          <p>{text}</p>
          <small>{time}</small>
        </article>
      ))}
    </div>
  );
}

function Empty({ text }: { text: string }) {
  return <div className="empty">{text}</div>;
}

function isOpenConflict(item: ConflictCase) {
  return item.review_status === "pending" || item.review_status === "unresolved";
}

function conflictTitle(item: ConflictCase) {
  if (item.key) return item.key;
  return [item.subject, item.predicate].filter(Boolean).join(" · ") || "memory conflict";
}

function conflictValues(item: ConflictCase) {
  if (Array.isArray(item.values) && item.values.length > 0) return item.values;
  const values = item.candidates?.map((candidate) => candidate.value).filter(Boolean) ?? [];
  if (values.length > 0) return values;
  return item.suggested_winner_value ? [item.suggested_winner_value] : ["needs review"];
}

function conflictRationale(item: ConflictCase) {
  return item.rationale ?? item.suggested_reason ?? "Сервер предлагает самую свежую активную версию, исходные memories остаются append-only.";
}

function stripFrontmatter(content: string) {
  return content.replace(/^---[\s\S]*?---\s*/, "").trim();
}

function replaceBody(original: string, body: string) {
  const match = original.match(/^---[\s\S]*?---\s*/);
  return `${match?.[0] ?? ""}${body.trim()}\n`;
}
