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
  type SystemStatus,
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
            <b>Ошибка интерфейса</b>
            <h1>Панель не должна быть пустой</h1>
            <p>{this.state.error.message}</p>
            <button onClick={() => location.reload()}>Перезагрузить</button>
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
  const [systemStatus, setSystemStatus] = useState<SystemStatus | null>(null);
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
      const [memoryData, conflictData, vaultData, modelData, statusData] = await Promise.all([
        api.memories(workspace, tenant),
        api.conflicts(workspace, tenant),
        api.vault(workspace, tenant),
        api.modelSettings(),
        api.systemStatus()
      ]);
      setMemories(memoryData.memories);
      setConflicts(conflictData.cases);
      setVault(vaultData.files);
      setSettings(modelData);
      setSystemStatus(statusData);
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
    ["Память", memories.length.toLocaleString(), `активных: ${activeMemories.length}`, "db"],
    ["Конфликты", openConflicts.length.toLocaleString(), "на проверку", "scale"],
    ["Файлы", vault.length.toLocaleString(), "редактируемый текст", "folder"],
    ["Статус", systemStatus?.status === "ok" ? "Онлайн" : "н/д", settings?.runtime.model_name ?? "runtime", "pulse"]
  ];

  async function runRecall() {
    setStatus("Ищу в памяти...");
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
    setStatus(name === "reflect" ? "Рефлексия запущена..." : "Пересчитываю векторы...");
    const result = name === "reflect" ? await api.reflect(workspace, tenant) : await api.reindex(workspace, tenant);
    setStatus(JSON.stringify(result));
    await refresh();
  }

  async function decideConflict(
    item: ConflictCase,
    status: "accepted" | "overridden" | "dismissed",
    winnerValue: string | null,
    reason: string
  ) {
    setStatus("Сохраняю решение по конфликту...");
    await api.decideConflict(workspace, tenant, item.id, {
      status,
      winner_value: winnerValue,
      reason
    });
    setStatus(status === "dismissed" ? "Конфликт скрыт как неактуальный" : `Конфликт решён: ${winnerValue ?? "без победителя"}`);
    await refresh();
  }

  return (
    <div className="app-shell">
      <Sidebar view={view} setView={setView} conflicts={openConflicts.length} systemStatus={systemStatus} />
      <main className="main">
        <Hero
          tenant={tenant}
          workspace={workspace}
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
              title={view === "settings" ? "Настройки моделей" : "Последние воспоминания"}
              action={<button onClick={() => void refresh()}>Обновить</button>}
            />
            <TabStrip view={view} setView={setView} />
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
              <ConflictList conflicts={conflicts} onDecide={decideConflict} />
            ) : (
              <MemoryList memories={memories} selectedId={selected?.id} onSelect={setSelectedMemory} />
            )}
          </div>

          <div className="panel graph-panel">
            <PanelHeader
              title={view === "memory" ? "Поиск по памяти" : "Граф памяти"}
              action={<button onClick={() => setView("graph")}>Развернуть</button>}
            />
            {view === "memory" ? (
              <RecallPanel query={query} setQuery={setQuery} runRecall={runRecall} results={recall} />
            ) : (
              <MemoryGraph memories={memories} selectedId={selected?.id} onSelect={setSelectedMemory} />
            )}
          </div>

          <aside className="panel operations-panel">
            <PanelHeader title="Операции" />
            <div className="operation-list">
              <button className="operation purple" onClick={() => void runOperation("reflect")}>
                <span>✳</span>
                <b>Рефлексия</b>
                <small>Синтезировать наблюдения</small>
              </button>
              <button className="operation blue" onClick={() => void runOperation("reindex")}>
                <span>⌬</span>
                <b>Переиндексация</b>
                <small>Пересчитать векторы</small>
              </button>
              <button className="operation pink" onClick={() => setView("inbox")}>
                <span>▣</span>
                <b>Входящие</b>
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
            <PanelHeader title="Предпросмотр vault" action={<button onClick={() => setView("vault")}>Редактировать</button>} />
            <VaultPreview files={vault} selectedPath={selectedVault?.path} setSelectedFile={setSelectedFile} />
          </div>

          <div className="panel conflict-panel">
            <PanelHeader title="Разбор конфликтов" badge={openConflicts.length} />
            <ConflictList conflicts={conflicts.slice(0, 4)} compact onDecide={decideConflict} />
          </div>

          <aside className="panel activity-panel">
            <PanelHeader title="Журнал активности" />
            <ActivityLog memories={memories} conflicts={conflicts} status={status} />
          </aside>
        </section>
      </main>
    </div>
  );
}

