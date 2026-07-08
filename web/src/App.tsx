import { Component, useEffect, useMemo, useState } from "react";
import type { PointerEvent, ReactNode } from "react";
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

type GraphNode = {
  id: string;
  label: string;
  sublabel: string;
  kind: "core" | "agent" | "semantic" | "context" | "task" | "error" | "weak";
  icon: "brain" | "paw" | "wing" | "document" | "heart" | "check" | "warning" | "cube" | null;
  x: number;
  y: number;
  r: number;
};

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
      setSelectedFile((current) => current ?? preferredVaultFile(vaultData.files)?.path ?? null);
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
  const selectedVault = vault.find((item) => item.path === selectedFile) ?? preferredVaultFile(vault) ?? vault[0];

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

        <section className={view === "graph" ? "content-grid graph-expanded" : "content-grid"}>
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
              action={
                <button onClick={() => setView(view === "graph" ? "dashboard" : "graph")}>
                  {view === "graph" ? "Свернуть" : "Развернуть"}
                </button>
              }
            />
            {view === "memory" ? (
              <RecallPanel query={query} setQuery={setQuery} runRecall={runRecall} results={recall} />
            ) : (
              <MemoryGraph memories={memories} selectedId={selected?.id} onSelect={setSelectedMemory} expanded={view === "graph"} />
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

function MemoryGraph({
  memories,
  selectedId,
  onSelect,
  expanded
}: {
  memories: MemoryItem[];
  selectedId?: string;
  onSelect: (id: string) => void;
  expanded: boolean;
}) {
  const nodes = useMemo<GraphNode[]>(() => {
    const semanticCount = memories.filter((item) => item.layer === "semantic").length;
    const coreCount = memories.filter((item) => item.layer === "core").length;
    return [
      { id: "project", label: "Проект памяти", sublabel: `${memories.length} memories`, kind: "core", icon: "brain", x: 500, y: 330, r: 76 },
      { id: "openclaw", label: "OpenClaw", sublabel: "агент", kind: "agent", icon: "paw", x: 330, y: 188, r: 58 },
      { id: "hermes", label: "Hermes", sublabel: "агент", kind: "agent", icon: "wing", x: 690, y: 188, r: 58 },
      { id: "facts", label: "Факты", sublabel: `${semanticCount || memories.length} записей`, kind: "context", icon: "document", x: 260, y: 346, r: 52 },
      { id: "prefs", label: "Предпочтения", sublabel: "стиль", kind: "context", icon: "heart", x: 760, y: 346, r: 52 },
      { id: "tasks", label: "Задачи", sublabel: "планы", kind: "semantic", icon: "check", x: 360, y: 505, r: 47 },
      { id: "errors", label: "Ошибки", sublabel: "ограничения", kind: "error", icon: "warning", x: 520, y: 540, r: 47 },
      { id: "context", label: "Контекст", sublabel: "сессия", kind: "context", icon: "cube", x: 690, y: 480, r: 50 },
      { id: "plugins", label: "Плагины", sublabel: "связь", kind: "weak", icon: null, x: 250, y: 135, r: 16 },
      { id: "commands", label: "Команды", sublabel: "связь", kind: "weak", icon: null, x: 170, y: 232, r: 14 },
      { id: "protocols", label: "Протоколы", sublabel: "связь", kind: "weak", icon: null, x: 430, y: 102, r: 15 },
      { id: "sessions", label: "Сессии", sublabel: "связь", kind: "weak", icon: null, x: 520, y: 142, r: 15 },
      { id: "style", label: "Стиль работы", sublabel: "связь", kind: "weak", icon: null, x: 890, y: 292, r: 15 },
      { id: "format", label: "Формат ответов", sublabel: "связь", kind: "weak", icon: null, x: 930, y: 390, r: 14 },
      { id: "environment", label: "Окружение", sublabel: "связь", kind: "weak", icon: null, x: 790, y: 570, r: 14 },
      { id: "events", label: "События", sublabel: "связь", kind: "weak", icon: null, x: 610, y: 620, r: 14 },
      { id: "goals", label: "Цели", sublabel: "связь", kind: "weak", icon: null, x: 405, y: 420, r: 14 },
      { id: "core", label: "Ядро", sublabel: `${coreCount} core`, kind: "weak", icon: null, x: 480, y: 445, r: 12 }
    ];
  }, [memories]);
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>({});
  const [dragging, setDragging] = useState<string | null>(null);
  const [miniDragging, setMiniDragging] = useState(false);
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 100, y: 70 });
  const resolved = nodes.map((node) => ({ ...node, ...(positions[node.id] ?? {}) }));
  const center = resolved.find((node) => node.id === "project");
  const world = { width: 1200, height: 840 };
  const viewport = { width: 1000 / zoom, height: 700 / zoom };
  const clampPan = (next: { x: number; y: number }, nextZoom = zoom) => {
    const nextViewport = { width: 1000 / nextZoom, height: 700 / nextZoom };
    return {
      x: Math.min(Math.max(0, next.x), Math.max(0, world.width - nextViewport.width)),
      y: Math.min(Math.max(0, next.y), Math.max(0, world.height - nextViewport.height))
    };
  };
  const updateZoom = (nextZoom: number) => {
    const clamped = Math.min(1.8, Math.max(0.72, nextZoom));
    setZoom(clamped);
    setPan((current) => clampPan(current, clamped));
  };
  const panFromMiniMap = (event: PointerEvent<SVGSVGElement>) => {
    const svg = event.currentTarget;
    const point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    const mapped = point.matrixTransform(svg.getScreenCTM()?.inverse());
    setPan(clampPan({ x: mapped.x - viewport.width / 2, y: mapped.y - viewport.height / 2 }));
  };
  const edges = [
    ["project", "openclaw", "strong"],
    ["project", "hermes", "strong"],
    ["project", "facts", "strong"],
    ["project", "prefs", "strong"],
    ["project", "tasks", "strong"],
    ["project", "errors", "strong"],
    ["project", "context", "strong"],
    ["openclaw", "plugins", "weak"],
    ["openclaw", "commands", "weak"],
    ["openclaw", "protocols", "weak"],
    ["openclaw", "sessions", "weak"],
    ["hermes", "sessions", "weak"],
    ["hermes", "style", "weak"],
    ["prefs", "style", "weak"],
    ["prefs", "format", "weak"],
    ["context", "environment", "weak"],
    ["context", "events", "weak"],
    ["tasks", "goals", "weak"],
    ["errors", "events", "weak"],
    ["facts", "goals", "weak"]
  ] as const;
  const byId = Object.fromEntries(resolved.map((node) => [node.id, node]));

  return (
    <div className={expanded ? "graph-wrap expanded" : "graph-wrap compact"}>
    <div className="graph-toolbar">
      <span>⌘ {resolved.length} узлов · {edges.length} связей</span>
      <button type="button" onClick={() => updateZoom(zoom - 0.12)} aria-label="Уменьшить граф">−</button>
      <button type="button" onClick={() => { setZoom(1); setPan({ x: 100, y: 70 }); }}>{Math.round(zoom * 100)}%</button>
      <button type="button" onClick={() => updateZoom(zoom + 0.12)} aria-label="Увеличить граф">+</button>
    </div>
    <svg
      className="graph"
      viewBox={`${pan.x} ${pan.y} ${viewport.width} ${viewport.height}`}
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
          <stop offset="0%" stopColor="#39b8ff" />
          <stop offset="58%" stopColor="#1263ff" />
          <stop offset="100%" stopColor="#0b1234" />
        </radialGradient>
        <radialGradient id="agentGlow">
          <stop offset="0%" stopColor="#b686ff" />
          <stop offset="72%" stopColor="#4823b9" />
          <stop offset="100%" stopColor="#130a37" />
        </radialGradient>
        <radialGradient id="contextGlow">
          <stop offset="0%" stopColor="#53f1ff" />
          <stop offset="72%" stopColor="#047484" />
          <stop offset="100%" stopColor="#06262e" />
        </radialGradient>
      </defs>
      <g className="graph-stars" aria-hidden="true">
        {resolved.slice(0, 16).map((node, index) => (
          <circle key={`star-${node.id}`} cx={(node.x * 1.37 + index * 43) % 940 + 30} cy={(node.y * 1.19 + index * 29) % 640 + 25} r={index % 3 === 0 ? 1.4 : 0.8} />
        ))}
      </g>
      {edges.map(([from, to, weight]) => {
        const a = byId[from];
        const b = byId[to];
        if (!a || !b) return null;
        const midX = (a.x + b.x) / 2;
        const midY = (a.y + b.y) / 2 - (weight === "strong" ? 34 : 18);
        return (
          <path
            key={`${from}-${to}`}
            className={weight === "strong" ? "graph-edge strong" : "graph-edge weak"}
            d={`M ${a.x} ${a.y} Q ${midX} ${midY} ${b.x} ${b.y}`}
          />
        );
      })}
      {resolved.map((node, index) => (
        <g
          key={node.id}
          className={`graph-node ${node.kind} ${node.id === selectedId ? "selected" : ""}`}
          transform={`translate(${node.x},${node.y})`}
          onPointerDown={(event) => {
            event.currentTarget.setPointerCapture(event.pointerId);
            setDragging(node.id);
            onSelect(node.id);
          }}
        >
          <circle r={node.r} />
          {node.icon ? <GraphIcon type={node.icon} large={node.id === "project"} /> : null}
          <text y={node.id === "project" ? 19 : 12} className="node-title">{node.label}</text>
          {expanded || node.r > 45 ? <text y={node.id === "project" ? 42 : 31} className="node-layer">{node.sublabel}</text> : null}
        </g>
      ))}
    </svg>
    {center ? (
      <div className="graph-minimap">
        <svg
          viewBox={`0 0 ${world.width} ${world.height}`}
          role="application"
          aria-label="Миникарта графа памяти. Нажмите или потяните, чтобы сдвинуть область просмотра."
          onPointerDown={(event) => {
            event.currentTarget.setPointerCapture(event.pointerId);
            setMiniDragging(true);
            panFromMiniMap(event);
          }}
          onPointerMove={(event) => {
            if (miniDragging) panFromMiniMap(event);
          }}
          onPointerUp={() => setMiniDragging(false)}
          onPointerLeave={() => setMiniDragging(false)}
        >
          {edges.slice(0, 14).map(([from, to]) => {
            const a = byId[from];
            const b = byId[to];
            return a && b ? <line key={`mini-${from}-${to}`} x1={a.x} y1={a.y} x2={b.x} y2={b.y} /> : null;
          })}
          {resolved.map((node) => <circle key={`mini-${node.id}`} cx={node.x} cy={node.y} r={node.id === "project" ? 22 : 14} />)}
          <rect className="mini-viewport" x={pan.x} y={pan.y} width={viewport.width} height={viewport.height} />
        </svg>
      </div>
    ) : null}
    <div className="graph-legend">
      <span><i className="blue-dot" /> <b>Ядро памяти</b><small>ключевая сущность</small></span>
      <span><i className="purple-dot" /> <b>Семантика</b><small>смысловые связи</small></span>
      <span><i className="cyan-dot" /> <b>Контекст</b><small>сессии и окружение</small></span>
      <span><i className="dim-dot" /> <b>Слабая связь</b><small>низкая сила связи</small></span>
    </div>
    </div>
  );
}

