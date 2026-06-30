# UAM TypeScript client

```bash
npm install ./sdk/typescript
```

```typescript
import { MemoryClient } from "@uam/client";

const memory = new MemoryClient({ baseUrl: "http://localhost:8080" });
const saved = await memory.retain({
  text: "Release checklist lives in docs/release.md",
  agent_id: "11111111-1111-1111-1111-111111111111",
});
const context = await memory.recall({
  query: "Where is the release checklist?",
});
console.log(saved.id, context.context.markdown);
```

The client uses the platform `fetch`, generates a stable idempotency key per
retain call, performs bounded retries, and exports typed request, response, and
error classes.
