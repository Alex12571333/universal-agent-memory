# Shared lifecycle contract

The shared integration contract describes *when* native agent plugins should
interact with Obelisk Memory. It does not assume a particular agent
framework.

Suggested hooks:

- `before_agent_run`: recall durable identity, project and task context.
- `before_model_call`: compile a small prompt/context package.
- `after_model_message`: retain durable observations or decisions.
- `after_tool_call`: retain useful tool outputs and error lessons.
- `on_checkpoint`: persist working state for recovery.
- `on_run_complete`: retain a summary and trigger background maintenance.
- `on_human_feedback`: promote, reject or supersede memory.

Implementation adapters should map these hooks onto the concrete runtime API of
OpenClaw, Hermes or another agent.
