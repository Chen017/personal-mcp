<h1 align="center">Personal MCP Suite</h1>

<p align="center">
  Self-hosted dual <strong>FastMCP</strong> servers (Personal Notes + Firecrawl Web + GitHub) with <strong>BM25 tool folding</strong> and <strong>Stateless Streamable HTTP</strong>, built for Google Gemini Custom Apps and standard MCP clients.
</p>

<p align="center">
  <a href="README.md">English</a> · <a href="README_zh_CN.md">简体中文</a>
</p>

<p align="center">
  <a href="https://github.com/Chen017/personal-mcp/actions/workflows/ci.yml"><img src="https://github.com/Chen017/personal-mcp/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/Python-%3E%3D3.11-3776ab.svg" alt="Python >= 3.11">
  <img src="https://img.shields.io/badge/FastMCP-4.x-10b981.svg" alt="FastMCP 4.x">
</p>

---

## ✨ Why This Exists

Connecting personal tools to cloud AI clients (such as Gemini Web & iOS Custom Apps) often runs into three practical bottlenecks:

1. **OAuth refresh drops on mobile & session expiry**: Stateful HTTP sessions and consumer OAuth connectors frequently lose sessions after idle periods or service restarts.
2. **Router isolation & tool bloat**: Exposing 30–40+ tools across multiple connectors bloats context schemas and lowers selection accuracy, while splitting closely related knowledge/web tools prevents fallback scraping inside the same conversation.
3. **Unbounded page dumps & unsafe write delegation**: Raw web scrapes or large files blow up context windows, while hiding mutating tools behind a generic proxy strips `destructiveHint=True` confirmation prompts.

This repository solves those with two isolated, stateless FastMCP servers behind capability URLs and reverse-proxy log redaction.

---

## 🏗️ Architecture

```text
                Gemini (Web / iOS) or any Streamable HTTP MCP Client
                                        │
                        HTTPS POST (Stateless JSON-RPC 2.0)
                                        ▼
                      Reverse Proxy (Nginx / Caddy + TLS)
                  ├── /<SECRET_PATH_NOTES>/mcp  ──► 127.0.0.1:8101/mcp (access_log off)
                  └── /<SECRET_PATH_GITHUB>/mcp ──► 127.0.0.1:8102/mcp (access_log off)
                                        │
          ┌─────────────────────────────┴─────────────────────────────┐
          ▼                                                           ▼
┌───────────────────────────────────────┐   ┌───────────────────────────────────────┐
│ Server 1: Personal Notes Web (:8101)  │   │ Server 2: GitHub Personal (:8102)     │
│ systemd: mcp-notes-web.service        │   │ systemd: mcp-github.service           │
│                                       │   │                                       │
│ • 17 backend tools -> 8 exposed       │   │ • 25 backend tools -> 13 exposed      │
│ • SiYuan Notes RPC client (5 tools)   │   │ • Shared httpx -> GitHub REST API     │
│ • Firecrawl v2 wrapper (12 tools)     │   │ • Fine-Grained PAT + owner allowlist  │
│ • Local LRU+TTL cache + offset paging │   │ • ALL 7 write tools pinned directly   │
│ • BM25SearchTransform (Markdown)      │   │ • BM25SearchTransform (Markdown)      │
└───────────────────────────────────────┘   └───────────────────────────────────────┘
          │
          ▼ (Localhost only, header stripped on public proxy)
┌───────────────────────────────────────┐
│ Optional: SiYuan Share Backend (:8081)│
└───────────────────────────────────────┘
```

### Tool Inventory & Exposure Policy

| Server | Port | Exposed (`list_tools`) | Backend Total | Pinned Tools (`always_visible`) | BM25 Hidden Tools (`search_tools` + `call_tool`) |
| :--- | :---: | :---: | :---: | :--- | :--- |
| **Personal Notes Web** (`servers.notes_web`) | `127.0.0.1:8101` | **8** | **17** | `notes_search_notes`, `notes_read_document`, `notes_list_shares`, `web_search`, `web_scrape`, `web_map` | `notes_get_share_tree`, `notes_get_document_outline`, `web_crawl`, `web_crawl_status`, `web_batch_scrape`, `web_extract_structured`, `web_scrape_pdf`, `web_interact_start`, `web_interact_action`, `web_interact_stop`, `web_check_credit_usage` |
| **GitHub Personal** (`servers.github_personal`) | `127.0.0.1:8102` | **13** | **25** | **4 Read**: `gh_list_repos`, `gh_get_repo_tree`, `gh_get_file_contents`, `gh_search_code`<br>**7 Write (`destructiveHint=True`)**: `gh_create_or_update_file`, `gh_patch_files`, `gh_create_pull_request`, `gh_create_branch`, `gh_push_multiple_files`, `gh_create_issue`, `gh_comment_issue_or_pr` | **14 Read / Download**: `gh_get_repo_info`, `gh_list_branches`, `gh_list_commits`, `gh_get_commit_diff`, `gh_compare_commits`, `gh_list_issues`, `gh_get_issue`, `gh_list_pull_requests`, `gh_get_pull_request_detail`, `gh_list_releases`, `gh_list_workflow_runs`, `gh_get_workflow_run_jobs`, `gh_get_archive_download_url`, `gh_fetch_directory_bundle` |

