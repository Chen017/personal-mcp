下面是一份**可直接复制保存为 `GEMINI_MCP_PLAN.md` 并交给其他 AI 编程助手（如 Claude Code / Cursor / Codex / Gemini CLI）直接落地执行**的完整方案、上下文说明、Debian 自建教程与使用验收指南。

---

# 个人自建双 MCP 架构执行计划与建造教程（Debian + FastMCP + Gemini Custom Apps）

> **致接手执行的 AI 助手（请先阅读）**：
> 1. 本文档面向**个人自建、单用户自用**场景（接入 Google Gemini Web / iOS 的 Custom Connected Apps）。
> 2. **明确不做（Non-Goals）**：不需要做 Google Connected Console 公开上架审核配置、隐私政策/服务条款页、企业级安全审计/监控告警系统或多租户复杂权限体系。核心目标是**快速、干净、稳定地把 2 个 FastMCP 服务在 Debian 服务器上跑通并接入 Gemini**。
> 3. **动手前必须先盘点现场**：请先检查当前 Debian 服务器上运行着的 `siyuan-share-web` Docker 容器（映射 `127.0.0.1:8081`）、已有的 Firecrawl 代码路径/环境变量、GitHub PAT 以及反向代理（Nginx/Caddy）配置，**不得破坏线上正在运行的思源分享站点及现有 OAuth 2.1 接口**。

---

## 1. 背景上下文：为什么要重建这套 MCP？（现状与真实痛点）

### 1.1 用户环境与核心需求
* **客户端**：Google Gemini Web 与 iOS App（通过 `Settings -> Connected Apps -> Custom apps` 接入自定义 MCP，并配合 `Instructions for Gemini / 已保存的信息` 控制路由规则）。
* **服务器**：个人 **Debian Linux** 服务器（自托管）。
* **三大数据源与期望行为**：
  1. **个人思源分享笔记（`note.etalib.space` & 主站博客 `www.etalib.space`）**：
     * **已存在后端**：思源笔记 MCP 已经在思源分享项目 `siyuan-plugin-share`（PHP 8.3，Docker 容器 `siyuan-share-web`，宿主机监听 `127.0.0.1:8081`）中完整实现。它是一个 **Stateless POST-only JSON-RPC 2.0 MCP**（`GET/DELETE /mcp` 返回 `405`），对外走 OAuth 2.1（`access_token` 1 小时过期，`refresh_token` 30 天单次轮换防重放），共提供 **5 个**高质量只读工具：`list_shares`、`get_share_tree`、`read_document`（内置 `offset`/`max_chars` 分块、LaTeX 保留与 IAL 清洗）、`get_document_outline`、`search_notes`。
     * **触发规则（严格显式触发）**：**仅当**用户给出了 `note.etalib.space` / `etalib.space` 链接，或明确发出查笔记指令/询问笔记内容（如“去看看笔记”、“查一下我的笔记”、“我的笔记里有什么”）时，才调用笔记工具；其余所有通用知识、概念定理、推导问答一律直接依靠模型通用知识与推理作答，**不主动调用笔记 MCP**。
     * 查到笔记后，需引用笔记标题与 URL，并对笔记中的笔误/遗漏前提做**智能批判性修正**。
  2. **网页抓取兜底（Firecrawl v2）**：
     * 平时优先用 Gemini 内置搜索/网页读取；仅当内置读取失败、被拦截（如遇到 `URL_FETCH_STATUS_GOOGLE_EXTENDED_OPT_OUT`）、内容不全、需 JS 渲染/点击交互/多页抓取/结构化提取/解析远程 PDF 时，调用 Firecrawl。
     * **工具策略**：保留完整精细粒度工具集（置顶 `web_scrape`、`web_map`，其余 `web_crawl`、`web_crawl_status`、`web_batch_scrape`、`web_extract_structured`、`web_scrape_pdf`、`web_interact_start`、`web_interact_action`、`web_interact_stop`、`web_check_credit_usage` 收纳进 BM25 按需检索）；**严禁暴露 `firecrawl_search`**（不替代 Gemini 内置搜索）；**不对 PDF 页数、多页 `crawl` 页数或信用点消耗设置硬上限**（仅提供合理默认返回字符截断以保护上下文窗口，且允许按需传参调整）。
  3. **GitHub 全功能读写（个人账号 `Chen017`）**：
     * 任何涉及 GitHub 仓库/链接/代码/PR 的需求直接走 GitHub MCP，保留精细粒度读写、完整只读信息查询（分支/Commit/Issue/PR/Release/Workflow 等）、代码下载、修改代码、提交 PR 等完整工具集（所有写操作必须置顶暴露，保留 `destructiveHint=True` 原生确认提示并经用户确认）。

### 1.2 为什么必须调整旧架构（真实问题根因）
1. **Gemini 宿主“按需路由挂载”限制（不打 `@` 不加载）**：
   * Gemini 不会在每轮会话中把所有已关联 MCP 全部注入上下文，而是由前置路由器根据提问关键词、`@<应用名>` 和 `server instructions` 动态决定本轮挂载哪些 MCP。
   * 实测现象：提问 `www.etalib.space 最新篇文章是什么` 时，宿主仅挂载了 `Etalib Notes`，未挂载 Firecrawl；内置浏览遭遇 `URL_FETCH_STATUS_GOOGLE_EXTENDED_OPT_OUT` 后，手里没有 Firecrawl 工具只能报错。
   * **解法**：将 `Etalib Notes` 与 `Firecrawl` 合并为统一入口 **`Etalib Web`**。由于思源分享网站采用 PHP 实现且与 Firecrawl 属于不同职责域，**不应把 Firecrawl 硬塞进 PHP 思源项目**，而是在外置 Python FastMCP 网关中将其与 Firecrawl 聚合并保留精细粒度工具。
2. **GitHub 官方远程 MCP 在 Gemini Web/iOS 上静默断连且工具极度臃肿**：
   * 官方端缺少 RFC 7591 动态注册且 OAuth App 缺少长期 Refresh Token，在移动端刷新失败后静默跳过挂载，且暴露 90+ 个带复杂 `$ref` Schema 的工具。
   * **解法**：自建基于 **Fine-Grained GitHub PAT** 的独立 FastMCP 服务。
3. **无状态 HTTP 与工具膨胀/上下文保护**：
   * FastMCP 默认 Streamable HTTP 为有状态会话模式（约 30 分钟空闲回收，且 `systemd` 重启会清空内存会话），容易在 Gemini Web/iOS 隔段时间续聊时报 `Session terminated / 404`。必须显式开启 **`stateless_http=True`**。
   * 后台保留 **16（Server 1）+ 23（Server 2）= 39 个**精细粒度工具，通过 **`BM25SearchTransform`** + **`serialize_tools_for_output_markdown`** 折叠低频只读/高阶工具，对外仅固定暴露高频置顶工具 + `search_tools` + `call_tool`。
4. **BM25 安全与中文检索陷阱（关键修正）**：
   * **陷阱 A（写操作确认失效）**：通过 BM25 合成的通用 `call_tool(name, arguments)` 间接调用隐藏工具时，Gemini 宿主只能看到 `call_tool` 的元信息，**无法继承底层工具的 `destructiveHint=True`**。因此**所有 GitHub 写操作工具必须全部放在 `always_visible` 中直出**，严禁藏进 BM25 索引。
   * **陷阱 B（无中文分词 + 代理穿透局限）**：`BM25SearchTransform` 默认按英文词元切分，没有中文分词，且 `FastMCP` 通用 HTTP 代理无法兼容 PHP 端仅 `POST /mcp` 的无状态端点（`GET` 405）及 1 小时过期 OAuth，也无法改写上游工具描述追加 `Keywords:`。因此不在 Python 端硬套通用代理，而是为思源 PHP 接口做极薄的本地静态内网头鉴权（仅约 20 行 PHP 改动），再在 Python 端用复用 `httpx.AsyncClient` 直发 JSON-RPC 包装为 5 个带英文 `Keywords:` 的原生 `@mcp.tool`。

---

## 2. 目标架构总览

