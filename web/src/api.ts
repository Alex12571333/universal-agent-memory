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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {})
    }
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${response.status} ${response.statusText}: ${text}`);
  }
  return response.json() as Promise<T>;
}

export const api = {
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
