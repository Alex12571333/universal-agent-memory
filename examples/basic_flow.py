"""Run a complete memory flow without external services."""

from uuid import uuid4

from memory_plane.bootstrap import build_in_memory_container
from memory_plane.contracts.dto import ContextRecipe, RecallQuery, RetainCommand
from memory_plane.domain.models import MemoryLayer, MemoryScope, Provenance

container = build_in_memory_container()
tenant_id = uuid4()
workspace_id = uuid4()
agent_id = uuid4()

for layer, text in (
    (MemoryLayer.CORE, "Always cite the source of release decisions."),
    (MemoryLayer.SEMANTIC, "Ivan owns the Alpha release."),
    (MemoryLayer.ERROR, "Do not publish Alpha without a rollback plan."),
):
    container.retention.retain(
        RetainCommand(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            layer=layer,
            scope=MemoryScope.WORKSPACE,
            kind="fact",
            text=text,
            provenance=Provenance(source_kind="example", origin_uri="chat://alpha"),
        )
    )

recall = container.retrieval.recall(
    RecallQuery(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        text="Who owns Alpha and what must we avoid?",
    )
)
context = container.context.compile(
    recall,
    ContextRecipe(
        operation="planner",
        budget_tokens=1000,
        layer_order=(MemoryLayer.SEMANTIC, MemoryLayer.ERROR),
    ),
)

print(context.render_markdown())
print(f"\ntrace={','.join(str(item_id) for item_id in context.trace_ids)}")
