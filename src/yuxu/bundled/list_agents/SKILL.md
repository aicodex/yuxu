---
name: list_agents
version: "1.0.0"
author: yuxu
license: MIT
description: List agents inside a yuxu project — bundled (under _system/) and user (under agents/) — in scan_order.
triggers: [list agents, show agents, agents in project]
parameters:
  type: object
  required: [project_dir]
  properties:
    project_dir:
      type: string
      description: Path to the yuxu project root (must contain yuxu.json).
---
# list_agents

For the given project_dir, walk each directory in `yuxu.json`'s `scan_order`
(default: `_system` then `agents`) and return one record per agent folder:

```
[{name, source: "bundled" | "user", path}, ...]
```

A directory counts as an agent if it has either `AGENT.md` or `__init__.py`.
Folders prefixed with `.` or `_` are skipped.
