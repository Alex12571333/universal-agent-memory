import type {
  ConflictsResponse,
  ImportVaultResponse,
  MemoriesResponse,
  ModelSettings,
  RecallResponse,
  SystemStatus,
  VaultFile,
  VaultResponse
} from "./types";

export type UiSession = {
  authenticated: boolean;
  auth_required: boolean;
  principal?: string;
  csrf_token?: string | null;
  expires_at?: number;
};

let csrfToken: string | null = null;

export function setCsrfToken(value: string | null | undefined) {
  csrfToken = value || null;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      ...(csrfToken && !["GET", "HEAD", "OPTIONS"].includes(init?.method ?? "GET")
        ? { "X-CSRF-Token": csrfToken }
        : {}),
      ...(init?.headers ?? {})
    }
  });
  if (!response.ok) {
    if (response.status === 401) {
      window.dispatchEvent(new Event("uam-auth-required"));
    }
    const text = await response.text();
    throw new Error(`${response.status} ${response.statusText}: ${text}`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  session() {
    return request<UiSession>("/v1/ui/session");
  },
  login(apiKey: string) {
    return request<UiSession>("/v1/ui/session", {
      method: "POST",
      body: JSON.stringify({ api_key: apiKey })
    });
  },
  logout() {
    return request<UiSession>("/v1/ui/session", { method: "DELETE" });
  },
  memories(workspace: string, tenant: string) {
    return request<MemoriesResponse>(
      `/v1/workspaces/${workspace}/memories?tenant_id=${tenant}`
    );
  },
  conflicts(workspace: string, tenant: string) {
    return request<ConflictsResponse>(
      `/v1/workspaces/${workspace}/conflicts?tenant_id=${tenant}&include_resolved=true`
    );
  },
  decideConflict(
    workspace: string,
    tenant: string,
    caseId: string,
    body: { status: "accepted" | "overridden" | "dismissed"; winner_value?: string | null; reason: string }
  ) {
    return request<Record<string, unknown>>(
      `/v1/workspaces/${workspace}/conflicts/${caseId}/decision`,
      {
        method: "PUT",
        body: JSON.stringify({
          tenant_id: tenant,
          winner_value: null,
          ...body
        })
      }
    );
  },
  vault(workspace: string, tenant: string) {
    return request<VaultResponse>(
      `/v1/workspaces/${workspace}/vault?tenant_id=${tenant}`
    );
  },
  modelSettings() {
    return request<ModelSettings>("/v1/settings/models");
  },
  systemStatus() {
    return request<SystemStatus>("/v1/system/status");
  },
  saveModelSettings(body: Record<string, unknown>) {
    return request<ModelSettings>("/v1/settings/models", {
      method: "PUT",
      body: JSON.stringify(body)
    });
  },
  testModelSettings(body: Record<string, unknown>) {
    return request<Record<string, unknown>>("/v1/settings/models/test", {
      method: "POST",
      body: JSON.stringify(body)
    });
  },
  recall(workspace: string, tenant: string, query: string) {
    return request<RecallResponse>("/v1/memory/recall", {
      method: "POST",
      body: JSON.stringify({
        tenant_id: tenant,
        workspace_id: workspace,
        query,
        operation: "operator-review",
        top_k: 8,
        context_budget_tokens: 1200
      })
    });
  },
  retain(workspace: string, tenant: string, text: string, layer = "semantic") {
    return request<Record<string, unknown>>("/v1/memory/retain", {
      method: "POST",
      body: JSON.stringify({
        tenant_id: tenant,
        workspace_id: workspace,
        layer,
        scope: "workspace",
        kind: "operator_note",
        text,
        labels: ["operator-ui"],
        source_kind: "operator-ui",
        confidence: 0.78,
        importance: 0.62
      })
    });
  },
  importVault(workspace: string, tenant: string, files: VaultFile[], dryRun: boolean) {
    return request<ImportVaultResponse>(`/v1/workspaces/${workspace}/vault/import`, {
      method: "POST",
      body: JSON.stringify({
        tenant_id: tenant,
        dry_run: dryRun,
        files
      })
    });
  },
  archiveVaultFile(workspace: string, tenant: string, file: VaultFile) {
    return request<ImportVaultResponse>(`/v1/workspaces/${workspace}/vault/archive`, {
      method: "POST",
      body: JSON.stringify({
        tenant_id: tenant,
        file
      })
    });
  },
  reindex(workspace: string, tenant: string) {
    return request<Record<string, unknown>>(
      `/v1/workspaces/${workspace}/reindex?tenant_id=${tenant}`,
      { method: "POST" }
    );
  },
  reflect(workspace: string, tenant: string) {
    return request<Record<string, unknown>>(
      `/v1/workspaces/${workspace}/reflect?tenant_id=${tenant}`,
      { method: "POST" }
    );
  }
};