---

## 🛡️ Key Engineering Safeguards

- **Stateless Streamable HTTP (`stateless_http=True`)**: Every JSON-RPC call is self-contained with zero in-memory session affinity, preventing `404 Session terminated` errors across mobile reconnects or `systemd` restarts.
- **Write Safety Preserved**: All 7 GitHub mutating tools are pinned in `always_visible` with `destructiveHint=True` (never hidden behind `call_tool`) and enforced server-side by `GITHUB_ALLOWED_WRITE_OWNERS` (fail-closed if unconfigured).
- **Server-Side Patching & Overwrite Guardrails**: `gh_patch_files` fetches full existing files server-side and applies strict unique search-and-replace edits in a single atomic commit (preserving CRLF/LF and file modes) without requiring the LLM to output entire large files; `gh_create_or_update_file` and `gh_push_multiple_files` block accidental large-file truncation overwrites unless `allow_large_overwrite=True` and support atomic file deletions via `delete_paths`.
- **Zero-Credit Local Cache Pagination**: `web_scrape` and `web_scrape_pdf` cache full extracted Markdown in an LRU + TTL memory store (`WEB_CACHE_TTL_SECONDS=1800`). Follow-up `offset` reads and `find_keyword` jumps consume **0 extra Firecrawl credits**, while each read is capped at `MAX_SINGLE_READ_CHARS` (default `32000`).
- **Soft Guardrails Without Dead-Ends**: `web_search` is hard-capped at `limit <= 10` without `scrapeOptions` (strictly 2 credits per call, with identical repeat queries cached locally) as a second-opinion fallback behind built-in search; `web_crawl`, `web_batch_scrape`, and `web_extract_structured` pause and return `needs_confirmation=True` above configurable thresholds unless `confirm_large=True` is passed.
- **Privacy & Log Redaction**: Capability URLs use 64-hex random paths paired with `access_log off` (Nginx) / `log_skip` (Caddy) and `proxy_buffering off`.

---

## 🚀 Quick Start (Debian / Ubuntu)

### 1. One-Command Install & Update

```bash
curl -fsSL https://raw.githubusercontent.com/Chen017/personal-mcp/main/deploy/deploy.sh | bash
```

The idempotent script installs `uv`, syncs Python dependencies into `/opt/personal-mcp`, creates `.env` (`chmod 600`) with generated random secrets on first run, installs and starts `mcp-notes-web.service` and `mcp-github.service`, verifies HTTP `200` on `127.0.0.1:8101/mcp` and `127.0.0.1:8102/mcp`, and prints ready-to-use Nginx/Caddy snippets. Re-run `bash /opt/personal-mcp/deploy/deploy.sh` anytime to pull updates without overwriting `.env`.

### 2. Configure Secrets & Reverse Proxy

1. Edit `/opt/personal-mcp/.env` (see [.env.example](.env.example)):
   - Set `GITHUB_PAT`, `GITHUB_DEFAULT_OWNER`, and `GITHUB_ALLOWED_WRITE_OWNERS`.
   - Set `FIRECRAWL_API_KEY`.
   - Optionally set `NOTES_RPC_URL`, `NOTES_INTERNAL_TOKEN`, `NOTES_SITE_DOMAIN`, and `BLOG_SITE_DOMAIN` if integrating a SiYuan Share instance.
2. Restart services:
   ```bash
   sudo systemctl restart mcp-notes-web mcp-github
   ```
3. Configure HTTPS reverse proxy using [deploy/nginx/mcp.example.com.conf](deploy/nginx/mcp.example.com.conf) or [deploy/caddy/Caddyfile.example](deploy/caddy/Caddyfile.example).
4. Follow the step-by-step client guide in [docs/GEMINI_SETUP.md](docs/GEMINI_SETUP.md) to register both Custom Apps, sync schemas in Developer Mode, and apply the Saved Info routing cards.

---

## 🧪 Local Development & CI

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest -v
```

GitHub Actions ([ci.yml](.github/workflows/ci.yml)) runs Ruff lint/format checks, Python bytecode compilation, shell syntax validation (`bash -n deploy/deploy.sh`), hygiene/privacy guards, and the full pytest suite across Python `3.11` and `3.12`. See [docs/MAINTENANCE.md](docs/MAINTENANCE.md) for architecture details and troubleshooting.

---

## 📄 License

[MIT](LICENSE)
