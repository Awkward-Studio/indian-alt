# Production agent runtime

This package owns the contracts and configuration for agent execution. The
manual implementation in `ai_orchestrator.agent_demo` remains a CLI experiment
and must not be imported by production endpoints.

## Compatibility

- Pydantic AI is pinned to `pydantic-ai-slim[openai]==2.38.0`.
- The current T4 runtime uses the OpenAI Chat Completions protocol exposed by
  llama.cpp with `gemma-4-12b-it-q8`.
- `AGENT_RUNTIME_ENABLED` and `AGENT_RUNTIME_SHADOW_ENABLED` default to false.
- `AGENT_RUNTIME_HTTP_MAX_RETRIES` defaults to zero. Agent-level retry and
  budget policy owns retries so the OpenAI client does not multiply requests.
- A newer SDK version requires the focused contract suite and the live Gemma 4
  llama.cpp tool-calling smoke test before the pin changes.

## Configuration

The runtime inherits its URL, API key, model, and timeouts from the existing
`VLLM_*` setting names. Those names now point to the llama.cpp T4 server. Every
value also has an `AGENT_RUNTIME_*` override. Zero for
`AGENT_RUNTIME_TOTAL_TOKENS_LIMIT` means no aggregate token ceiling. Request and
tool-call limits still apply.

Run `python manage.py smoke_test_agent_tool_calling` after changing the GGUF,
llama.cpp image, or chat template. The command checks `/props` and then requires
a native OpenAI-compatible `tool_calls` response. An ordinary `/v1/models`
health check is not enough to enable agent traffic.

Application code must create `AgentDependencies` from authenticated server
state. Client or model input cannot choose `requested_by_id`,
`allowed_deal_ids`, or the allowed capability set. Production entry points must
use `AgentAuthorizationService.build_dependencies`; it binds the audit and
optional conversation to the authenticated Django user, rejects unsupported
capabilities, and derives analyst deal access from active profile
responsibility (administrators retain their existing wider scope).

## Capabilities and rollout

The default registry exposes only `deals.read` and `documents.search`. Both
start from querysets filtered by the immutable `allowed_deal_ids` dependency;
model-supplied IDs can only narrow that set. Responses contain bounded excerpts
and stable `deal:<uuid>` or `chunk:<uuid>` handles for citations. There is no
generic ORM, SQL, shell, filesystem, or network tool.

`AgentShadowRunner` is an opt-in comparison boundary. With shadow mode off it
constructs no runtime and makes no model request. With shadow mode on it records
only duration, usage, terminal state, answer length, and normalized equality in
the originating audit log's `source_metadata.agent_shadow` field. It neither
returns the shadow answer to the user nor persists the shadow answer text.
