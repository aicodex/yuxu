"""yuxu shipped skills (CC-style: SKILL.md + handler.py per folder).

Skills here have global scope. Project-scoped skills go in
`<project>/skills/`; agent-scoped skills go in `<agent_dir>/skills/`.
SkillRegistry skips files / underscored directories at this level, so
`_shared.py` (cross-skill helpers) is safe to live next to skill folders.
"""
