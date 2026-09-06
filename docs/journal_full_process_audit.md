# Full process audit — 2026-09-06

Brief: audit every process end to end, fix what is broken, and strengthen the unit +
regression suites so each fix ships with the test that would have caught it.

## Before state (measured, not assumed)

```
git:      main @ 24b8c1a "docs: attachments verified live (image described, CSV answered)"
version:  jarvis-core 2.0.0a8 / jarvis-web 2.0.0-alpha.1
python:   uv run pytest -q                -> 187 passed
web:      npm run test -- --run           -> 36 passed (5 files)
lint:     uv run ruff check packages      -> All checks passed
types:    uv run pyright packages         -> 0 errors, 0 warnings
coverage: 83% overall (8567 statements, 1474 uncovered)
```

Weakest modules by line coverage at the start:

| module | cov |
|---|---|
| `host/audio.py` | 26% |
| `host/screen.py` | 45% |
| `host/status.py` | 48% |
| `core/api/features.py` | 49% |
| `core/api/media.py` | 50% |
| `core/features/boards.py` | 57% |
| `core/features/collab.py` | 68% |
| `core/features/knowledge.py` | 69% |
| `core/features/skills.py` | 73% |
| `core/features/schedules.py` | 75% |

Coverage is a map of where nobody has looked, not a defect count. The audit reads the
processes; coverage only says which rooms are dark.

## Processes in scope

1. Agent loop / run engine / supervision (the thing every other process runs on)
2. Context assembly + prompt-cache prefix discipline
3. Schedules (cron, catch-up, dry runs)
4. Triage (mail routing)
5. RSVP (calendar invites)
6. Meetings (capture -> transcribe -> summarise)
7. Attachments (images, PDFs, pasted text)
8. Knowledge graph + boards + skills
9. Tools: registry, MCP provider, WS provider, facades
10. Model adapters (ollama, openai-compat, http)
11. Host: Outlook, OneNote, files, shell, audio, status
12. HTTP surface: rest, ws, media, features, collab, openai-compat

## Findings

(filled in as the audit proceeds)
