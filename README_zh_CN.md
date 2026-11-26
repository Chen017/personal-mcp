<h1 align="center">Personal MCP Suite</h1>

<p align="center">
  面向 Google Gemini Custom Apps 与标准 MCP 客户端的个人自建双 <strong>FastMCP</strong> 服务端（个人笔记 + Firecrawl v2 网页抓取 + GitHub 全功能读写），支持 <strong>BM25 工具折叠</strong>与<strong>无状态 Streamable HTTP</strong>。
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

## ✨ 解决什么问题

把个人知识库、强力网页抓取和 GitHub 仓库接入云端 AI 客户端（如 Gemini Web / iOS Custom Apps）时，通常会遇到三类实际痛点：

1. **移动端 OAuth 断连与有状态会话失效**：标准 OAuth Refresh Token 刷新失败或服务端内存 Session 空闲回收/重启后，容易在跨设备或长间隔续聊时静默掉线。
2. **工具膨胀与跨能力路由孤岛**：三四十个工具全部平铺会显著拉低模型选择准确率；而把密切相关的笔记和网页抓取拆开，又容易在主站触发访问限制时无法无缝兜底。
3. **长文本撑爆上下文 & 写操作安全退化**：未截断的网页/代码会快速耗尽上下文；把写操作藏在通用代理之后则会抹平 `destructiveHint=True` 原生二次确认提示。

本项目通过两个物理隔离、无状态运行的 FastMCP 服务与高熵路径鉴权解决上述问题。

---

## 🏗️ 架构总览

```text
                Gemini (Web / iOS) 或任意 Streamable HTTP MCP 客户端
                                        │
                        HTTPS POST (无状态 JSON-RPC 2.0)
                                        ▼
                      反向代理 (Nginx / Caddy + TLS)
                  ├── /<SECRET_PATH_NOTES>/mcp  ──► 127.0.0.1:8101/mcp (关闭访问日志)
                  └── /<SECRET_PATH_GITHUB>/mcp ──► 127.0.0.1:8102/mcp (关闭访问日志)
                                        │
          ┌─────────────────────────────┴─────────────────────────────┐
          ▼                                                           ▼
┌───────────────────────────────────────┐   ┌───────────────────────────────────────┐
│ Server 1: Personal Notes Web (:8101)  │   │ Server 2: GitHub Personal (:8102)     │
│ systemd: mcp-notes-web.service        │   │ systemd: mcp-github.service           │
│                                       │   │                                       │
│ • 后端 17 工具 -> 对外暴露 8 个       │   │ • 后端 25 工具 -> 对外暴露 13 个      │
│ • 思源分享 JSON-RPC 客户端 (5 工具)   │   │ • 复用 httpx -> GitHub REST API       │
│ • Firecrawl v2 全功能封装 (12 工具)   │   │ • Fine-Grained PAT + Owner 写白名单   │
│ • 本地 LRU+TTL 缓存 + offset 续读     │   │ • 全部 7 个写工具置顶直出             │
│ • BM25SearchTransform (Markdown)      │   │ • BM25SearchTransform (Markdown)      │
└───────────────────────────────────────┘   └───────────────────────────────────────┘
          │
          ▼ (仅本机回环直连，公网反代强制剥离静态鉴权头)
┌───────────────────────────────────────┐
│ 可选：思源分享后端 (:8081)            │
└───────────────────────────────────────┘
```

### 工具分布与暴露策略

| 服务 | 监听地址 | 对外暴露 (`list_tools`) | 后端总工具 | 置顶工具 (`always_visible`) | BM25 隐藏工具 (`search_tools` + `call_tool`) |
| :--- | :---: | :---: | :---: | :--- | :--- |
| **Personal Notes Web** (`servers.notes_web`) | `127.0.0.1:8101` | **8** | **17** | `notes_search_notes`, `notes_read_document`, `notes_list_shares`, `web_search`, `web_scrape`, `web_map` | `notes_get_share_tree`, `notes_get_document_outline`, `web_crawl`, `web_crawl_status`, `web_batch_scrape`, `web_extract_structured`, `web_scrape_pdf`, `web_interact_start`, `web_interact_action`, `web_interact_stop`, `web_check_credit_usage` |
| **GitHub Personal** (`servers.github_personal`) | `127.0.0.1:8102` | **13** | **25** | **4 读**：`gh_list_repos`, `gh_get_repo_tree`, `gh_get_file_contents`, `gh_search_code`<br>**7 写 (`destructiveHint=True`)**：`gh_create_or_update_file`, `gh_patch_files`, `gh_create_pull_request`, `gh_create_branch`, `gh_push_multiple_files`, `gh_create_issue`, `gh_comment_issue_or_pr` | **14 只读/下载**：`gh_get_repo_info`, `gh_list_branches`, `gh_list_commits`, `gh_get_commit_diff`, `gh_compare_commits`, `gh_list_issues`, `gh_get_issue`, `gh_list_pull_requests`, `gh_get_pull_request_detail`, `gh_list_releases`, `gh_list_workflow_runs`, `gh_get_workflow_run_jobs`, `gh_get_archive_download_url`, `gh_fetch_directory_bundle` |

