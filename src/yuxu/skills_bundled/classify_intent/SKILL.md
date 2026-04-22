---
name: classify_intent
description: Given a natural-language request to build a new yuxu agent, classify it — pick a template, suggest a folder name, propose run_mode, and list likely depends_on. Output is structured JSON consumed by generate_agent_md / agent-creator flows.
triggers: [classify intent, what kind of agent, new agent type, agent template]
parameters:
  type: object
  required: [description]
  properties:
    description:
      type: string
      description: Free-form natural-language description of what the user wants the new agent to do.
    available_templates:
      type: array
      items: {type: string}
      default: [default]
      description: Templates the caller is willing to use (folder names under yuxu/templates/).
    pool:
      type: string
      description: rate_limit pool name (defaults to env CLASSIFY_INTENT_POOL or NEWSFEED_POOL or "openai").
    model:
      type: string
      description: model name (defaults to env CLASSIFY_INTENT_MODEL or TFE_MODEL or "gpt-4o-mini").
---
# classify_intent

LLM-mediated classifier. The skill calls `llm_driver` once with `json_mode=true`
and asks the model to fill a fixed schema:

```json
{
  "agent_type": "<one of available_templates>",
  "suggested_name": "<snake_case>",
  "run_mode": "one_shot | persistent | scheduled | triggered | spawned",
  "depends_on": ["<bundled or user agent name>", ...],
  "driver": "python | llm | hybrid",
  "reasoning": "<one or two sentences>"
}
```

Returns `{ok: True, classification: {...}}` on success. On LLM failure /
malformed JSON, returns `{ok: False, error: ..., raw: ...}` so callers can
retry or surface the model output for inspection.

This is **paired with `generate_agent_md`**: classify_intent picks the type,
generate_agent_md fills in the AGENT.md text using that classification.
