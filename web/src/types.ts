export const DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001";
export const DEFAULT_WORKSPACE = "00000000-0000-0000-0000-000000000002";

export type MemoryLayer =
  | "core"
  | "semantic"
  | "episodic"
  | "procedural"
  | "reflection"
  | "error"
  | "social";

export type MemoryStatus = "active" | "superseded" | "retired";

export interface MemoryItem {
  id: string;
  layer: MemoryLayer;
  scope: string;
  status: MemoryStatus;
  kind: string;
  text: string;
  labels: string[];
  confidence: number;
  revision: number;
  supersedes_id: string | null;
  created_at: string;
}

export interface MemoriesResponse {
  tenant_id: string;
  workspace_id: string;
  count: number;
  memories: MemoryItem[];
}

export interface ConflictCase {
  id: string;
  status?: string;
  review_status: string;
  kind?: string;
  key?: string;
  subject?: string;
  predicate?: string;
  values?: string[];
  evidence_item_ids?: string[];
  suggested_winner_value: string | null;
  suggested_reason?: string;
  rationale?: string;
  created_at?: string;
  candidates?: Array<{
    value: string;
    status: string;
    evidence_ids: string[];
    confidence: number;
    latest_created_at: string;
  }>;
}

export interface ConflictsResponse {
  count: number;
  cases: ConflictCase[];
}

export interface VaultFile {
  path: string;
  content: string;
  editable_content?: string;
}

export interface VaultResponse {
  file_count: number;
  files: VaultFile[];
}

export interface VaultHealthIssue {
  severity: "error" | "warning";
  code: string;
  message: string;
  item_id: string | null;
  edge_id: string | null;
  observation_id: string | null;
}

export interface VaultHealthResponse {
  healthy: boolean;
  memory_count: number;
  edge_count: number;
  observation_count: number;
  recallable_head_count: number;
  unlinked_head_count: number;
  error_count: number;
  warning_count: number;
  issues: VaultHealthIssue[];
}

export interface ModelSettings {
  runtime: {
    provider: string;
    model_name: string;
    dimension: number;
    base_url: string | null;
    timeout_seconds: number;
  };
  desired: {
    provider: string;
    model_name: string;
    dimension: number;
    base_url: string | null;
    timeout_seconds: number;
    api_key_set?: boolean;
  };
  env: Record<string, string | number | null>;
  restart_required: boolean;
  settings_path: string | null;
}

export interface RecallResult {
  id: string;
  text: string;
  layer: MemoryLayer;
  status: MemoryStatus;
  score: number;
  source: string;
}

export interface RecallResponse {
  results: RecallResult[];
  sources_used: string[];
  context: {
    operation: string;
    used_tokens: number;
    budget_tokens: number;
    markdown: string;
    trace_ids: string[];
  };
}

export interface ImportVaultResponse {
  dry_run: boolean;
  supersede_count: number;
  changes: Array<{
    path: string;
    action: string;
    item_id: string | null;
    expected_revision: number | null;
    new_item_id: string | null;
    message: string;
  }>;
}

export interface SystemStatus {
  status: "ok" | string;
  version: string;
  uptime_seconds: number;
  storage: {
    path: string;
    total_bytes: number;
    used_bytes: number;
    free_bytes: number;
    used_percent: number | null;
  };
  process: {
    rss_mb: number | null;
    pid: number;
  };
  load_average: {
    one_minute: number | null;
    five_minutes: number | null;
    fifteen_minutes: number | null;
  };
}
