"""HTTP end-to-end checks for a running Universal Agent Memory server."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

TENANT = UUID("00000000-0000-0000-0000-000000000001")
WORKSPACE = UUID("00000000-0000-0000-0000-000000000002")
FOREIGN_TENANT = UUID("00000000-0000-0000-0000-0000000000ff")


@dataclass(frozen=True, slots=True)
class Api:
    base_url: str
    api_key: str | None = None

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        expect_status: int = 200,
        auth: bool = True,
    ) -> Any:
        headers = {"Content-Type": "application/json"}
        if auth and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            f"{self.base_url.rstrip('/')}{path}",
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=20) as response:  # noqa: S310
                status = response.status
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            status = exc.code
            raw = exc.read().decode("utf-8")
        if status != expect_status:
            raise AssertionError(f"{method} {path}: expected {expect_status}, got {status}: {raw}")
        if not raw:
            return None
        content_type = request.get_header("Accept") or ""
        if raw.strip().startswith("{") or raw.strip().startswith("["):
            return json.loads(raw)
        if "text" in content_type:
            return raw
        return raw


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def retain(api: Api, text: str, *, tenant: UUID = TENANT, key: str) -> dict[str, Any]:
    return api.request(
        "POST",
        "/v1/memory/retain",
        {
            "tenant_id": str(tenant),
            "workspace_id": str(WORKSPACE),
            "layer": "semantic",
            "scope": "workspace",
            "kind": "fact",
            "text": text,
            "labels": ["api-e2e"],
            "idempotency_key": key,
        },
        expect_status=201,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:6798")
    parser.add_argument("--api-key")
    args = parser.parse_args()
    api = Api(args.base_url, args.api_key)

    health = api.request("GET", "/health", auth=False)
    expect(health["status"] == "ok", "health did not return ok")
    print("PASS health")

    if args.api_key:
        api.request("POST", "/v1/memory/retain", {}, expect_status=401, auth=False)
        print("PASS auth_required")

    first = retain(
        api,
        "API E2E memory says Docker server stores durable facts.",
        key="api-e2e:durable-fact",
    )
    retry = retain(
        api,
        "API E2E retry body should not overwrite idempotent fact.",
        key="api-e2e:durable-fact",
    )
    expect(first["id"] == retry["id"], "retain idempotency failed")
    print("PASS retain_idempotency")

    api.request(
        "POST",
        "/v1/memory/retain",
        {
            "tenant_id": str(FOREIGN_TENANT),
            "workspace_id": str(WORKSPACE),
            "layer": "semantic",
            "scope": "workspace",
            "kind": "fact",
            "text": "Foreign tenant memory must fail cleanly when tenant is not provisioned.",
            "labels": ["api-e2e"],
            "idempotency_key": "api-e2e:foreign-tenant",
        },
        expect_status=422,
    )
    recall = api.request(
        "POST",
        "/v1/memory/recall",
        {
            "tenant_id": str(TENANT),
            "workspace_id": str(WORKSPACE),
            "query": "Docker server durable facts",
            "top_k": 5,
        },
    )
    texts = {row["text"] for row in recall["results"]}
    expect(any("Docker server" in text for text in texts), "expected own memory missing")
    print("PASS recall_and_unknown_tenant_boundary")

    superseded = api.request(
        "PUT",
        f"/v1/memory/{first['id']}/supersede",
        {
            "tenant_id": str(TENANT),
            "text": "API E2E memory says Docker server uses safe supersede revisions.",
            "expected_revision": 1,
            "idempotency_key": "api-e2e:supersede",
        },
        expect_status=201,
    )
    api.request(
        "PUT",
        f"/v1/memory/{first['id']}/supersede",
        {
            "tenant_id": str(TENANT),
            "text": "API E2E stale update should conflict.",
            "expected_revision": 1,
            "idempotency_key": "api-e2e:stale-supersede",
        },
        expect_status=409,
    )
    expect(superseded["supersedes_id"] == first["id"], "supersede response missing parent")
    print("PASS supersede_cas")

    graph_edge = api.request(
        "POST",
        "/v1/graph/edges",
        {
            "tenant_id": str(TENANT),
            "workspace_id": str(WORKSPACE),
            "src_id": first["id"],
            "dst_id": superseded["id"],
            "edge_type": "supersedes",
            "weight": 1.0,
        },
        expect_status=201,
    )
    graph_query = urlencode({"tenant_id": str(TENANT), "workspace_id": str(WORKSPACE)})
    neighbors = api.request("GET", f"/v1/memory/{first['id']}/neighbors?{graph_query}")
    expect(graph_edge["dst_id"] == superseded["id"], "graph edge response missing dst")
    expect(neighbors["count"] >= 1, "graph neighbors returned no edges")
    expect(
        any(edge["edge_type"] == "supersedes" for edge in neighbors["edges"]),
        "graph neighbors missing supersedes edge",
    )
    print("PASS graph_neighbors")

    retain(api, "Release E2E is July 15.", key="api-e2e:conflict-old")
    retain(api, "Release E2E is July 16.", key="api-e2e:conflict-new")
    conflict_query = urlencode({"tenant_id": str(TENANT), "include_resolved": "true"})
    conflicts = api.request("GET", f"/v1/workspaces/{WORKSPACE}/conflicts?{conflict_query}")
    expect(conflicts["count"] >= 1, "conflict inbox returned no cases")
    print("PASS conflicts")

    vault_query = urlencode({"tenant_id": str(TENANT)})
    vault = api.request("GET", f"/v1/workspaces/{WORKSPACE}/vault?{vault_query}")
    expect(vault["file_count"] > 0, "vault export returned no files")
    expect(
        any(
            file["path"].startswith("semantic/") or file["path"].startswith("core/")
            for file in vault["files"]
        ),
        "vault missing memories",
    )
    print("PASS vault")

    vault_source_text = f"Vault editor source text {uuid4()}."
    vault_updated_text = vault_source_text.replace("source", "updated")
    retain(api, vault_source_text, key=f"api-e2e:vault-edit:{uuid4()}")
    editable_vault = api.request("GET", f"/v1/workspaces/{WORKSPACE}/vault?{vault_query}")
    editable_file = next(
        file
        for file in editable_vault["files"]
        if (file["path"].startswith("semantic/") or file["path"].startswith("core/"))
        and vault_source_text in file["content"]
    )
    editable_file["content"] = editable_file["content"].replace(
        vault_source_text,
        vault_updated_text,
    )
    dry_import = api.request(
        "POST",
        f"/v1/workspaces/{WORKSPACE}/vault/import",
        {
            "tenant_id": str(TENANT),
            "dry_run": True,
            "files": [editable_file],
        },
    )
    applied_import = api.request(
        "POST",
        f"/v1/workspaces/{WORKSPACE}/vault/import",
        {
            "tenant_id": str(TENANT),
            "dry_run": False,
            "files": [editable_file],
        },
    )
    expect(
        dry_import["changes"][0]["action"] == "supersede",
        f"vault edit dry-run failed: {dry_import}",
    )
    expect(
        applied_import["changes"][0]["new_item_id"] is not None,
        "vault edit did not create a new revision",
    )
    edited_reindex = api.request(
        "POST",
        f"/v1/workspaces/{WORKSPACE}/reindex?{urlencode({'tenant_id': str(TENANT)})}",
        expect_status=202,
    )
    expect(edited_reindex["reindexed_count"] >= 1, "vault edit reindex failed")
    edited_recall = api.request(
        "POST",
        "/v1/memory/recall",
        {
            "tenant_id": str(TENANT),
            "workspace_id": str(WORKSPACE),
            "query": vault_updated_text,
            "top_k": 5,
        },
    )
    expect(
        any(vault_updated_text in row["text"] for row in edited_recall["results"]),
        "vault edited text was not recallable after reindex",
    )
    print("PASS vault_edit_reindex")

    reindex = api.request(
        "POST",
        f"/v1/workspaces/{WORKSPACE}/reindex?{urlencode({'tenant_id': str(TENANT)})}",
        expect_status=202,
    )
    expect(reindex["reindexed_count"] >= 1, "reindex did not process memories")
    print("PASS reindex")

    ui = api.request("GET", "/ui")
    expect("Универсальная память агентов" in ui, "Russian operator UI missing")
    expect("Настройки моделей" in ui, "model settings UI missing")
    expect("mountForceGraph" in ui, "interactive graph UI missing")
    print("PASS ui")

    model_settings = {
        "provider": "fake",
        "model_name": "fake-api-e2e",
        "dimension": 32,
        "base_url": None,
        "api_key": "local-e2e-secret",
        "timeout_seconds": 5,
    }
    saved_settings = api.request("PUT", "/v1/settings/models", model_settings)
    tested_settings = api.request(
        "POST",
        "/v1/settings/models/test",
        {
            "provider": "fake",
            "model_name": "fake-api-e2e",
            "dimension": 32,
            "timeout_seconds": 5,
        },
    )
    expect(
        saved_settings["desired"]["model_name"] == "fake-api-e2e",
        "model settings save failed",
    )
    expect(tested_settings["ok"] is True, "model settings probe failed")
    print("PASS model_settings")

    metrics = api.request("GET", f"/metrics?{urlencode({'tenant_id': str(TENANT)})}")
    expect("uam_memory_items_total" in metrics, "metrics missing memory counter")
    print("PASS metrics")

    print("api_e2e_eval=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