```text
                   Google Gemini (Web / iOS App)
                                 │
                   HTTPS (Stateless Streamable HTTP / JSON-RPC)
                                 ▼
              Debian Reverse Proxy (Nginx / Caddy + TLS)
                 ├── /<SECRET_1>/mcp  ──► 127.0.0.1:8101/mcp  (access_log off)
                 └── /<SECRET_2>/mcp  ──► 127.0.0.1:8102/mcp  (access_log off)
                                 │
        ┌────────────────────────┴────────────────────────┐
        ▼                                                 ▼
┌─────────────────────────────────────────┐      ┌─────────────────────────────────────────┐
│ Server 1: Etalib Web (:8101)            │      │ Server 2: GitHub Personal (:8102)       │
│ systemd: mcp-etalib-web.service         │      │ systemd: mcp-github.service             │
│ FastMCP(..., stateless_http=True)       │      │ FastMCP(..., stateless_http=True)       │
│                                         │      │                                         │
│ • Backend (16 tools total):             │      │ • Backend (23 tools total):             │
│   - SiYuan Notes JSON-RPC Client (5)    │      │   - Shared httpx -> GitHub REST API     │
│     -> POST http://127.0.0.1:8081/mcp   │      │   - Fine-Grained PAT (Chen017)          │
│     -> Auth: X-MCP-Internal-Token       │      │   - 4 Pinned Read + 6 Pinned Write      │
│   - Firecrawl v2 Wrapper (11)           │      │   - 13 Hidden Read/Download tools       │
│     (no firecrawl_search; no hard caps) │      │ • Transform: BM25SearchTransform        │
│ • Transform: BM25SearchTransform        │      │ • Exposed via list_tools() (12 tools):  │
│ • Exposed via list_tools() (7 tools):   │      │   - 4 pinned Read tools                 │
│   - 3 pinned Notes tools                │      │   - 6 pinned Write tools (ALL write     │
│   - 2 pinned Firecrawl tools            │      │     tools pinned, destructiveHint=True)   │
│   - search_tools + call_tool            │      │   - search_tools + call_tool            │
└─────────────────────────────────────────┘      └─────────────────────────────────────────┘
                 │
                 ▼ (Localhost only, stripped on public Nginx/Caddy)
┌─────────────────────────────────────────┐
│ Existing Docker: siyuan-share-web       │
│ 127.0.0.1:8081 (PHP 8.3 + SQLite)       │
│ • Public OAuth 2.1 untouched            │
│ • Internal static header check (~20 LoC)│
└─────────────────────────────────────────┘
```

### 认证与访问控制策略（针对个人自建：零掉线、极简可靠）
* **外网接入 Gemini**：Gemini Custom Apps 原生支持 **No authentication** 或 OAuth 连接。为彻底规避 OAuth Refresh Token 在移动端静默掉线，对外端点采用 **HTTPS + 64 位高熵随机密钥路径（Capability URL）**，在 Gemini Custom Apps 中选 **No authentication** 即可永久稳定在线。
* **思源内网鉴权**：`siyuan-share-web` 原有对外 OAuth 2.1（`/oauth/*` 与 Bearer Token）**完全保留不动**；仅在 `php-site/mcp-oauth.php` 与 `php-site/config.php` 增加本机内网静态头 `X-MCP-Internal-Token` 白名单校验，供同机 `Etalib Web (:8101)` 免 OAuth 刷新直连 `http://127.0.0.1:8081/mcp`。
* **轻量安全底线（必做）**：
  1. **反代访问日志防泄漏**：Nginx/Caddy 中对应 `/<SECRET_1>/mcp` 与 `/<SECRET_2>/mcp` 的路由**必须关闭访问日志**（`access_log off;` / `log_skip`），防止 64 位路径密钥明文落盘。
  2. **公网剥离内网头**：`note.etalib.space` 的公网反代块中必须显式清除客户端传入的 `X-MCP-Internal-Token` 请求头（`proxy_set_header X-MCP-Internal-Token "";`），确保外部公网请求无法伪造内网认证。
  3. **GitHub PAT 最小权限与 owner 护栏**：使用 **Fine-Grained PAT**（仅勾选所需仓库的 `Contents`、`Pull requests`、`Issues`、`Actions(Read)`、`Metadata`）；Server 2 代码通过 `GITHUB_ALLOWED_WRITE_OWNERS=Chen017` 硬限制写操作归属账号。
  4. **密钥隔离**：所有密钥存于 `/opt/personal-mcp/.env`（`chmod 600`）与 `php-site/config.php`，严禁写入代码或提交至 Git。

---

## 3. 核心工程约束（接手 AI 必须严格遵守）

1. **FastMCP 版本、无状态模式与 API 核验**：
   * 使用 **`uv`** 管理 Python 3.12 环境并锁定 `fastmcp>=3.4`。
   * **必须开启无状态模式**：初始化 `FastMCP(..., stateless_http=True)`（或确认当前版本 `mcp.run(..., stateless_http=True)` / `mcp.http_app(path="/mcp", stateless_http=True)` 签名），写代码前执行一次：
     `uv run python -c "import fastmcp, inspect; print(fastmcp.__version__, inspect.signature(fastmcp.FastMCP))"`
     核实 `stateless_http`、`BM25SearchTransform` 与 `serialize_tools_for_output_markdown` 的实际位置与签名。
2. **必须使用 Streamable HTTP 传输协议**：
   * 运行于 `host="127.0.0.1"`，端口 `8101` / `8102`，`path="/mcp"`。
3. **严格符合 Gemini JSON Schema 习惯**：
   * 所有工具入参尽量使用扁平基础类型：`str`、`int`、`bool`、`list[str]`、`Literal[...]` + 合理默认值，避免深层嵌套 `$ref`/`$defs`。
4. **BM25 安全红线与中英文召回双保险**：
   * **安全红线**：**只有只读/非破坏性工具可以隐藏在 BM25 索引中**。所有写操作工具（Server 2 的全部 6 个 GitHub 写工具）必须加入 `always_visible` 直接暴露，保证 `destructiveHint=True` 完整透传给 Gemini。
   * **保险 A（能力目录 + 英文搜索引导）**：`BM25SearchTransform` 默认无中文分词，因此每个 Server 的 `FastMCP(instructions=...)` 必须明确指示模型：**调用 `search_tools` 时始终使用英文关键词**，并附带 40～80 个英文词的隐藏工具能力目录。
   * **保险 B（同义词锚点）**：每个隐藏工具的英文 docstring 末尾必须加一行 `Keywords: ...` 英文同义词（例如 `Keywords: github download code zipball tarball archive clone export`）。
   * **压缩输出**：`BM25SearchTransform` 必须传入 `search_result_serializer=serialize_tools_for_output_markdown`。
5. **共享连接池与上游降级容错**：
   * Server 1 与 Server 2 均维护模块级复用的 `httpx.AsyncClient`（不每次请求新建连接），并设置合理超时。
   * 当上游思源容器重启/超时、Firecrawl 返回错误、或 GitHub 返回 401/403（含 Rate Limit）/404/422 时，工具应捕获异常并返回清晰可读的错误信息，**不得拖垮当前 FastMCP 进程**。
6. **上下文默认保护与 Firecrawl 策略**：
   * **绝不暴露 `firecrawl_search`**。
   * **不对 PDF 页数、多页 `crawl` 页数或信用点消耗设置硬上限**：仅在代码层保留合理默认值（如 `crawl` 默认 `limit=5`、`max_depth=2`），以及**默认字符截断**（`DEFAULT_WEB_CHARS=16000`、`DEFAULT_NOTE_CHARS=16000`，均支持工具入参传大值覆盖，防止单次无意拉回几十万字撑爆上下文）。
   * GitHub `gh_get_file_contents` 支持 `start_line` / `end_line`（默认最多返回 `350` 行），并正确处理目录路径、>1MB 大文件与二进制文件；目录树默认过滤 `.git`、`node_modules`、`dist`、`__pycache__`、lockfile，并保留 GitHub API 的 `truncated` 警告标志；Diff/Log 默认截断至 `15000` 字符。

---

## 4. 分步建造执行计划（Debian 服务器落地）

### Step 0：盘点 Debian 服务器现状（执行 AI 第一步必做）
请先在服务器执行只读检查并记录结果：
```bash
# 1. 检查 Python / uv / Docker / 已有服务
python3 --version
which uv || echo "need install uv"
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
ps aux | grep -iE "mcp|etalib|firecrawl|uvicorn|fastmcp"
systemctl list-units --type=service | grep -iE "mcp|etalib|firecrawl"

# 2. 检查反向代理类型与配置
which nginx caddy
ls -la /etc/nginx/sites-enabled/ 2>/dev/null || ls -la /etc/caddy/ 2>/dev/null

# 3. 验证思源分享容器内网可达性及 site_base_url 设置
curl -i -s http://127.0.0.1:8081/mcp -X POST -H "Content-Type: application/json" -d '{}' | head -n 15
```
> **盘点确认项**：
> 1. 确认思源分享管理后台 `/admin` 中已开启 MCP（`mcp_enabled = 1`），且 `site_base_url` 已配置为 `https://note.etalib.space`（这样从 `http://127.0.0.1:8081/mcp` 调用时，返回的笔记链接与图片资源链接依然是标准的 `https://note.etalib.space/s/...`）。
> 2. 找出当前服务器上已部署的 `siyuan-plugin-share` 代码目录路径（含 `docker-compose.yml` 与 `php-site/config.php`）。

---

### Step 1：最小化增强思源分享项目的内网静态认证（向后兼容 ~20 行）

