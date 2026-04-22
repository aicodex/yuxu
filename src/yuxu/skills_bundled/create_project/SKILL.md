---
name: create_project
version: "1.0.0"
author: yuxu
license: MIT
description: Scaffold a new yuxu project at the given directory (yuxu.json, agents/, skills/, _system/, config/, data/, .gitignore, manifest).
triggers: [create project, new project, init project, scaffold project]
parameters:
  type: object
  required: [dir]
  properties:
    dir:
      type: string
      description: Target directory. Created if missing. Must not contain an existing yuxu.json unless force=true.
    force:
      type: boolean
      default: false
      description: Overwrite an existing yuxu.json in the target.
edit_warning: true
---
# create_project

Initialize a new yuxu project. Side effects:

1. Creates the directory (and `agents/`, `skills/`, `_system/`, `config/`, `data/{checkpoints,logs,memory,sessions}/`, `.yuxu/`).
2. Writes `yuxu.json` with the running yuxu version and inferred name (folder basename).
3. Drops a `.gitignore`, `config/rate_limits.yaml` template, `config/skills_enabled.yaml`.
4. Copies every shipped bundled agent into `_system/<agent>/`.
5. Records the project in `~/.yuxu/projects.yaml`.

Returns `{ok, path}` on success; `{ok: false, error}` on failure.
