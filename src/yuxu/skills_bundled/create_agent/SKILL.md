---
name: create_agent
description: Scaffold a new user agent inside a yuxu project, copying a template into agents/<name>/ and substituting the agent name placeholder.
triggers: [create agent, new agent, scaffold agent, add agent]
parameters:
  type: object
  required: [project_dir, name]
  properties:
    project_dir:
      type: string
      description: Path to the yuxu project root (must contain agents/).
    name:
      type: string
      description: Agent folder name (also used as default bus address).
    template:
      type: string
      default: default
      description: Template subfolder under yuxu/templates/ (default → templates/agent/).
edit_warning: true
---
# create_agent

Copy a template into `<project_dir>/agents/<name>/`. Substitutes the
placeholder `NAME = "my_agent"` in `__init__.py` with the chosen name.

Returns `{ok, path}` on success; `{ok: false, error}` on:
- project_dir has no `agents/` (not a yuxu project)
- agent folder already exists
- unknown template