> **目的**：在不破坏现有 OAuth 2.1 客户端与 `tests/mcp.py` 全部测试的前提下，允许同机 `127.0.0.1:8101` 的 `Etalib Web` 通过私有请求头 `X-MCP-Internal-Token` 免 OAuth 刷新直连 `http://127.0.0.1:8081/mcp`。

1. 在思源分享服务器的 `php-site/config.php` 中新增两项配置（使用 `openssl rand -hex 32` 生成随机密钥）：
```php
    'mcp_internal_token' => 'replace_with_64_hex_chars_internal_only',
    'mcp_internal_user_id' => 1, // 默认管理员账号 ID
```

2. 修改 `php-site/mcp-oauth.php` 中的 `mcp_authenticate_bearer_request()`（在开头校验 `mcp_extract_bearer_token()` 之前插入内网头检查）：
```php
function mcp_authenticate_bearer_request(): array {
    global $config;
    $configuredInternal = trim((string)($config['mcp_internal_token'] ?? ''));
    $providedInternal = trim((string)($_SERVER['HTTP_X_MCP_INTERNAL_TOKEN'] ?? ''));
    if ($configuredInternal !== '' && $providedInternal !== '' && hash_equals($configuredInternal, $providedInternal)) {
        $internalUid = max(1, (int)($config['mcp_internal_user_id'] ?? 1));
        $pdo = db();
        $userStmt = $pdo->prepare('SELECT * FROM users WHERE id = :uid AND role = "admin" AND disabled = 0 LIMIT 1');
        $userStmt->execute([':uid' => $internalUid]);
        $user = $userStmt->fetch(PDO::FETCH_ASSOC);
        if ($user) {
            // 返回与 OAuth 路径完全一致的结构，确保 handle_mcp_request() 审计日志与限流无缝兼容
            return [
                'token' => ['token_hash' => mcp_hash_secret($configuredInternal), 'id' => 0],
                'client' => ['id' => 0, 'client_id' => 'internal_gateway'],
                'user' => $user,
            ];
        }
    }

    $rawToken = mcp_extract_bearer_token();
    // ... 原有 OAuth Bearer Token 校验逻辑保持不变 ...
```

3. **公网反代安全加固**：找到 `note.etalib.space` 现有的 Nginx / Caddy 站点配置，显式清空来自公网的该请求头：
   * Nginx：`proxy_set_header X-MCP-Internal-Token "";`
   * Caddy：`header_up -X-MCP-Internal-Token`
4. **回归验证**：运行思源项目原有测试（`python3 tests/mcp.py`），确保原有 OAuth 2.1 流程与全部 MCP 测试 100% 通过；并用 `curl` 带上 `X-MCP-Internal-Token` 本地测试 `http://127.0.0.1:8081/mcp` 的 `tools/list` 返回 `list_shares` 等 5 个工具。

---

### Step 2：初始化项目目录与依赖

```bash
# 如未安装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 创建统一工作目录
sudo mkdir -p /opt/personal-mcp
sudo chown -R $USER:$USER /opt/personal-mcp
cd /opt/personal-mcp

uv init --python 3.12
uv add "fastmcp>=3.4" httpx python-dotenv
```
*(注：Firecrawl v2 API 与 GitHub REST API 均直接通过复用的 `httpx.AsyncClient` 异步调用，零冗余 SDK 版本分裂、超时与连接池完全可控。)*

建议目录结构：
```text
/opt/personal-mcp/
├── .env                     # chmod 600，存放 API Key / PAT / 内网 Token / 密钥路径
├── .env.example
├── pyproject.toml
├── common/
│   └── utils.py             # 通用截断、行切片、过滤、HTTP 错误可读化工具函数
├── servers/
│   ├── etalib_web.py        # Server 1: SiYuan Notes JSON-RPC 包装(5) + Firecrawl v2(11) (:8101)
│   └── github_personal.py   # Server 2: GitHub 全功能读写/PR/下载(23) (:8102)
└── tests/
    └── test_bm25_recall.py  # 本地自动化验证（工具数、写保护、Schema、BM25 召回率、只读联调）
```

创建 `.env`（示例）：
```dotenv
# Server 1: Etalib Web (SiYuan Notes + Firecrawl v2)
ETALIB_NOTES_RPC_URL=http://127.0.0.1:8081/mcp
ETALIB_NOTES_INTERNAL_TOKEN=replace_with_same_64_hex_as_php_config
DEFAULT_NOTE_CHARS=16000
FIRECRAWL_API_KEY=fc-xxxxxxxxxxxxxxxx
DEFAULT_WEB_CHARS=16000

# Server 2: GitHub Personal
GITHUB_PAT=github_pat_xxxxxxxxxxxxxxxx
GITHUB_DEFAULT_OWNER=Chen017
GITHUB_ALLOWED_WRITE_OWNERS=Chen017
DEFAULT_FILE_LINES=350
DEFAULT_DIFF_CHARS=15000

# Reverse Proxy Random Capability Paths (用 openssl rand -hex 32 生成)
SECRET_PATH_ETALIB=replace_with_64_hex_chars_1
SECRET_PATH_GITHUB=replace_with_64_hex_chars_2
```

---

### Step 3：编写通用工具 `common/utils.py`

```python
from __future__ import annotations
import httpx

IGNORED_DIRS = {
    ".git", "node_modules", "dist", "build", ".next", ".venv",
    "venv", "__pycache__", ".pytest_cache", ".mypy_cache", "target",
}
IGNORED_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock", "Cargo.lock",
}


def truncate_text(text: str, max_chars: int = 16000, hint: str = "") -> str:
    if not text:
        return ""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    extra = f" {hint}" if hint else ""
    return (
        f"{text[:max_chars]}\n\n"
        f"[TRUNCATED: showing {max_chars:,} of {len(text):,} chars.{extra}]"
    )


def slice_lines(
    text: str,
    start_line: int = 1,
    end_line: int | None = None,
    default_max_lines: int = 350,
) -> dict:
    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        return {"total_lines": 0, "start_line": 1, "end_line": 0, "truncated": False, "content": ""}
    s = max(1, start_line)
    e = min(total, end_line) if end_line is not None else min(total, s + default_max_lines - 1)
    chunk = "\n".join(f"{idx}: {line}" for idx, line in enumerate(lines[s - 1 : e], start=s))
    return {
        "total_lines": total,
        "start_line": s,
        "end_line": e,
        "truncated": e < total,
        "content": chunk,
    }


def format_http_error(service: str, exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        resp = exc.response
        code = resp.status_code
        remaining = resp.headers.get("X-RateLimit-Remaining")
        rate_hint = f" (rate_limit_remaining={remaining})" if remaining is not None else ""
        detail = resp.text[:400].strip()
        return f"{service} HTTP {code}{rate_hint}: {detail}"
    if isinstance(exc, httpx.TimeoutException):
        return f"{service} request timed out: {exc}"
    if isinstance(exc, httpx.RequestError):
        return f"{service} connection error (upstream may be restarting or unreachable): {exc}"
    return f"{service} error: {exc}"
```

---

### Step 4：建造 Server 1 —— `servers/etalib_web.py`（SiYuan Notes 5 工具 + Firecrawl v2 11 工具）

> **实现要点**：
> * **不使用 FastMCP 通用 HTTP 代理透传**，直接通过共享 `httpx.AsyncClient` 向 `http://127.0.0.1:8081/mcp` 发送 JSON-RPC 2.0 `POST`，包装思源分享全部 **5 个只读工具**为原生 `@mcp.tool`：
>   * **置顶 3 个**：`notes_search_notes`、`notes_read_document`、`notes_list_shares`
>   * **隐藏进 BM25 2 个**：`notes_get_share_tree`、`notes_get_document_outline`（均带英文 `Keywords:`）
> * **Firecrawl v2 全功能 11 个工具**（直调 `https://api.firecrawl.dev/v2/...`）：
>   * **置顶 2 个**：`web_scrape`、`web_map`
>   * **隐藏进 BM25 9 个**：`web_crawl`、`web_crawl_status`、`web_batch_scrape`、`web_extract_structured`、`web_scrape_pdf`、`web_interact_start`、`web_interact_action`、`web_interact_stop`、`web_check_credit_usage`
>   * **绝不实现或暴露 `firecrawl_search`**；**不对 PDF 页数、多页 `crawl` 页数或信用点消耗设硬上限**，仅提供合理默认字符截断（可传参覆盖）。
> * **对外 `list_tools()` 共暴露 7 个工具**（5 置顶 + `search_tools` + `call_tool`）。

参考实现骨架（请接手 AI 补全全部 9 个 Firecrawl 隐藏工具并与 Firecrawl v2 实际响应字段对齐）：

