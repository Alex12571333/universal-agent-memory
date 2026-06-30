export type MemoryLayer =
  | "working"
  | "core"
  | "episodic"
  | "semantic"
  | "procedural"
  | "social"
  | "reflection"
  | "error";

export type MemoryScope =
  | "private"
  | "thread"
  | "team"
  | "workspace"
  | "organization";

export interface RetainRequest {
  text: string;
  layer?: MemoryLayer;
  scope?: MemoryScope;
  kind?: string;
  source_kind?: string;
  agent_id?: string;
  thread_id?: string;
  labels?: string[];
  importance?: number;
  confidence?: number;
  idempotency_key?: string;
}

export interface RetainResponse {
  id: string;
  created: boolean;
  queued_event_ids: string[];
}

export interface RecallRequest {
  query: string;
  agent_id?: string;
  thread_id?: string;
  layers?: MemoryLayer[];
  labels?: string[];
  top_k?: number;
  minimum_score?: number;
  operation?: string;
  context_budget_tokens?: number;
}

export interface MemoryResult {
  id: string;
  text: string;
  layer: MemoryLayer;
  score: number;
  source: string;
}

export interface CompiledContext {
  operation: string;
  used_tokens: number;
  budget_tokens: number;
  markdown: string;
  trace_ids: string[];
}

export interface RecallResponse {
  results: MemoryResult[];
  sources_used: string[];
  context: CompiledContext;
}

export interface IngestTextRequest {
  text: string;
  origin_uri: string;
  agent_id?: string;
  thread_id?: string;
  labels?: string[];
  chunk_size_chars?: number;
  chunk_overlap_chars?: number;
}

export interface IngestTextResponse {
  document_checksum: string;
  memory_ids: string[];
  created_count: number;
}

export interface RetryPolicy {
  maxRetries?: number;
  baseDelayMs?: number;
  retryStatuses?: ReadonlySet<number>;
}
