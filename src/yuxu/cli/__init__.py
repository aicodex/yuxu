"""yuxu CLI — thin ops surface (serve / init / status).

End users don't touch this; they interact via chat frontend through the
`gateway` + `shell` agents. This CLI exists for:
  - First-time `~/.yuxu/` setup on any invocation
  - Running the daemon (`yuxu serve`)
  - Scaffolding a new project directory (`yuxu init`)
  - Quick process health check (`yuxu status`)
"""