```python
import os
import uuid
import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.transforms.search import BM25SearchTransform
from fastmcp.server.transforms.search.base import serialize_tools_for_output_markdown
from common.utils import format_http_error, truncate_text

load_dotenv()

ETALIB_NOTES_RPC_URL = os.getenv("ETALIB_NOTES_RPC_URL", "http://127.0.0.1:8081/mcp")
ETALIB_NOTES_INTERNAL_TOKEN = os.environ["ETALIB_NOTES_INTERNAL_TOKEN"]
FIRECRAWL_API_KEY = os.environ["FIRECRAWL_API_KEY"]
DEFAULT_NOTE_CHARS = int(os.getenv("DEFAULT_NOTE_CHARS", "16000"))
DEFAULT_WEB_CHARS = int(os.getenv("DEFAULT_WEB_CHARS", "16000"))

SERVER_INSTRUCTIONS = """
Personal notes and web-reading MCP hub combining:
1. Etalib Notes (note.etalib.space): read-only access to personal SiYuan shared notes. ONLY invoke notes_* tools when the user explicitly provides a note.etalib.space/etalib.space link OR explicitly asks to check/search their personal notes; for general knowledge or conceptual questions without explicit mention of notes, answer directly using general knowledge without calling notes_* tools.
2. Firecrawl Web Tools: fallback web scraping/crawling for inaccessible URLs (including www.etalib.space, JS-rendered pages, remote PDFs, and GOOGLE_EXTENDED_OPT_OUT sites).

IMPORTANT for hidden tools: always pass ENGLISH keywords to `search_tools`, then invoke with `call_tool`.
Hidden tools available via `search_tools` + `call_tool`:
- Notes: browse share folder hierarchy tree (`notes_get_share_tree`), inspect document headings/TOC and offsets (`notes_get_document_outline`).
- Web/Firecrawl: multi-page crawl (`web_crawl`, `web_crawl_status`), batch URL scraping (`web_batch_scrape`), structured JSON extraction (`web_extract_structured`), remote PDF parsing (`web_scrape_pdf`), browser interactive click/form sessions (`web_interact_start`, `web_interact_action`, `web_interact_stop` - always stop after use), and credit usage check (`web_check_credit_usage`).
Do NOT use web tools as a general search engine; use them only for explicit URLs or when built-in browsing fails.
""".strip()

PINNED_TOOLS = [
    "notes_search_notes",
    "notes_read_document",
    "notes_list_shares",
    "web_scrape",
    "web_map",
]

mcp = FastMCP(
    name="Etalib Web",
    instructions=SERVER_INSTRUCTIONS,
    stateless_http=True,
    transforms=[
        BM25SearchTransform(
            max_results=6,
            always_visible=PINNED_TOOLS,
            search_tool_name="search_tools",
            call_tool_name="call_tool",
            search_result_serializer=serialize_tools_for_output_markdown,
        )
    ],
)

_notes_client = httpx.AsyncClient(
    headers={"X-MCP-Internal-Token": ETALIB_NOTES_INTERNAL_TOKEN, "Content-Type": "application/json"},
    timeout=30.0,
)
_firecrawl_client = httpx.AsyncClient(
    base_url="https://api.firecrawl.dev/v2",
    headers={"Authorization": f"Bearer {FIRECRAWL_API_KEY}", "Content-Type": "application/json"},
    timeout=120.0,
)

READ_ONLY_ANNOTATIONS = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}


async def _call_siyuan_notes(tool_name: str, arguments: dict) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    try:
        resp = await _notes_client.post(ETALIB_NOTES_RPC_URL, json=payload)
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        return {"isError": True, "error": format_http_error("Etalib Notes upstream", exc)}

    if "error" in body:
        return {"isError": True, "error": f"RPC error: {body['error']}"}
    result = body.get("result") or {}
    if result.get("isError"):
        texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        return {"isError": True, "error": "\n".join(texts) or "Unknown notes tool error."}
    return result.get("structuredContent") or {"content": result.get("content", [])}


async def _fc_request(method: str, path: str, **kwargs) -> dict:
    try:
        resp = await _firecrawl_client.request(method, path, **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else {}
    except Exception as exc:
        return {"isError": True, "error": format_http_error("Firecrawl v2", exc)}


# ==================== 1. SiYuan Notes 工具（5 个：3 置顶 + 2 隐藏） ====================

@mcp.tool(annotations={"title": "List Shared Note Notebooks", **READ_ONLY_ANNOTATIONS})
async def notes_list_shares(cursor: str = "") -> dict:
    """List all public SiYuan shared notebooks and top-level course nodes.
    Keywords: siyuan notes list shares notebooks courses catalog index.
    """
    args = {"cursor": cursor} if cursor else {}
    return await _call_siyuan_notes("list_shares", args)


@mcp.tool(annotations={"title": "Search Personal Notes", **READ_ONLY_ANNOTATIONS})
async def notes_search_notes(
    query: str,
    share: str = "",
    parent_doc_id: str = "",
    limit: int = 20,
) -> dict:
    """Search public SiYuan notes by space-separated keywords across all notebooks or inside a specific share/subtree.
    Keywords: siyuan notes search find theorem definition lecture query.
    """
    args: dict = {"query": query, "limit": max(1, min(limit, 50))}
    if share:
        args["share"] = share
    if parent_doc_id:
        args["parent_doc_id"] = parent_doc_id
    return await _call_siyuan_notes("search_notes", args)


@mcp.tool(annotations={"title": "Read Note Document", **READ_ONLY_ANNOTATIONS})
async def notes_read_document(
    share: str,
    doc_id: str = "",
    offset: int = 0,
    max_chars: int = DEFAULT_NOTE_CHARS,
) -> dict:
    """Read clean Markdown of a SiYuan shared note (with LaTeX preserved). Supports chunked reading via offset and max_chars.
    Keywords: siyuan notes read document content markdown latex offset.
    """
    clamped = max(1000, min(max_chars, 50000))
    args: dict = {"share": share, "offset": max(0, offset), "max_chars": clamped}
    if doc_id:
        args["doc_id"] = doc_id
    return await _call_siyuan_notes("read_document", args)


@mcp.tool(annotations={"title": "Get Share Folder Tree", **READ_ONLY_ANNOTATIONS})
async def notes_get_share_tree(
    share: str,
    parent_doc_id: str = "",
    max_depth: int = 2,
) -> dict:
    """Browse the hierarchical folder and document tree of a SiYuan shared notebook or drill into a course subtree.
    Keywords: siyuan note folder tree directory hierarchy structure browse course.
    """
    args: dict = {"share": share, "max_depth": max(1, min(max_depth, 6))}
    if parent_doc_id:
        args["parent_doc_id"] = parent_doc_id
    return await _call_siyuan_notes("get_share_tree", args)


@mcp.tool(annotations={"title": "Get Note Document Outline", **READ_ONLY_ANNOTATIONS})
async def notes_get_document_outline(share: str, doc_id: str = "") -> dict:
    """Extract H1-H6 headings and exact character offsets of a SiYuan note to jump to sections via notes_read_document(offset=...).
    Keywords: siyuan note document outline headings toc table of contents sections offsets.
    """
    args: dict = {"share": share}
    if doc_id:
        args["doc_id"] = doc_id
    return await _call_siyuan_notes("get_document_outline", args)


# ==================== 2. Firecrawl v2 工具（11 个：2 置顶 + 9 隐藏） ====================

@mcp.tool(annotations={"title": "Scrape Webpage (Firecrawl v2)", **READ_ONLY_ANNOTATIONS})
async def web_scrape(
    url: str,
    only_main_content: bool = True,
    max_chars: int = DEFAULT_WEB_CHARS,
    wait_for_ms: int = 0,
) -> dict:
    """Scrape a single webpage or blog post (including www.etalib.space or JS-rendered pages) into clean Markdown.
    Use when built-in browsing fails or hits GOOGLE_EXTENDED_OPT_OUT.
    Keywords: firecrawl scrape webpage url read article markdown js render opt_out etalib blog.
    """
    payload: dict = {"url": url, "formats": ["markdown"], "onlyMainContent": only_main_content}
    if wait_for_ms > 0:
        payload["waitFor"] = wait_for_ms
    res = await _fc_request("POST", "/scrape", json=payload)
    if res.get("isError"):
        return res
    data = res.get("data") or {}
    md = truncate_text(data.get("markdown") or "", max_chars=max_chars, hint="Pass larger max_chars if full text is required.")
    return {"url": url, "metadata": data.get("metadata") or {}, "markdown": md}


@mcp.tool(annotations={"title": "Map Website Sub-links (Firecrawl v2)", **READ_ONLY_ANNOTATIONS})
async def web_map(url: str, search: str = "", limit: int = 50) -> dict:
    """Discover and enumerate sub-page URLs under a website before selectively scraping target pages.
    Keywords: firecrawl map sitemap links directory subpages discover urls.
    """
    payload: dict = {"url": url, "limit": max(1, limit)}
    if search:
        payload["search"] = search
    return await _fc_request("POST", "/map", json=payload)


# --- 其余 9 个 Firecrawl v2 隐藏工具（自动收录入 BM25 索引，不设页数/额度硬上限，仅保留默认字符截断） ---
# 请接手 AI 依同样模式补全以下 9 个 @mcp.tool（每个 docstring 末尾必须附英文 Keywords 行）：
# 1. web_crawl(url: str, limit: int = 10, max_depth: int = 2, include_paths: list[str] | None = None, exclude_paths: list[str] | None = None)
#    -> POST /crawl (不设硬上限，保留合理默认值；返回 crawl job id)
#    Keywords: firecrawl crawl multiple subpages spider site recursive
# 2. web_crawl_status(crawl_id: str, max_chars_per_page: int = 6000)
#    -> GET /crawl/{crawl_id} (对每页 markdown 做 truncate_text，允许传参覆盖)
#    Keywords: firecrawl crawl status job progress results pages
# 3. web_batch_scrape(urls: list[str], only_main_content: bool = True, max_chars_per_page: int = 6000)
#    -> POST /batch/scrape (或轮询结果，保留默认字符截断)
#    Keywords: firecrawl batch scrape multiple urls list parallel
# 4. web_extract_structured(urls: list[str], prompt: str, schema_json: str = "")
#    -> POST /extract (或 /scrape formats json，不设硬限制)
#    Keywords: firecrawl extract structured json schema data parse fields
# 5. web_scrape_pdf(url: str, mode: str = "auto", max_chars: int = DEFAULT_WEB_CHARS)
#    -> POST /scrape with parsers=[{"type": "pdf", "mode": mode}] (不对 PDF 页数设硬上限，返回正文用 truncate_text(..., max_chars) 并允许传参调整)
#    Keywords: firecrawl parse remote pdf document paper file extract text
# 6. web_interact_start(url: str)
#    -> 启动/绑定 Firecrawl v2 interact 浏览器沙箱并返回 scrape_id / session 标识
#    Keywords: firecrawl browser interact start session open click login dynamic
# 7. web_interact_action(scrape_id: str, prompt: str = "", code: str = "")
#    -> POST /scrape/{scrape_id}/interact 执行页面点击/填表/滚动
#    Keywords: firecrawl click button interact browser form scroll action
# 8. web_interact_stop(scrape_id: str)
#    -> DELETE /scrape/{scrape_id}/interact 立即关闭交互沙箱防止持续计费
#    Keywords: firecrawl interact stop close browser session end cleanup
# 9. web_check_credit_usage()
#    -> GET /team/credit-usage 查询 Firecrawl 剩余额度
#    Keywords: firecrawl credit usage quota balance remaining billing
#
# 注意：严禁实现或暴露 firecrawl_search！

if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8101, path="/mcp")
```

