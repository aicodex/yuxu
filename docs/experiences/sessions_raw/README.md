# sessions_raw — verbatim Claude Code session JSONLs

**Local archive only. Not checked into git** (see `.gitignore`).

## What

Every Claude Code session writes a full JSONL transcript to
`~/.claude/projects/<sanitized-cwd>/<session-uuid>.jsonl`. These
contain the complete turn-by-turn history: user messages, assistant
responses, tool calls, tool results, thinking blocks — verbatim, not
summarized.

For the iteration agent this is the **highest-fidelity training data**
available. Hand-curated `workflows/` and `methodologies/` distill the
signal; replays capture specific failures; but the raw JSONL retains
everything, including the unused signals that might turn out to matter
later.

## Why not git

Each session is 1–10 MB. Over months the corpus would balloon past
git's comfortable limits. We version only the distilled artifacts
(workflows / methodologies / replays); the raw JSONL stays local.

If we later need a subset of sessions to be versioned (e.g. a
benchmark suite for the iteration agent), we'll curate those
explicitly and version them — probably gzipped and under a separate
path.

## How to archive a session

Two paths:

### One-shot (manual)

```bash
bash yuxu/tools/archive_session.sh
```

This picks the most recently modified JSONL from
`~/.claude/projects/-home-xzp-project-theme-flow-engine/`, copies it
here with a `YYYY-MM-DD-<uuid-prefix>.jsonl` filename, and prints the
destination.

### Auto (not yet built)

A future Claude Code hook could call this on session exit. For now
it's manual — run the script when you remember, ideally at the end
of any session that taught you something.

## Naming convention

`YYYY-MM-DD-<uuid-prefix>.jsonl`
- date = the day the session started
- uuid-prefix = first 8 chars of the session UUID (enough to be unique
  in practice, short enough to type)

Example: `2026-04-24-c9a03460.jsonl`

## Reading a JSONL entry

Each line is a JSON object. Common shapes:

```json
{"type":"user-prompt","timestamp":"...","content":"..."}
{"type":"assistant-message","timestamp":"...","content":[{"type":"text","text":"..."}]}
{"type":"tool-call","tool":"Read","args":{...}}
{"type":"tool-result","toolUseId":"...","content":"..."}
{"type":"queue-operation","operation":"..."}
```

The `type` field is the dispatcher. For extracting reasoning: look
for `content` array entries with `"type":"thinking"`. For extracting
tool usage: filter `type:"tool-call"`.

## For the future iteration agent

When memory infra Phase 1 lands, this folder's files become inputs
to a replay extractor that can:

1. Scan for challenge-correction loops (user pushes back after a
   suboptimal response).
2. Auto-generate replay case drafts from the raw data.
3. Score my historical decisions against the corpus.

Until then: raw archive for future analysis.
