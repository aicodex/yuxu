---
name: list_projects
version: "1.0.0"
author: yuxu
license: MIT
description: List yuxu projects registered in ~/.yuxu/projects.yaml with name, version, and existence flag.
triggers: [list projects, show projects, my projects]
parameters:
  type: object
  properties: {}
---
# list_projects

Read `~/.yuxu/projects.yaml` and return one record per registered project:

```
[{path, exists, name?, yuxu_version?}, ...]
```

`name` and `yuxu_version` come from the project's `yuxu.json`; missing on
projects whose directory has been deleted or whose config is corrupt.
`exists` is True iff the project directory still exists.