---

### Step 5：建造 Server 2 —— `servers/github_personal.py`（独立 GitHub 读写/PR/下载 FastMCP）

> **实现要点**：
> * 直接复用模块级 `httpx.AsyncClient` 调 GitHub REST API（`https://api.github.com`），彻底摆脱 GitHub 远程 OAuth 断连。
> * **安全红线**：**全部 6 个写操作工具必须放入 `PINNED_TOOLS`（`always_visible`）** 并标记 `destructiveHint=True`，绝不隐藏进 BM25，确保 Gemini 原生二次确认提示在每一次写操作时都能正常触发！
> * **工具统计**：4 置顶高频读 + **6 置顶写** + `search_tools` + `call_tool` = **对外暴露 12 个工具**；其余 **13 个低频只读/下载工具**放入 BM25 隐藏索引（总数 23 个）。
> * **已修复 5 个旧骨架 Bug**：
>   1. 共享复用 `httpx.AsyncClient` + `format_http_error`（优雅处理 401/403 rate-limit/404/422）；
>   2. `gh_list_repos` 对本人传 `affiliation="owner"`，避免混入外部协作仓；对非本人兼容 `/users/{owner}/repos` 与 `/orgs/{owner}/repos`；
>   3. `gh_get_file_contents` 正确处理目录路径（GitHub 返回 `list`）、`>1MB` 大文件（当 `encoding` 缺失/非 base64 或无 `content` 时走 `Accept: application/vnd.github.raw+json` 流式读取或返回友好提示）与二进制文件拦截；
>   4. `gh_get_repo_tree` 保留上游 GitHub API 的 `tree_truncated_by_github` 标记；
>   5. `gh_get_archive_download_url` 明确区分公开仓与私有仓（使用 `follow_redirects=False` 请求 `/zipball/{ref}` 捕获 302 短效签名 `Location`，同时返回 `git clone` / `curl` 命令作为备用）。

参考实现骨架（请接手 AI 补全全部 13 个 BM25 隐藏工具）：

