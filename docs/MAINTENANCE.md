# Maintenance & Engineering Guide

This guide documents repository layout, design boundaries, testing, CI/CD workflows, and security invariants for maintainers and coding agents.

---

## Repository Structure

```text
.
├── .github/workflows/
│   ├── ci.yml                     # Lint, format check, compileall, bash -n, pytest (3.11 & 3.12)
│   └── deploy.yml                 # Manual workflow_dispatch: runs ci.yml then deploys via SSH
├── common/
│   └── utils.py                   # Ignored tree patterns, truncate_text, slice_lines, HTTP error formatting
├── deploy/
│   ├── deploy.sh                  # Idempotent Debian/Ubuntu setup & update script (LF only)
│   ├── caddy/Caddyfile.example    # Caddy template with log_skip & flush_interval -1
│   ├── nginx/mcp.example.com.conf # Nginx template with access_log off & proxy_buffering off
│   └── systemd/
│       ├── mcp-notes-web.service  # Systemd unit for servers.notes_web (:8101)
│       └── mcp-github.service     # Systemd unit for servers.github_personal (:8102)
├── docs/
│   ├── GEMINI_SETUP.md            # Token setup, Gemini registration, Saved Info cards & checks
│   └── MAINTENANCE.md             # This file
├── servers/
│   ├── notes_web.py               # Server 1: SiYuan Notes RPC wrapper (5) + Firecrawl v2 (12)
│   └── github_personal.py         # Server 2: GitHub REST API wrapper (23)
├── tests/
│   ├── test_bm25_recall.py        # Tool counts, annotations, BM25 recall, mock integration & ASGI HTTP smoke
│   └── test_repo_hygiene.py       # Markdown link checks, LF enforcement, and PII/desensitization guard
├── .env.example
├── .gitattributes
├── LICENSE
├── pyproject.toml
├── README.md
└── README_zh_CN.md
```

---

## Architectural Boundaries & Design Decisions

### 1. Why `servers/notes_web.py` Wraps SiYuan Notes Directly Instead of `create_proxy()`
The SiYuan Share MCP backend is stateless POST-only JSON-RPC (`GET`/`DELETE` return `405`) and enforces short-lived OAuth 2.1 access tokens externally. Direct `httpx.AsyncClient` RPC calls with a localhost-only `X-MCP-Internal-Token`:
- Avoid OAuth refresh drops;
- Let us write clean bilingual docstrings and English BM25 `Keywords:` lines on every wrapped tool;
- Keep upstream connection failures isolated (`isError=True` instead of crashing the server).

### 2. Why All 6 GitHub Write Tools Are Pinned in `always_visible`
`BM25SearchTransform` hides non-pinned tools behind a synthetic `call_tool(name, arguments)` proxy. MCP clients inspect `annotations` on the exposed tool (`call_tool`), not on hidden target tools, so hiding mutating tools behind `call_tool` strips `destructiveHint=True` confirmation prompts. Pinning all 6 write tools preserves native host confirmations while `GITHUB_ALLOWED_WRITE_OWNERS` enforces server-side owner allowlisting (fail-closed if empty).

### 3. Why `SERVER_INSTRUCTIONS` Stays Positive While Saved Info Enforces Restraint
Gemini's pre-router evaluates App name, `instructions`, and exposed tool descriptions **before** built-in web search or Saved Info run. Putting negative clauses (`ONLY invoke...`, `Do NOT use...`, `only when built-in browsing fails`) inside `SERVER_INSTRUCTIONS` causes the pre-router to skip mounting the MCP server on turn 1. Keep `SERVER_INSTRUCTIONS` purely descriptive and positive; place restraint rules (such as avoiding unsolicited notes lookups) in client-side Saved Info.

### 4. Local Cache Pagination vs. Single-Read Ceiling
`web_scrape` and `web_scrape_pdf` cache full extracted Markdown in an in-memory `OrderedDict` (LRU + TTL). Every response clamps `max_chars` to `[1000, MAX_SINGLE_READ_CHARS]` so a single tool call cannot overflow the client context window, while `offset` and `find_keyword` allow reading the entire document from local memory at zero extra Firecrawl credits.

---

## Development & Verification Commands

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run python -m compileall -q common servers tests
uv run pytest -v
```

On Linux/macOS or Git Bash, also validate shell syntax:
```bash
bash -n deploy/deploy.sh
```

---

## CI/CD Workflows

| Workflow | Trigger | Purpose |
| :--- | :--- | :--- |
| [ci.yml](../.github/workflows/ci.yml) | `push` to `main`, `pull_request` to `main`, `workflow_dispatch`, `workflow_call` | Runs Ruff lint & format checks, Python bytecode compilation, `bash -n deploy/deploy.sh`, and `pytest` on Python `3.11` and `3.12`. |
| [deploy.yml](../.github/workflows/deploy.yml) | Manual `workflow_dispatch` | Re-runs `ci.yml`, then connects via SSH (`HOST`, `PORT`, `USERNAME`, `SSH_KEY` repository secrets) to fast-forward `/opt/personal-mcp` and run `NON_INTERACTIVE=1 bash deploy/deploy.sh`. |

---

## Security Checklist

- Never commit `.env` or `.venv/`.
- Keep `access_log off;` (Nginx) or `log_skip` (Caddy) on both 64-hex capability endpoints so path tokens are never written to disk logs.
- Keep `proxy_buffering off;` (Nginx) / `flush_interval -1` (Caddy) for Streamable HTTP streaming compatibility.
- On the public SiYuan Share virtual host, reject or strip external `X-MCP-Internal-Token` headers.
- Keep `GITHUB_ALLOWED_WRITE_OWNERS` restricted to your own account(s).