function Sidebar({ view, setView, conflicts, systemStatus }: { view: View; setView: (view: View) => void; conflicts: number; systemStatus: SystemStatus | null }) {
  const overviewItems: Array<[View, string, string]> = [
    ["dashboard", "Панель", "◈"],
    ["memory", "Память", "✦"],
    ["inbox", "Входящие", "□"]
  ];
  const systemItems: Array<[View, string, string]> = [
    ["graph", "Граф памяти", "◎"],
    ["vault", "Хранилище", "▤"],
    ["settings", "Настройки", "⚙"]
  ];
  return (
    <aside className="sidebar" role="navigation">
      <div className="brand">
        <span className="brand-mark">◌</span>
        <span><b>UAM</b><small>слой памяти</small></span>
      </div>
      <span className="nav-label">Обзор</span>
      <nav>
        {overviewItems.map(([key, label, icon]) => (
          <button key={key} className={view === key ? "active" : ""} onClick={() => setView(key)}>
            <span>{icon}</span>
            {label}
            {key === "inbox" && conflicts > 0 ? <em>{conflicts}</em> : null}
          </button>
        ))}
      </nav>
      <span className="nav-label">Система</span>
      <nav>
        {systemItems.map(([key, label, icon]) => (
          <button key={key} className={view === key ? "active" : ""} onClick={() => setView(key)}>
            <span>{icon}</span>
            {label}
          </button>
        ))}
      </nav>
      <HealthCard systemStatus={systemStatus} />
    </aside>
  );
}

function HealthCard({ systemStatus }: { systemStatus: SystemStatus | null }) {
  const storagePercent = systemStatus?.storage.used_percent ?? 0;
  return (
    <div className="health-card">
      <div className="health-head">
        <b>Состояние</b>
        <span className={systemStatus?.status === "ok" ? "pill green" : "pill amber"}>
          {systemStatus?.status === "ok" ? "Работает" : "н/д"}
        </span>
      </div>
      <small>
        Версия {systemStatus?.version ?? "н/д"} · uptime {formatDuration(systemStatus?.uptime_seconds)}
      </small>
      <label>Диск сервера <em>{formatBytes(systemStatus?.storage.used_bytes)} / {formatBytes(systemStatus?.storage.total_bytes)}</em></label>
      <div className="meter"><i style={{ width: `${Math.min(100, storagePercent)}%` }} /></div>
      <label>Load 1m <em>{formatNumber(systemStatus?.load_average.one_minute)}</em></label>
      <label>RSS процесса <em>{systemStatus?.process.rss_mb != null ? `${systemStatus.process.rss_mb} MiB` : "н/д"}</em></label>
      <small>PID {systemStatus?.process.pid ?? "н/д"} · {systemStatus?.storage.path ?? "путь н/д"}</small>
    </div>
  );
}

function Hero(props: {
  tenant: string;
  workspace: string;
  loading: boolean;
}) {
  return (
    <header className="hero">
      <div className="hero-orbits" aria-hidden="true"><i /><i /><i /></div>
      <div>
        <p className="eyebrow">Локальный сервер · слой памяти агентов</p>
        <h1>Universal Agent Memory</h1>
        <p>Единый слой долговременной памяти для OpenClaw, Hermes и других агентов.</p>
      </div>
      <div className="identity-card">
        <span className="self-hosted"><i /> Локально</span>
        <div className="identity-row"><span>Сервер</span><code title={props.tenant}>{shortUuid(props.tenant)}</code></div>
        <div className="identity-row"><span>Проект</span><code title={props.workspace}>{shortUuid(props.workspace)}</code></div>
        <span className={props.loading ? "sync loading" : "sync"}>{props.loading ? "Синхронизация" : "Живой статус"}</span>
      </div>
    </header>
  );
}

