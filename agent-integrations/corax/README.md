# Corax memory provider

This directory is an installable Corax `memory_provider`, not an LLM tool. It
maps `agent.memory/v1` to the self-hosted UAM `/retain` and `/recall` APIs.

Configure Corax with path
`../universal-agent-memory/agent-integrations/corax`, bind `memory` to
`memory.uam`, and set `UAM_URL`, `UAM_API_KEY`, `UAM_TENANT_ID`,
`UAM_WORKSPACE_ID`, and optionally `UAM_AGENT_ID`.

`forget` deliberately fails closed because UAM uses reviewed supersede/privacy
workflows instead of destructive agent-initiated deletion.