---

## 🛡️ 核心工程保障

- **无状态 Streamable HTTP（`stateless_http=True`）**：每个 JSON-RPC 请求独立处理，服务端重启或移动端长时间休眠后不会出现 `404 Session terminated`。
- **写操作原生确认与白名单双保险**：全部 7 个 GitHub 写工具均直出并标记 `destructiveHint=True`（绝不藏进 BM25 代理），且由服务端 `GITHUB_ALLOWED_WRITE_OWNERS` 硬限制（未配置时默认拒绝所有写操作）。
- **服务端局部补丁与防截断覆盖护栏**：`gh_patch_files` 在服务端内存拉取完整文件并执行严格唯一匹配的局部替换与单次原子提交（保留原文件 CRLF/LF 与权限位），彻底避免大模型整文件输出截断；`gh_create_or_update_file` 与 `gh_push_multiple_files` 默认拦截对大文件的危险缩减/截断覆盖（除非显式传入 `allow_large_overwrite=True`），并支持通过 `delete_paths` 原子删除或重命名文件。
- **0 额度本地缓存分页续读**：`web_scrape` 与 `web_scrape_pdf` 在抓取一次后将完整 Markdown 缓存在本地 LRU + TTL 内存中（默认 30 分钟）。后续传 `offset` 翻页或 `find_keyword` 页内定位均命中缓存，**消耗 0 额外 Firecrawl 额度**，单次返回上限受 `MAX_SINGLE_READ_CHARS`（默认 `32000`）保护。
- **软上限护栏而不堵死大任务**：`web_search` 在代码层强制禁止 `scrapeOptions` 且 `limit <= 10`（严格固定 2 credits/次，同查询本地缓存命中 0 消耗），作为内置搜索后的二次核查兜底；`web_crawl`、`web_batch_scrape` 与 `web_extract_structured` 超出默认阈值时先返回 `needs_confirmation=True`，确认后传 `confirm_large=True` 即可放行。
- **隐私与防探测**：Capability URL 采用 64 位随机十六进制路径，搭配反代 `access_log off`（Nginx）/ `log_skip`（Caddy）和 `proxy_buffering off`。

---

## 🚀 快速部署（Debian / Ubuntu）

### 1. 一键安装与更新

```bash
curl -fsSL https://raw.githubusercontent.com/Chen017/personal-mcp/main/deploy/deploy.sh | bash
```

脚本为幂等设计：自动安装 `uv`、同步 `/opt/personal-mcp` 依赖、首次生成 `.env`（`chmod 600`）与随机密钥、安装并启动 `mcp-notes-web.service` 和 `mcp-github.service`、完成 `8101/8102` 本地健康检查，并直接打印带真实路径的 Nginx/Caddy 配置模板。后续拉取最新代码，重跑 `bash /opt/personal-mcp/deploy/deploy.sh` 即可，已有 `.env` 不会被覆盖。

### 2. 配置密钥与反向代理

1. 编辑 `/opt/personal-mcp/.env`（参考 [.env.example](.env.example)）：
   - 填写 `GITHUB_PAT`、`GITHUB_DEFAULT_OWNER` 与 `GITHUB_ALLOWED_WRITE_OWNERS`；
   - 填写 `FIRECRAWL_API_KEY`；
   - 如需接入思源分享后端，填写 `NOTES_RPC_URL`、`NOTES_INTERNAL_TOKEN`、`NOTES_SITE_DOMAIN` 与 `BLOG_SITE_DOMAIN`。
2. 重启服务生效：
   ```bash
   sudo systemctl restart mcp-notes-web mcp-github
   ```
3. 参考 [deploy/nginx/mcp.example.com.conf](deploy/nginx/mcp.example.com.conf) 或 [deploy/caddy/Caddyfile.example](deploy/caddy/Caddyfile.example) 配置 HTTPS 反向代理。
4. 按照 [docs/GEMINI_SETUP.md](docs/GEMINI_SETUP.md) 中的完整教程生成最小权限 GitHub PAT、注册 Gemini Custom Apps、在 Developer Mode 同步 Schema 并配置路由卡片。

---

## 🧪 本地开发与 CI 验证

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest -v
```

GitHub Actions（[ci.yml](.github/workflows/ci.yml)）在每次推送与 PR 时跨 Python `3.11` / `3.12` 自动运行 Ruff 检查、字节码编译、`deploy.sh` 语法检查、仓库卫生/脱敏守卫以及完整 `pytest` 回归。更多结构与排障细节见 [docs/MAINTENANCE.md](docs/MAINTENANCE.md)。

---

## 📄 开源协议

[MIT](LICENSE)