function TabStrip({ view, setView }: { view: View; setView: (view: View) => void }) {
  const tabs: Array<[View, string]> = [
    ["dashboard", "Память"],
    ["memory", "Поиск"],
    ["inbox", "Конфликты"],
    ["vault", "Файлы"],
    ["graph", "Граф"],
    ["settings", "Модели"]
  ];
  return (
    <div className="tab-strip" role="tablist">
      {tabs.map(([key, label]) => (
        <button
          key={key}
          role="tab"
          aria-selected={view === key}
          className={view === key ? "active" : ""}
          onClick={() => setView(key)}
        >
          {label}
        </button>
      ))}
    </div>
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
            <p>{translateKind(item.kind)} · ревизия {item.revision} · уверенность {Math.round(item.confidence * 100)}%</p>
          </div>
          <span className={`tag ${item.layer}`}>{translateLayer(item.layer)}</span>
          <span className={`tag ${item.status}`}>{translateStatus(item.status)}</span>
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
      <button onClick={() => void runRecall()}>Найти в памяти</button>
      <div className="recall-results">
        {results.map((item) => (
          <article key={item.id}>
            <b>{translateLayer(item.layer)} · {translateSource(item.source)}</b>
            <p>{item.text}</p>
            <small>оценка {item.score.toFixed(3)}</small>
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
    <div className="graph-wrap">
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
      aria-label="Интерактивный граф памяти в стиле Obsidian"
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
          <text y="15" className="node-layer">{translateLayer(node.layer)}</text>
        </g>
      ))}
    </svg>
    <div className="graph-legend">
      <span><i className="blue-dot" /> Ядро памяти</span>
      <span><i className="purple-dot" /> Семантическая память</span>
      <span><i className="cyan-dot" /> Контекстная память</span>
      <span><i className="dim-dot" /> Слабая связь</span>
    </div>
    </div>
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
      <pre>{stripFrontmatter(file?.content ?? "Файлов пока нет. Сохрани первую память.")}</pre>
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
      props.setStatus(`${dryRun ? "Проверка" : "Сохранено"}: замен ${result.supersede_count}, изменений ${result.changes.length}`);
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
        <p className="hint">Редактируй обычный текст памяти. Служебные поля, ревизии и векторы остаются под капотом.</p>
        <textarea value={text} disabled={!canEdit} onChange={(event) => setText(event.target.value)} />
        <div className="actions">
          <button onClick={() => void save(true)}>Dry-run</button>
          <button className="primary" onClick={() => void save(false)}>Сохранить и пересчитать вектор</button>
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
  const dgxSparkPreset = {
    provider: "tei",
    model_name: "jina-embeddings-v4",
    dimension: 2048,
    base_url: "http://192.168.0.10:8002",
    api_key: "",
    timeout_seconds: 30
  };
  const [form, setForm] = useState({
    provider: settings?.desired.provider ?? "tei",
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

  function applyDgxSparkPreset() {
    setForm(dgxSparkPreset);
    setStatus("Выбран preset DGX Spark Q8. Проверь endpoint, затем сохрани конфиг модели.");
  }

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
      <div className="preset-card">
        <div>
          <span className="eyebrow">Рекомендованная реальная модель векторов</span>
          <b>DGX Spark · Jina v4 Q8 · 2048 измерений</b>
          <p>OpenAI-совместимый endpoint: <code>http://192.168.0.10:8002/v1/embeddings</code></p>
        </div>
        <button onClick={applyDgxSparkPreset}>Использовать preset</button>
      </div>
      {(["provider", "model_name", "base_url", "api_key"] as const).map((key) => (
        <label key={key}>
          {modelFieldLabel(key)}
          <input
            type={key === "api_key" ? "password" : "text"}
            value={String(form[key])}
            onChange={(event) => setForm((current) => ({ ...current, [key]: event.target.value }))}
          />
        </label>
      ))}
      <label>
        Размерность
        <input type="number" value={form.dimension} onChange={(event) => setForm((current) => ({ ...current, dimension: Number(event.target.value) }))} />
      </label>
      <label>
        Таймаут, сек
        <input type="number" value={form.timeout_seconds} onChange={(event) => setForm((current) => ({ ...current, timeout_seconds: Number(event.target.value) }))} />
      </label>
      <div className="settings-summary">
        <b>Текущая модель векторов</b>
        <p>{settings?.runtime.provider} · {settings?.runtime.model_name} · {settings?.runtime.dimension} измерений</p>
        <small>
          Желаемый: {settings?.desired.provider} · {settings?.desired.model_name} · {settings?.desired.dimension} измерений
          {" · "}
          Перезапуск: {settings?.restart_required ? "да, нужен restart + reindex" : "нет"}
        </small>
      </div>
      <div className="actions">
        <button onClick={() => void save(true)}>Проверить endpoint</button>
        <button className="primary" onClick={() => void save(false)}>Сохранить конфиг модели</button>
      </div>
    </div>
  );
}

function ConflictList({
  conflicts,
  compact = false,
  onDecide
}: {
  conflicts: ConflictCase[];
  compact?: boolean;
  onDecide?: (
    item: ConflictCase,
    status: "accepted" | "overridden" | "dismissed",
    winnerValue: string | null,
    reason: string
  ) => Promise<void>;
}) {
  return (
    <div className={compact ? "conflicts compact" : "conflicts"}>
      {conflicts.length === 0 ? <Empty text="Конфликтов нет." /> : null}
      {conflicts.map((item) => (
        <article key={item.id} className={isOpenConflict(item) ? "" : "resolved"}>
          <div className="conflict-topline">
            <b>{conflictTitle(item)}</b>
            <span className={isOpenConflict(item) ? "pill amber" : "pill green"}>{reviewStatusLabel(item.review_status)}</span>
          </div>
          <p>{conflictValues(item).join(" ↔ ")}</p>
          {!compact ? (
            <>
              <small>{conflictRationale(item)}</small>
              <div className="candidate-list">
                {(item.candidates ?? []).map((candidate) => (
                  <div key={`${item.id}-${candidate.value}`} className={candidate.value === item.suggested_winner_value ? "candidate recommended" : "candidate"}>
                    <div>
                      <b>{candidate.value}</b>
                      <small>
                        {translateStatus(candidate.status)} · уверенность {Math.round(candidate.confidence * 100)}%
                      </small>
                    </div>
                    {onDecide && isOpenConflict(item) ? (
                      <button onClick={() => void onDecide(item, "overridden", candidate.value, "operator selected this candidate")}>
                        Выбрать
                      </button>
                    ) : null}
                  </div>
                ))}
              </div>
              {onDecide && isOpenConflict(item) ? (
                <div className="conflict-actions">
                  <button
                    className="primary"
                    disabled={!item.suggested_winner_value}
                    onClick={() => void onDecide(item, "accepted", item.suggested_winner_value, "accepted server recommendation")}
                  >
                    Принять рекомендацию
                  </button>
                  <button onClick={() => void onDecide(item, "dismissed", null, "dismissed as not actionable")}>
                    Скрыть как неактуальный
                  </button>
                </div>
              ) : null}
            </>
          ) : (
            <small>{item.suggested_winner_value ? `рекомендация: ${item.suggested_winner_value}` : conflictRationale(item)}</small>
          )}
        </article>
      ))}
    </div>
  );
}

function ActivityLog({ memories, conflicts, status }: { memories: MemoryItem[]; conflicts: ConflictCase[]; status: string }) {
  const events = [
    ["✦", status, "сейчас"],
    ["▱", `воспоминаний в индексе: ${memories.length}`, "онлайн"],
    ["⚖", `конфликтов отслеживается: ${conflicts.length}`, "онлайн"],
    ["▤", "Файлы в режиме обычного текста", "готово"],
    ["◎", "Узлы графа можно двигать", "готово"]
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

function translateKind(value: string) {
  const map: Record<string, string> = {
    fact: "факт",
    preference: "предпочтение",
    task: "задача",
    note: "заметка",
    placeholder: "узел"
  };
  return map[value] ?? value;
}

function translateLayer(value: string) {
  const map: Record<string, string> = {
    core: "ядро",
    semantic: "семантика",
    episodic: "эпизоды",
    procedural: "процедуры",
    reflection: "рефлексия",
    error: "ошибки",
    social: "социальное"
  };
  return map[value] ?? value;
}

function translateStatus(value: string) {
  const map: Record<string, string> = {
    active: "активна",
    pinned: "закреплена",
    stale: "устарела",
    disputed: "спорная",
    archived: "архив"
  };
  return map[value] ?? value;
}

function reviewStatusLabel(value: string) {
  const map: Record<string, string> = {
    unresolved: "нужно решить",
    pending: "нужно решить",
    accepted: "принято",
    overridden: "переопределено",
    dismissed: "скрыто"
  };
  return map[value] ?? value;
}

function translateSource(value: string) {
  if (value.includes("qdrant")) return "гибридный поиск";
  if (value.includes("postgres")) return "лексический поиск";
  return value;
}

function modelFieldLabel(value: "provider" | "model_name" | "base_url" | "api_key") {
  const map = {
    provider: "Провайдер",
    model_name: "Модель",
    base_url: "Base URL",
    api_key: "API ключ"
  };
  return map[value];
}

function shortUuid(value: string) {
  if (value.length <= 13) return value;
  return `${value.slice(0, 8)}…${value.slice(-4)}`;
}

function formatBytes(value?: number) {
  if (value == null) return "н/д";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size >= 10 ? size.toFixed(1) : size.toFixed(2)} ${units[unit]}`;
}

function formatDuration(seconds?: number) {
  if (seconds == null) return "н/д";
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days > 0) return `${days}д ${hours}ч`;
  if (hours > 0) return `${hours}ч ${minutes}м`;
  return `${minutes}м`;
}

function formatNumber(value?: number | null) {
  return value == null ? "н/д" : value.toFixed(2);
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