function VaultPreview({ files, selectedPath, setSelectedFile }: { files: VaultFile[]; selectedPath?: string; setSelectedFile: (path: string) => void }) {
  const file = files.find((item) => item.path === selectedPath) ?? files[0];
  const readable = vaultReadableBody(file?.content ?? "");
  return (
    <div className="vault-preview-grid">
      <div className="file-tree">
        {files.slice(0, 10).map((item) => (
          <button key={item.path} title={item.path} className={item.path === file?.path ? "active" : ""} onClick={() => setSelectedFile(item.path)}>
            <span>{vaultFileTitle(item)}</span>
            <small>{fileFolderName(item.path)}</small>
          </button>
        ))}
      </div>
      <article className="vault-readable">
        <div>
          <small>{file ? `${fileDisplayName(file.path)} · ${fileFolderName(file.path)}` : "vault"}</small>
          <p>{readable || "Файлов пока нет. Сохрани первую память."}</p>
        </div>
      </article>
    </div>
  );
}

function GraphIcon({ type, large = false }: { type: NonNullable<GraphNode["icon"]>; large?: boolean }) {
  const scale = large ? 1.24 : 1;
  const common = {
    fill: "none",
    stroke: "currentColor",
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    strokeWidth: 2.1
  };
  return (
    <g className="node-svg-icon" transform={`translate(-12 ${large ? -33 : -31}) scale(${scale})`}>
      {type === "brain" ? (
        <>
          <path {...common} d="M9 5.5c-2.5 0-4.5 2-4.5 4.5 0 1 .3 1.9.8 2.6A5.1 5.1 0 0 0 4 16.2C4 19.4 6.6 22 9.8 22H12V7.8A3.7 3.7 0 0 0 9 5.5Z" />
          <path {...common} d="M15 5.5c2.5 0 4.5 2 4.5 4.5 0 1-.3 1.9-.8 2.6a5.1 5.1 0 0 1 1.3 3.6c0 3.2-2.6 5.8-5.8 5.8H12V7.8a3.7 3.7 0 0 1 3-2.3Z" />
          <path {...common} d="M8.2 12.2h3.2M12.6 12.2h3.2M7.8 16.3h3.4M12.8 16.3h3.4" />
        </>
      ) : null}
      {type === "paw" ? (
        <>
          <ellipse cx="12" cy="15.3" rx="4.7" ry="4" fill="currentColor" opacity="0.92" />
          <circle cx="6.8" cy="10" r="2.1" fill="currentColor" opacity="0.85" />
          <circle cx="10.3" cy="7.2" r="2.1" fill="currentColor" opacity="0.85" />
          <circle cx="13.8" cy="7.2" r="2.1" fill="currentColor" opacity="0.85" />
          <circle cx="17.2" cy="10" r="2.1" fill="currentColor" opacity="0.85" />
        </>
      ) : null}
      {type === "wing" ? (
        <>
          <path {...common} d="M20 5c-6.6.6-11.6 3.3-15 8.2 4.5-.5 8.3-2.2 11.3-5.2" />
          <path {...common} d="M17 10c-3.6 4.1-7.7 6.4-12.4 7 2.2 1.9 5.2 2.3 8.1 1.2 3.1-1.2 5.5-3.9 7.3-8.2Z" />
          <path {...common} d="M8 14.2c2.3.2 4.7-.5 7.2-2.2" />
        </>
      ) : null}
      {type === "document" ? (
        <>
          <path {...common} d="M7 4.5h7l4 4V20H7z" />
          <path {...common} d="M14 4.5V9h4" />
          <path {...common} d="M10 13h5M10 16h6" />
        </>
      ) : null}
      {type === "heart" ? (
        <path fill="currentColor" d="M12 21s-7.5-4.8-9.2-9.2C1.5 8.4 3.6 5.5 6.8 5.5c1.9 0 3.3 1 4.2 2.3.9-1.3 2.3-2.3 4.2-2.3 3.2 0 5.3 2.9 4 6.3C19.5 16.2 12 21 12 21Z" opacity="0.92" />
      ) : null}
      {type === "check" ? (
        <>
          <rect {...common} x="5" y="5" width="14" height="16" rx="3" />
          <path {...common} d="m8.5 13.5 2.4 2.4 4.8-5.4" />
        </>
      ) : null}
      {type === "warning" ? (
        <>
          <path {...common} d="M12 4.5 21 20H3z" />
          <path {...common} d="M12 10v4" />
          <circle cx="12" cy="17" r="1" fill="currentColor" />
        </>
      ) : null}
      {type === "cube" ? (
        <>
          <path {...common} d="m12 3.8 8 4.4v8.9l-8 4.4-8-4.4V8.2z" />
          <path {...common} d="M4.4 8.5 12 13l7.6-4.5M12 13v8" />
        </>
      ) : null}
    </g>
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
  const [text, setText] = useState(vaultReadableBody(file?.content ?? ""));
  useEffect(() => setText(vaultReadableBody(file?.content ?? "")), [file?.path, file?.content]);
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
          <button key={item.path} title={item.path} className={item.path === file?.path ? "active" : ""} onClick={() => props.setSelectedFile(item.path)}>
            <span>{vaultFileTitle(item)}</span>
            <small>{fileFolderName(item.path)}</small>
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

function fileDisplayName(path: string) {
  const last = path.split("/").pop() ?? path;
  if (last === "README.md") return "README";
  const stem = last.replace(/^mem-/, "").replace(/\.md$/, "");
  return shortUuid(stem);
}

function vaultFileTitle(file: VaultFile) {
  if (file.path.endsWith("README.md")) return "README";
  const readable = vaultReadableBody(file.content)
    .split(/\n+/)
    .map((line) => line.replace(/^#{1,6}\s*/, "").replace(/^[-*]\s*/, "").trim())
    .find(Boolean);
  return readable ? truncateText(readable, 46) : fileDisplayName(file.path);
}

function preferredVaultFile(files: VaultFile[]) {
  return files.find((file) => !file.path.endsWith("README.md") && !file.path.endsWith("index.md")) ?? files[0];
}

function fileFolderName(path: string) {
  const parts = path.split("/");
  if (parts.length <= 1) return "vault root";
  return parts.slice(0, -1).join("/");
}

function truncateText(value: string, max: number) {
  return value.length <= max ? value : `${value.slice(0, max - 1).trimEnd()}…`;
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

function vaultReadableBody(content: string) {
  return splitVaultMarkdown(content).body;
}

function replaceBody(original: string, body: string) {
  const parts = splitVaultMarkdown(original);
  const suffix = parts.systemSections ? `\n\n${parts.systemSections.trim()}\n` : "\n";
  return `${parts.frontmatter}${body.trim()}${suffix}`;
}

function splitVaultMarkdown(content: string) {
  const frontmatterMatch = content.match(/^---[\s\S]*?---\s*/);
  const frontmatter = frontmatterMatch?.[0] ?? "";
  const withoutFrontmatter = content.slice(frontmatter.length).trim();
  const sectionMatch = withoutFrontmatter.match(/\n## (?:Provenance|Quote|Links|Evidence)\b/);
  if (!sectionMatch || sectionMatch.index === undefined) {
    return { frontmatter, body: withoutFrontmatter.trim(), systemSections: "" };
  }
  return {
    frontmatter,
    body: withoutFrontmatter.slice(0, sectionMatch.index).trim(),
    systemSections: withoutFrontmatter.slice(sectionMatch.index + 1).trim()
  };
}