```python
import base64
import os
from typing import Literal
import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.transforms.search import BM25SearchTransform
from fastmcp.server.transforms.search.base import serialize_tools_for_output_markdown
from common.utils import IGNORED_DIRS, IGNORED_FILES, format_http_error, slice_lines, truncate_text

load_dotenv()

GITHUB_PAT = os.environ["GITHUB_PAT"]
DEFAULT_OWNER = os.getenv("GITHUB_DEFAULT_OWNER", "Chen017")
ALLOWED_WRITE_OWNERS = {
    x.strip().lower()
    for x in os.getenv("GITHUB_ALLOWED_WRITE_OWNERS", DEFAULT_OWNER).split(",")
    if x.strip()
}
DEFAULT_FILE_LINES = int(os.getenv("DEFAULT_FILE_LINES", "350"))
DEFAULT_DIFF_CHARS = int(os.getenv("DEFAULT_DIFF_CHARS", "15000"))

SERVER_INSTRUCTIONS = f"""
Personal GitHub MCP server (default account: {DEFAULT_OWNER}) supporting full repository reading, code inspection, code downloading, editing files, branch management, and creating Pull Requests.

Always ask for explicit user confirmation before calling any write/mutating tool (all 6 write tools are directly exposed with destructiveHint=True).
IMPORTANT for hidden tools: always pass ENGLISH keywords to `search_tools`, then invoke with `call_tool`.

Hidden read-only & download tools available via `search_tools` + `call_tool`:
- Repository inspection: repo metadata/stars/permissions (`gh_get_repo_info`), branches (`gh_list_branches`), commit history (`gh_list_commits`), commit diff (`gh_get_commit_diff`), compare refs (`gh_compare_commits`), issues (`gh_list_issues`, `gh_get_issue`), pull requests (`gh_list_pull_requests`, `gh_get_pull_request_detail`), releases (`gh_list_releases`), GitHub Actions workflows & runs (`gh_list_workflow_runs`).
- Code download & batch reading: generate zip/tarball download URL (`gh_get_archive_download_url`), fetch multiple files in a directory bundle (`gh_fetch_directory_bundle`).
""".strip()

# 安全原则：4 个高频读 + 全部 6 个写工具置顶直出（保证 destructiveHint=True 生效）；13 个低频只读/下载工具由 BM25 检索
PINNED_TOOLS = [
    "gh_list_repos",
    "gh_get_repo_tree",
    "gh_get_file_contents",
    "gh_search_code",
    "gh_create_or_update_file",
    "gh_create_pull_request",
    "gh_create_branch",
    "gh_push_multiple_files",
    "gh_create_issue",
    "gh_comment_issue_or_pr",
]

mcp = FastMCP(
    name="GitHub Personal",
    instructions=SERVER_INSTRUCTIONS,
    stateless_http=True,
    transforms=[
        BM25SearchTransform(
            max_results=6,
            always_visible=PINNED_TOOLS,
            search_tool_name="search_tools",
            call_tool_name="call_tool",
            search_result_serializer=serialize_tools_for_output_markdown,
        )
    ],
)

_gh_client = httpx.AsyncClient(
    base_url="https://api.github.com",
    headers={
        "Authorization": f"Bearer {GITHUB_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "personal-fastmcp-github/1.0",
    },
    timeout=30.0,
)

READ_ANNOTATIONS = {"readOnlyHint": True, "destructiveHint": False}
WRITE_ANNOTATIONS = {"readOnlyHint": False, "destructiveHint": True}


def _check_write_allowed(owner: str) -> str | None:
    if ALLOWED_WRITE_OWNERS and owner.lower() not in ALLOWED_WRITE_OWNERS:
        return f"Write blocked: owner '{owner}' is not in GITHUB_ALLOWED_WRITE_OWNERS ({sorted(ALLOWED_WRITE_OWNERS)})."
    return None


async def _gh_request(method: str, path: str, **kwargs) -> dict | list:
    resp = await _gh_client.request(method, path, **kwargs)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


# ==================== 置顶读工具（4 个） ====================

@mcp.tool(annotations={"title": "List GitHub Repositories", **READ_ANNOTATIONS})
async def gh_list_repos(
    owner: str = DEFAULT_OWNER,
    visibility: Literal["all", "public", "private"] = "all",
    limit: int = 30,
) -> dict:
    """List repositories for the authenticated user (owned repos including private) or any public GitHub user/org.
    Keywords: github list repos repositories projects private public Chen017.
    """
    try:
        per_page = max(1, min(limit, 100))
        if owner.lower() == DEFAULT_OWNER.lower():
            data = await _gh_request(
                "GET",
                "/user/repos",
                params={"visibility": visibility, "affiliation": "owner", "sort": "updated", "per_page": per_page},
            )
        else:
            try:
                data = await _gh_request("GET", f"/users/{owner}/repos", params={"sort": "updated", "per_page": per_page})
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    data = await _gh_request("GET", f"/orgs/{owner}/repos", params={"sort": "updated", "per_page": per_page})
                else:
                    raise
        repos = [
            {
                "full_name": r["full_name"],
                "private": r["private"],
                "default_branch": r["default_branch"],
                "description": r.get("description") or "",
                "updated_at": r["updated_at"],
                "html_url": r["html_url"],
            }
            for r in data[:limit]
        ]
        return {"owner": owner, "count": len(repos), "repos": repos}
    except Exception as exc:
        return {"isError": True, "error": format_http_error("GitHub", exc)}


@mcp.tool(annotations={"title": "Get Repository Tree", **READ_ANNOTATIONS})
async def gh_get_repo_tree(
    owner: str,
    repo: str,
    ref: str = "",
    path_prefix: str = "",
    max_entries: int = 200,
) -> dict:
    """List files and directory structure of a GitHub repository, automatically ignoring .git, node_modules, dist, and lockfiles.
    Keywords: github directory tree files structure browse folder list.
    """
    try:
        if not ref:
            meta = await _gh_request("GET", f"/repos/{owner}/{repo}")
            ref = meta["default_branch"]
        data = await _gh_request("GET", f"/repos/{owner}/{repo}/git/trees/{ref}", params={"recursive": "1"})
        prefix = path_prefix.strip("/")
        items = []
        for entry in data.get("tree", []):
            p = entry["path"]
            parts = p.split("/")
            if set(parts) & IGNORED_DIRS or parts[-1] in IGNORED_FILES:
                continue
            if prefix and p != prefix and not p.startswith(prefix + "/"):
                continue
            items.append({"path": p, "type": entry["type"], "size": entry.get("size", 0)})
        return {
            "ref": ref,
            "tree_truncated_by_github": bool(data.get("truncated", False)),
            "total_matched": len(items),
            "truncated": len(items) > max_entries,
            "entries": items[:max_entries],
        }
    except Exception as exc:
        return {"isError": True, "error": format_http_error("GitHub", exc)}


@mcp.tool(annotations={"title": "Read File Contents", **READ_ANNOTATIONS})
async def gh_get_file_contents(
    owner: str,
    repo: str,
    path: str,
    ref: str = "",
    start_line: int = 1,
    end_line: int | None = None,
) -> dict:
    """Read a source code file or README (or list a directory if path points to a folder) with line-range slicing.
    Keywords: github read file code source readme view cat content.
    """
    try:
        clean_path = path.lstrip("/")
        params = {"ref": ref} if ref else {}
        data = await _gh_request("GET", f"/repos/{owner}/{repo}/contents/{clean_path}", params=params)
        if isinstance(data, list):
            return {
                "type": "directory",
                "path": clean_path,
                "entries": [{"name": e["name"], "path": e["path"], "type": e["type"], "size": e.get("size", 0)} for e in data],
            }
        if data.get("encoding") == "base64" and "content" in data:
            raw_bytes = base64.b64decode(data["content"])
        else:
            # 超过 1MB 的文件 /contents 不附带 base64 content，改走 raw media type
            raw_resp = await _gh_client.get(
                f"/repos/{owner}/{repo}/contents/{clean_path}",
                params=params,
                headers={"Accept": "application/vnd.github.raw+json"},
            )
            raw_resp.raise_for_status()
            raw_bytes = raw_resp.content

        if b"\x00" in raw_bytes[:4096]:
            return {
                "type": "binary",
                "path": data.get("path", clean_path),
                "size": len(raw_bytes),
                "html_url": data.get("html_url", ""),
                "note": "Binary file cannot be displayed as text.",
            }
        raw_text = raw_bytes.decode("utf-8", errors="replace")
        sliced = slice_lines(raw_text, start_line=start_line, end_line=end_line, default_max_lines=DEFAULT_FILE_LINES)
        return {
            "type": "file",
            "path": data.get("path", clean_path),
            "sha": data.get("sha", ""),
            "html_url": data.get("html_url", ""),
            **sliced,
        }
    except Exception as exc:
        return {"isError": True, "error": format_http_error("GitHub", exc)}


@mcp.tool(annotations={"title": "Search GitHub Code", **READ_ANNOTATIONS})
async def gh_search_code(query: str, repo: str = "", limit: int = 15) -> dict:
    """Search code across GitHub or within a specific repository (pass repo as 'owner/name').
    Keywords: github search code grep symbol function find.
    """
    try:
        q = f"{query} repo:{repo}" if repo else query
        data = await _gh_request("GET", "/search/code", params={"q": q, "per_page": max(1, min(limit, 30))})
        items = [
            {"repo": i["repository"]["full_name"], "path": i["path"], "html_url": i["html_url"]}
            for i in data.get("items", [])[:limit]
        ]
        return {"query": q, "total_count": data.get("total_count", len(items)), "items": items}
    except Exception as exc:
        return {"isError": True, "error": format_http_error("GitHub", exc)}


# ==================== 置顶写工具（全部 6 个写工具均直出，确保 destructiveHint=True 生效） ====================

@mcp.tool(annotations={"title": "Create or Update File (Write)", **WRITE_ANNOTATIONS})
async def gh_create_or_update_file(
    owner: str,
    repo: str,
    path: str,
    content: str,
    message: str,
    branch: str,
    sha: str = "",
) -> dict:
    """Create a new file or update an existing file in a GitHub repository branch.
    Always ask for explicit user confirmation first. Auto-fetches current sha if omitted.
    """
    if err := _check_write_allowed(owner):
        return {"isError": True, "error": err}
    clean_path = path.lstrip("/")
    try:
        if not sha:
            try:
                existing = await _gh_request("GET", f"/repos/{owner}/{repo}/contents/{clean_path}", params={"ref": branch})
                if isinstance(existing, dict):
                    sha = existing.get("sha", "")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
        payload = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        res = await _gh_request("PUT", f"/repos/{owner}/{repo}/contents/{clean_path}", json=payload)
        return {
            "path": res["content"]["path"],
            "commit_sha": res["commit"]["sha"],
            "html_url": res["content"]["html_url"],
        }
    except Exception as exc:
        return {"isError": True, "error": format_http_error("GitHub", exc)}


@mcp.tool(annotations={"title": "Create Pull Request (Write)", **WRITE_ANNOTATIONS})
async def gh_create_pull_request(
    owner: str,
    repo: str,
    title: str,
    head: str,
    base: str,
    body: str = "",
    draft: bool = False,
) -> dict:
    """Open a Pull Request on GitHub. Always ask for explicit user confirmation before calling."""
    if err := _check_write_allowed(owner):
        return {"isError": True, "error": err}
    try:
        res = await _gh_request(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            json={"title": title, "head": head, "base": base, "body": body, "draft": draft},
        )
        return {"number": res["number"], "state": res["state"], "html_url": res["html_url"]}
    except Exception as exc:
        return {"isError": True, "error": format_http_error("GitHub", exc)}


@mcp.tool(annotations={"title": "Create Branch (Write)", **WRITE_ANNOTATIONS})
async def gh_create_branch(owner: str, repo: str, new_branch: str, from_branch: str = "") -> dict:
    """Create a new Git branch from default branch or specified base branch. Always confirm with user first."""
    if err := _check_write_allowed(owner):
        return {"isError": True, "error": err}
    try:
        if not from_branch:
            meta = await _gh_request("GET", f"/repos/{owner}/{repo}")
            from_branch = meta["default_branch"]
        ref_data = await _gh_request("GET", f"/repos/{owner}/{repo}/git/ref/heads/{from_branch}")
        sha = ref_data["object"]["sha"]
        res = await _gh_request(
            "POST",
            f"/repos/{owner}/{repo}/git/refs",
            json={"ref": f"refs/heads/{new_branch}", "sha": sha},
        )
        return {"branch": new_branch, "from_branch": from_branch, "sha": res["object"]["sha"]}
    except Exception as exc:
        return {"isError": True, "error": format_http_error("GitHub", exc)}


@mcp.tool(annotations={"title": "Push Multiple Files in One Commit (Write)", **WRITE_ANNOTATIONS})
async def gh_push_multiple_files(
    owner: str,
    repo: str,
    branch: str,
    file_paths: list[str],
    file_contents: list[str],
    message: str,
) -> dict:
    """Create or update multiple files in a single atomic commit using Git Data API.
    Note: uses parallel flat lists file_paths & file_contents for strict JSON Schema compatibility.
    Always ask for explicit user confirmation first.
    """
    if err := _check_write_allowed(owner):
        return {"isError": True, "error": err}
    if len(file_paths) != len(file_contents) or not file_paths:
        return {"isError": True, "error": "file_paths and file_contents must be non-empty lists of equal length."}
    # TODO: 接手 AI 补全 Git Data API (GET ref -> GET commit -> POST blobs -> POST tree -> POST commit -> PATCH ref)
    raise NotImplementedError


@mcp.tool(annotations={"title": "Create Issue (Write)", **WRITE_ANNOTATIONS})
async def gh_create_issue(owner: str, repo: str, title: str, body: str = "", labels: list[str] | None = None) -> dict:
    """Open a new GitHub Issue. Always ask for explicit user confirmation first."""
    if err := _check_write_allowed(owner):
        return {"isError": True, "error": err}
    try:
        payload: dict = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        res = await _gh_request("POST", f"/repos/{owner}/{repo}/issues", json=payload)
        return {"number": res["number"], "state": res["state"], "html_url": res["html_url"]}
    except Exception as exc:
        return {"isError": True, "error": format_http_error("GitHub", exc)}


@mcp.tool(annotations={"title": "Comment on Issue or PR (Write)", **WRITE_ANNOTATIONS})
async def gh_comment_issue_or_pr(owner: str, repo: str, issue_number: int, body: str) -> dict:
    """Post a comment on an existing Issue or Pull Request. Always ask for explicit user confirmation first."""
    if err := _check_write_allowed(owner):
        return {"isError": True, "error": err}
    try:
        res = await _gh_request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            json={"body": body},
        )
        return {"id": res["id"], "html_url": res["html_url"]}
    except Exception as exc:
        return {"isError": True, "error": format_http_error("GitHub", exc)}


# ==================== BM25 隐藏只读/下载工具（13 个，均须标 READ_ANNOTATIONS 并附英文 Keywords 行） ====================
# 请接手 AI 补全以下 13 个只读工具：
#  1. gh_get_repo_info(owner, repo) -> 仓库元信息、star、语言、权限、默认分支
#     Keywords: github repo info metadata stars license default branch visibility
#  2. gh_list_branches(owner, repo, limit=30)
#     Keywords: github list branches refs heads
#  3. gh_list_commits(owner, repo, ref="", path="", limit=20)
#     Keywords: github list commits history log changes author
#  4. gh_get_commit_diff(owner, repo, sha, max_chars=DEFAULT_DIFF_CHARS)
#     Keywords: github commit diff patch changes inspect
#  5. gh_compare_commits(owner, repo, base, head, max_chars=DEFAULT_DIFF_CHARS)
#     Keywords: github compare branches commits diff ahead behind
#  6. gh_list_issues(owner, repo, state="open", limit=20)
#     Keywords: github list issues bug tickets open closed
#  7. gh_get_issue(owner, repo, issue_number) -> 含评论列表
#     Keywords: github issue detail comments thread body
#  8. gh_list_pull_requests(owner, repo, state="open", limit=20)
#     Keywords: github list pull requests pr open merged
#  9. gh_get_pull_request_detail(owner, repo, pr_number, include_files=True, max_patch_chars=DEFAULT_DIFF_CHARS)
#     Keywords: github pull request pr detail files diff patch review
# 10. gh_list_releases(owner, repo, limit=10)
#     Keywords: github releases tags versions changelog assets
# 11. gh_list_workflow_runs(owner, repo, limit=10)
#     Keywords: github actions workflow runs ci cd build status logs
# 12. gh_get_archive_download_url(owner, repo, ref="", archive_format: Literal["zipball", "tarball"] = "zipball")
#     -> 请求 GET /repos/{owner}/{repo}/{archive_format}/{ref} (follow_redirects=False) 捕获 302 短效签名 Location（私有仓也能用短效签名直链下载），同时附上 git clone / curl 命令
#     Keywords: github download code zip zipball tarball archive clone export repository
# 13. gh_fetch_directory_bundle(owner, repo, path="", ref="", max_files=10, max_total_chars=18000)
#     -> 批量拉取目标目录下文本源码合并返回（自动跳过二进制与 IGNORED_DIRS/IGNORED_FILES）
#     Keywords: github download folder batch read multiple files bundle directory

if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8102, path="/mcp")
```

