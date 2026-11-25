# Personal Dual FastMCP Servers (`Etalib Web` + `GitHub Personal`)

个人自建双 MCP 服务（基于 **FastMCP 4 + Stateless Streamable HTTP + BM25SearchTransform**），面向 Debian Linux 服务器与 Google Gemini Custom Connected Apps（Web / iOS）。

## 一键部署（Debian）

```bash
curl -fsSL https://raw.githubusercontent.com/Chen017/personal-mcp/main/deploy/deploy.sh | bash
```

部署脚本默认将项目安装到 `/opt/personal-mcp`，并自动完成：
1. 检查并安装 `git` / `curl` / `openssl` 与 `uv`，执行 `uv sync`；
2. 首次部署时从 `.env.example` 生成 `.env`（`chmod 600`），自动生成 64 位随机 `ETALIB_NOTES_INTERNAL_TOKEN`、`SECRET_PATH_ETALIB`、`SECRET_PATH_GITHUB`，并引导填写 `FIRECRAWL_API_KEY` 与 `GITHUB_PAT`；
3. 安装并启动 `mcp-etalib-web.service`（`:8101`）与 `mcp-github.service`（`:8102`）；
4. 对本地 `127.0.0.1:8101/mcp` 与 `127.0.0.1:8102/mcp` 执行无状态 JSON-RPC `tools/list` 健康检查；
5. 自动输出 Nginx / Caddy 反代配置（含 `access_log off` / `log_skip` 与 `proxy_buffering off`）、Gemini Custom Apps URL 以及思源分享 `php-site/config.php` 配置片段。

后续拉取最新代码并重启服务，直接重跑同一条命令（或本机执行 `bash /opt/personal-mcp/deploy/deploy.sh`）即可，已有 `.env` 不会被覆盖。

---

## 架构概览

| 服务 | 端口 | `systemd` 单元 | 暴露工具数 | 后端总工具数 | 能力说明 |
| :--- | :---: | :--- | :---: | :---: | :--- |
| **Etalib Web** | `127.0.0.1:8101/mcp` | `mcp-etalib-web.service` | 7 | 16 | 思源分享笔记内网客户端（5 工具）+ Firecrawl v2 全功能封装（11 工具，禁用 `firecrawl_search`；支持本地内存缓存 `offset` 续读、`find_keyword` 跳转及 `confirm_large=True` 软上限） |
| **GitHub Personal** | `127.0.0.1:8102/mcp` | `mcp-github.service` | 12 | 23 | 4 置顶只读 + 6 置顶写工具（`destructiveHint=True`）+ 13 BM25 隐藏只读/下载工具 |

---

## 本地开发与测试

```bash
uv sync
uv run pytest -v
```

完整背景设计、Nginx/Caddy 配置要点以及 Gemini **4 张指令卡片**与端到端验收清单见 [plan.md](plan.md)。
