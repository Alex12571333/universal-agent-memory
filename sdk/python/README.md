# UAM Python client

```bash
pip install ./sdk/python
```

```python
from uam_client import MemoryClient, RecallRequest, RetainRequest

memory = MemoryClient("http://localhost:8080")
saved = memory.retain(
    RetainRequest(
        text="Release checklist lives in docs/release.md",
        agent_id="11111111-1111-1111-1111-111111111111",
    )
)
context = memory.recall(RecallRequest(query="Where is the release checklist?"))
print(saved.id, context.context.markdown)
```

`retain()` generates one idempotency key before retrying. The default retry
policy handles network failures, 429, 502, 503 and 504 with bounded exponential
backoff. HTTP failures are exposed as typed `MemoryServerError` subclasses.