---

### Step 6：编写本地自动化测试 `tests/test_bm25_recall.py`（上线前必跑！）

要求接手 AI 写一个用 `fastmcp.Client` 直连本地两个服务的测试脚本，自动验证：
1. `list_tools()` 暴露数量与安全注解：
   * Server 1（`Etalib Web`）暴露 **7 个**工具；
   * Server 2（`GitHub Personal`）暴露 **12 个**工具，且 **全部 6 个写工具都在 `list_tools()` 中直接可见并携带 `destructiveHint=True`**。
2. **BM25 英文自然语言召回测试**（至少测以下 10 条英文查询，确保目标工具全部进入 Top 3）：
   * Server 1：
     * `search_tools("note folder tree directory")` → `notes_get_share_tree`
     * `search_tools("document outline headings sections")` → `notes_get_document_outline`
     * `search_tools("crawl multiple subpages")` → `web_crawl`
     * `search_tools("parse remote pdf file")` → `web_scrape_pdf`
     * `search_tools("extract structured json schema")` → `web_extract_structured`
     * `search_tools("click button interact browser")` → `web_interact_action` / `web_interact_start`
   * Server 2：
     * `search_tools("download repository zip archive")` → `gh_get_archive_download_url`
     * `search_tools("list pull requests and diff")` → `gh_list_pull_requests` / `gh_get_pull_request_detail`
     * `search_tools("github actions workflow runs")` → `gh_list_workflow_runs`
     * `search_tools("commit history and changes")` → `gh_list_commits`
3. **只读联调与回归**：
   * 实测调用 `notes_list_shares()`（确认通过 `127.0.0.1:8081` + `X-MCP-Internal-Token` 成功返回且链接仍为 `https://note.etalib.space/s/...`）、`web_scrape("https://www.etalib.space")`、`gh_list_repos(owner="Chen017")`。
   * 在思源分享项目目录执行原有测试 `python3 tests/mcp.py`，确认原有 OAuth 2.1 与 5 个工具回归测试 100% 通过。

---

### Step 7：Debian 部署（`systemd` 常驻 + Nginx/Caddy HTTPS 反代）

#### 7.1 创建两个 `systemd` 服务
> 注意：`ExecStart` 直接调用 `.venv/bin/python` 避免常驻服务里 `uv` 环境变量或联网检查干扰；`mcp-etalib-web.service` 声明 `After=network.target docker.service` 确保 Docker 中的 `siyuan-share-web` 先起。

`/etc/systemd/system/mcp-etalib-web.service`：
```ini
[Unit]
Description=Personal FastMCP Server - Etalib Web (SiYuan Notes + Firecrawl v2)
After=network.target docker.service

[Service]
Type=simple
User=YOUR_DEBIAN_USER
WorkingDirectory=/opt/personal-mcp
EnvironmentFile=/opt/personal-mcp/.env
ExecStart=/opt/personal-mcp/.venv/bin/python servers/etalib_web.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/mcp-github.service`：
```ini
[Unit]
Description=Personal FastMCP Server - GitHub Personal
After=network.target

[Service]
Type=simple
User=YOUR_DEBIAN_USER
WorkingDirectory=/opt/personal-mcp
EnvironmentFile=/opt/personal-mcp/.env
ExecStart=/opt/personal-mcp/.venv/bin/python servers/github_personal.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

启动并设为开机自启：
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mcp-etalib-web mcp-github
sudo systemctl status mcp-etalib-web mcp-github
```

#### 7.2 反向代理配置（二选一，**必关缓冲 + 必关密钥路径日志**！）
> **关键红线**：
> 1. Streamable HTTP 响应在 Nginx 下必须配置 `proxy_buffering off;`；
> 2. 必须对 Capability URL 路径关闭访问日志（`access_log off;` / `log_skip`），防止 64 位路径密钥明文落盘；
> 3. 确认对 `/<SECRET>/mcp` 发 `POST` 不会返回 `307` 尾斜杠重定向（否则会被 `location / { return 404; }` 拦截）；
> 4. **务必在 `note.etalib.space` 已有站点配置中剥离 `X-MCP-Internal-Token` 请求头**。

**选项 A：Nginx（配合现有 Certbot / SSL 证书）**
```nginx
server {
    listen 443 ssl http2;
    server_name mcp.yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/mcp.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/mcp.yourdomain.com/privkey.pem;

    # Server 1: Etalib Web
    location = /<SECRET_PATH_ETALIB>/mcp {
        access_log off;
        proxy_pass http://127.0.0.1:8101/mcp;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
    }

    # Server 2: GitHub Personal
    location = /<SECRET_PATH_GITHUB>/mcp {
        access_log off;
        proxy_pass http://127.0.0.1:8102/mcp;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
    }

    # 其余所有路径一律 404，防止探测
    location / {
        return 404;
    }
}
```
同时在 `note.etalib.space` 的 Nginx `location /` 中补一行防伪造内网头：
```nginx
proxy_set_header X-MCP-Internal-Token "";
```

