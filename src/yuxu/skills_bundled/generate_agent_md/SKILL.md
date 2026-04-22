---
name: generate_agent_md
description: Generate the AGENT.md text (frontmatter + body) for a new yuxu agent given a description and a classification (output of classify_intent). Returns raw text plus parsed frontmatter so the caller can validate before writing to disk.
triggers: [generate AGENT.md, write agent markdown, scaffold agent body]
parameters:
  type: object
  required: [name, description]
  properties:
    name:
      type: string
      description: Agent folder name (snake_case). Becomes the bus address.
    description:
      type: string
      description: Free-form summary of what the agent should do.
    run_mode:
      type: string
      default: one_shot
      description: one_shot / persistent / scheduled / triggered / spawned
    driver:
      type: string
      default: python
      description: python / llm / hybrid
    depends_on:
      type: array
      items: {type: string}
      default: []
      description: Other agents this one will need (bus addresses).
    scope:
      type: string
      default: user
      description: user (default) or system (only for bundled)
    extra_hints:
      type: string
      description: Any additional context the LLM should bake into the body (e.g. example I/O, constraints, related skills).
    pool:
      type: string
      description: rate_limit pool override.
    model:
      type: string
      description: model override.
---
# generate_agent_md

LLM-mediated AGENT.md scaffolder. Calls `llm_driver` once with the chosen
fields baked into the system prompt, asks the model to emit a complete
AGENT.md body, then parses the frontmatter back to verify it round-trips.

Output:

```
{
  "ok": true,
  "agent_md": "<full text>",
  "frontmatter": {...},
  "body": "<markdown body>",
  "warnings": ["..."]   // non-fatal issues (e.g. driver mismatch, missing section)
}
```

If the model emits malformed frontmatter, returns
`{ok: false, error, raw}` so the caller can retry.

Pair with `classify_intent`: the classification's `agent_type / run_mode /
driver / depends_on / suggested_name` map directly into this skill's fields.