**选项 B：Caddy（自动申请 HTTPS 证书，原生支持流式传输）**
```caddyfile
mcp.yourdomain.com {
    log_skip /<SECRET_PATH_ETALIB>/mcp
    log_skip /<SECRET_PATH_GITHUB>/mcp

    handle /<SECRET_PATH_ETALIB>/mcp {
        rewrite * /mcp
        reverse_proxy 127.0.0.1:8101 {
            flush_interval -1
        }
    }
    handle /<SECRET_PATH_GITHUB>/mcp {
        rewrite * /mcp
        reverse_proxy 127.0.0.1:8102 {
            flush_interval -1
        }
    }
    handle {
        respond 404
    }
}
```
同时在 `note.etalib.space` 的 Caddy `reverse_proxy` 中补一行防伪造内网头：
```caddyfile
header_up -X-MCP-Internal-Token
```

---

## 5. Gemini 接入、调试与最终 4 张指令卡片（上线使用教程）

### 5.1 在 Gemini 注册 2 个 Custom Apps
1. 电脑浏览器访问 `https://gemini.google.com` → `Settings` → `Connected Apps` → `Custom apps`：
   * **删除旧的 3 个自定义连接器**（旧 `@Etalib Notes`、旧 `@Firecrawl Fastmcp`、旧 `@GitHub MCP Server`）。
   * **新增 App 1**：
     * Name: **`Etalib Notes Web`**
     * MCP Server URL: `https://mcp.yourdomain.com/<SECRET_PATH_ETALIB>/mcp`
     * Auth: **No authentication**
   * **新增 App 2**：
     * Name: **`GitHub Personal`**
     * MCP Server URL: `https://mcp.yourdomain.com/<SECRET_PATH_GITHUB>/mcp`
     * Auth: **No authentication**
   * **防劫持建议**：若不用 Google Keep 记学习笔记，建议在 `Connected Apps` 关闭 Google Keep，避免 `"check my notes"` 被 Google Keep/Drive 抢走。
2. **用官方开发者模式核验**：打开 **`https://gemini.google.com/devmode`**，分别选中 `Etalib Notes Web` 与 `GitHub Personal`，点击 `Sync schema`，确认 `Tool Schemas` 正常显示（Server 1 为 7 个工具，Server 2 为 12 个工具且写工具标注完整）且 `Event Log` 无报错。

### 5.2 更新「Instructions for Gemini / 已保存的信息」（删除旧卡片，粘贴以下 4 张英文卡片）

> 已同步适配新命名（`@Etalib Notes Web` / `@GitHub Personal`）及最新需求：**笔记严格仅在用户显式要求或给出链接时查阅、明确区分于 Google Keep/Drive、普通问答不查笔记、查到笔记做批判性纠错、引导用英文关键词调 `search_tools`、移除 Firecrawl 页数硬上限限制并支持缓存续读**。

#### Card 1: `@Etalib Notes Web` (Personal Notes — Explicit Trigger Only)
```text
"My notes", "check my notes", and "course notes" refer to my SiYuan notes in @Etalib Notes Web (note.etalib.space), NOT Google Keep or Google Drive. Only invoke the notes tools inside @Etalib Notes Web (notes_search_notes, notes_read_document, notes_list_shares, or hidden tree/outline tools) when: (1) my message contains a note.etalib.space or etalib.space URL; OR (2) I explicitly ask you to check, look up, or inspect my personal notes (e.g., "check my notes", "search my notes", "去看看笔记", "我的笔记里有什么"). If I explicitly ask to check my notes and @Etalib Notes Web is not mounted in the current session, do NOT fall back to Google Keep/Drive or fabricate note contents—simply ask me to reply with "@Etalib Notes Web". For all general knowledge, math, physics, or CS questions where I do not explicitly refer to my notes or provide a note URL, answer directly using your general knowledge and reasoning without calling any notes tool. When a needed notes tool (such as folder tree or document outline) is hidden, query search_tools using ENGLISH keywords and invoke it via call_tool. Never use built-in web search for note.etalib.space. When relevant notes are found, cite the note title and URL and align with their notation, but verify accuracy critically: do not blindly copy errors—explicitly flag typos, missing hypotheses, or technical mistakes and provide the corrected, rigorous version.
```

#### Card 2: `@GitHub Personal` (GitHub Read, Write, PR & Download)
```text
Always proactively invoke the @GitHub Personal MCP tools—even if I do not explicitly type @GitHub Personal—for any request involving GitHub, including GitHub URLs, repositories, source code, directories, READMEs, commits, branches, issues, pull requests, releases, actions, code downloads, or my personal account Chen017. Prefer @GitHub Personal over built-in web search for all GitHub lookups. If a needed GitHub capability (such as viewing commit diffs, issues/PR details, workflow runs, or downloading repo archives) is hidden, query search_tools using ENGLISH keywords and invoke it via call_tool. If @GitHub Personal is not mounted in the current session, do not fabricate repo details—simply ask me to reply with "@GitHub Personal". Always ask for my explicit confirmation before performing any write or mutating operation (modifying files, creating branches, pushing commits, opening issues, or submitting PRs).
```

#### Card 3: General Web Access & `@Etalib Notes Web` (Firecrawl) Fallback
```text
For general web queries and public URLs outside etalib.space and GitHub, start with built-in web search and web reading. If built-in search/reading fails, hits an access restriction (such as GOOGLE_EXTENDED_OPT_OUT), returns incomplete content, or requires crawling sub-links, JS rendering, structured extraction, browser interaction, or remote PDF parsing: (1) if @Etalib Notes Web is available in the current session, immediately use its web/Firecrawl tools (query search_tools using ENGLISH keywords and invoke via call_tool for crawl/batch/extract/PDF/interact tools) instead of saying you cannot access the site; (2) if @Etalib Notes Web is not mounted in the current session, do not ask me to paste the webpage—simply tell me briefly to reply with "@Etalib Notes Web" so it mounts and fetches the page. When web_scrape or web_scrape_pdf returns truncated=True, continue reading via offset=next_offset or find_keyword (which reads from local cache at 0 extra Firecrawl credits). Never use web tools as a general search engine; prefer web_map followed by web_scrape for targeted sub-pages; ask confirmation before passing confirm_large=True; and always call web_interact_stop immediately after finishing any interactive browser session.
```

#### Card 4: Factual Verification & Citation Discipline
```text
For questions involving public external facts, official regulations, statistics, product specs, time-sensitive events, or community opinions, do not rely solely on internal memory—verify them using actual tool outputs from the current session (built-in web search/reading or @Etalib Notes Web) and cite the real sources. Never fabricate facts, links, or tool outputs. If verification fails or returns no supporting evidence, explicitly state that no verified evidence was found. This web-verification requirement does not apply when answering directly from @Etalib Notes Web course notes (when explicitly requested), @GitHub Personal, or pure logic/math derivation.
```

---

### 5.3 上线端到端验收清单（在 Gemini Web 与 iOS 实测）

1. **显式查笔记 + 隐藏工具**：新对话提问「Check my notes and give me the definition of the group.」→ 应挂载 `@Etalib Notes Web`（不再被 Google Keep/Drive 劫持），搜到对应代数笔记。
2. **未提及笔记不触发**：新对话提问「请详细推导并解释光滑流形上切空间的定义和基本性质」→ **不应**调用 `@Etalib Notes Web` 查笔记，直接用通用知识回答。
3. **主站防 `OPT_OUT`**：新对话提问「`www.etalib.space` 最新一篇文章是什么」→ 应自动挂载 `@Etalib Notes Web` 并用 `web_scrape` 读出正文（不再因 `OPT_OUT` 报错）。
4. **陌生被拦截网页兜底**：新对话丢一个搜不到的动态网站或远程 PDF 让它解析总结 → 若首轮未挂载 `@Etalib Notes Web`，它应简短提示你回复 `@Etalib Notes Web`；你回复后立刻用 `web_scrape` / `web_scrape_pdf`（经 `search_tools`）完成读取。
5. **GitHub 跨端稳定性 & 隐藏工具**：在 **iOS App** 新对话提问「查看 `Chen017` 最近更新的仓库，并给我下载某个仓库 zip 的链接」→ 因 `stateless_http=True` + PAT 直连，即使隔天仍稳定挂载不掉线，且能通过英文 `search_tools` 找到 `gh_get_archive_download_url`。
6. **GitHub 写操作原生确认保护**：测试「帮我在 `Chen017/<测试仓库>` 新建一个分支修改 README 并准备提 PR」→ 所有写工具（包括 `gh_create_branch`、`gh_create_or_update_file`、`gh_create_pull_request`）均直接可见并带 `destructiveHint=True`，必须在执行前向你二次确认。
