# Gemini Setup & Usage Guide

This guide walks through generating credentials, connecting both FastMCP servers to **Google Gemini (Web / iOS)**, and verifying end-to-end routing behavior.

---

## 1. Create a Fine-Grained GitHub PAT

Using a Fine-Grained Personal Access Token eliminates third-party OAuth session drops on mobile clients while scoping permissions strictly to the repositories you select.

1. Open [GitHub Fine-grained personal access tokens](https://github.com/settings/personal-access-tokens/new).
2. Give the token a name (for example `personal-mcp`) and choose an expiration period.
3. Under **Repository access**, select **All repositories** or **Only select repositories**.
4. Under **Repository permissions**, grant only:
   - **Contents**: `Read and write` (read code, edit files, create branches, fetch archives)
   - **Pull requests**: `Read and write`
   - **Issues**: `Read and write`
   - **Actions**: `Read-only` (check workflow run status)
   - **Metadata**: `Read-only` (selected automatically)
5. Click **Generate token**, copy the `github_pat_...` string, and place it into `GITHUB_PAT=` in `/opt/personal-mcp/.env` alongside `GITHUB_DEFAULT_OWNER` and `GITHUB_ALLOWED_WRITE_OWNERS`.

---

## 2. Optional: Connect SiYuan Share Notes via Internal Header

If you run a self-hosted SiYuan Share backend on the same host (default `127.0.0.1:8081`):

1. Add `'mcp_internal_token'` (matching `NOTES_INTERNAL_TOKEN` in `/opt/personal-mcp/.env`) and `'mcp_internal_user_id' => 1` to your SiYuan Share `php-site/config.php`, then restart its container.
2. In the public reverse-proxy server block for your notes domain (e.g. `notes.example.com`), block or strip external `X-MCP-Internal-Token` headers so external clients cannot spoof localhost authentication:
   ```nginx
   if ($http_x_mcp_internal_token != "") {
       return 403;
   }
   proxy_set_header X-MCP-Internal-Token "";
   ```

---

## 3. Smoke-Test the Endpoints Before Connecting Gemini

Run these checks on your server (`source /opt/personal-mcp/.env`):

```bash
MCP_DOMAIN="mcp.example.com"

# 1) Verify Personal Notes Web -> SiYuan Share (optional)
curl -s -X POST "https://${MCP_DOMAIN}/${SECRET_PATH_NOTES}/mcp" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"notes_list_shares","arguments":{}}}' | head -c 400; echo ""

# 2) Verify Firecrawl v2 web_scrape & web_search
curl -s -X POST "https://${MCP_DOMAIN}/${SECRET_PATH_NOTES}/mcp" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"web_scrape","arguments":{"url":"https://example.com","max_chars":500}}}' | head -c 400; echo ""

curl -s -X POST "https://${MCP_DOMAIN}/${SECRET_PATH_NOTES}/mcp" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"web_search","arguments":{"query":"example domain","limit":3}}}' | head -c 400; echo ""

# 3) Verify GitHub Personal
curl -s -X POST "https://${MCP_DOMAIN}/${SECRET_PATH_GITHUB}/mcp" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"gh_list_repos","arguments":{"limit":3}}}' | head -c 400; echo ""
```

---

## 4. Register Custom Apps in Gemini

1. Open `https://gemini.google.com` → **Settings** → **Connected Apps** → **Custom apps**.
2. Add **App 1**:
   - **Name**: `Personal Notes Web` (match `NOTES_MCP_NAME` in `.env`, or include both `Notes` and `Web` in the name)
   - **MCP Server URL**: `https://mcp.example.com/<SECRET_PATH_NOTES>/mcp`
   - **Authentication**: `No authentication`
3. Add **App 2**:
   - **Name**: `GitHub Personal`
   - **MCP Server URL**: `https://mcp.example.com/<SECRET_PATH_GITHUB>/mcp`
   - **Authentication**: `No authentication`
4. Open `https://gemini.google.com/devmode`, select each app, and click **Sync schema**:
   - `Personal Notes Web` should list **8** tools (including `web_search`).
   - `GitHub Personal` should list **12** tools (with `destructiveHint=True` on all 6 write tools).
5. **Prevent note-tool hijacking**: If you do not store study notes in Google Keep, consider disabling the built-in **Google Keep** extension under Connected Apps so prompts like `"Check my notes..."` are not diverted away from your MCP server. Toggle the custom apps off/on once and refresh the browser tab after syncing schemas.

---

## 5. Saved Info Routing Cards (Instructions for Gemini)

Paste these four cards into **Settings → Instructions for Gemini / Saved info** (replace `<notes-domain>`, `<blog-domain>`, and `<your-github-username>` with your values):

### Card 1: `@Personal Notes Web` (Explicit Notes Trigger)
```text
"My notes", "check my notes", and "course notes" refer to my SiYuan notes in @Personal Notes Web (<notes-domain>), NOT Google Keep or Google Drive. Only invoke the notes tools inside @Personal Notes Web (notes_search_notes, notes_read_document, notes_list_shares, or hidden tree/outline tools) when: (1) my message contains a <notes-domain> or <blog-domain> URL; OR (2) I explicitly ask you to check, look up, or inspect my personal notes (e.g., "check my notes", "search my notes", "去看看笔记", "我的笔记里有什么"). If I explicitly ask to check my notes and @Personal Notes Web is not mounted in the current session, do NOT fall back to Google Keep/Drive or fabricate note contents—simply ask me to reply with "@Personal Notes Web". For all general knowledge, math, physics, or CS questions where I do not explicitly refer to my notes or provide a note URL, answer directly using your general knowledge and reasoning without calling any notes tool. When a needed notes tool (such as folder tree or document outline) is hidden, query search_tools using ENGLISH keywords and invoke it via call_tool. Never use built-in web search for <notes-domain>. When relevant notes are found, cite the note title and URL and align with their notation, but verify accuracy critically: do not blindly copy errors—explicitly flag typos, missing hypotheses, or technical mistakes and provide the corrected, rigorous version.
```

### Card 2: `@GitHub Personal` (Read, Write, PR & Archive Download)
```text
Always proactively invoke the @GitHub Personal MCP tools—even if I do not explicitly type @GitHub Personal—for any request involving GitHub, including GitHub URLs, repositories, source code, directories, READMEs, commits, branches, issues, pull requests, releases, actions, code downloads, or my personal account <your-github-username>. Prefer @GitHub Personal over built-in web search for all GitHub lookups. If a needed GitHub capability (such as viewing commit diffs, issues/PR details, workflow runs, or downloading repo archives) is hidden, query search_tools using ENGLISH keywords and invoke via call_tool. If @GitHub Personal is not mounted in the current session, do not fabricate repo details—simply ask me to reply with "@GitHub Personal". Always ask for my explicit confirmation before performing any write or mutating operation (modifying files, creating branches, pushing commits, opening issues, or submitting PRs).
```

### Card 3: Two-Stage Web Search & Firecrawl Fallback
```text
For general web queries and public URLs outside <notes-domain> and GitHub, ALWAYS follow a two-stage search workflow:
Stage 1 (Default): Proactively use built-in Google web search and reading first (free) for all initial lookups.
Stage 2 (Firecrawl Escalation): Escalate to @Personal Notes Web ONLY when Stage 1 (a) returns no relevant results, (b) produces vague, conflicting, or secondary-only snippets that you cannot confidently verify (such as official application requirements, deadlines, policies, recent releases, or niche terms in any field), (c) is about to lead to a negative claim ("X does not exist / was never released / is not offered"), or (d) fails/gets blocked when reading a target URL (e.g., GOOGLE_EXTENDED_OPT_OUT, JS-rendered pages, incomplete text, or browser interaction).
When escalating: (1) if @Personal Notes Web is mounted, run web_search (2-credit snippet search, optionally with tbs recency filter) and then call web_scrape on ONLY the 1–2 most relevant primary-source URLs (never substitute web_search for routine Stage 1 queries); (2) if @Personal Notes Web is NOT mounted, do NOT assert that something does not exist or ask me to paste pages—simply tell me briefly to reply with "@Personal Notes Web" so it mounts and runs a second-opinion check.
When web_scrape or web_scrape_pdf returns truncated=True, continue reading via offset=next_offset or find_keyword (0 extra credits from local cache). Ask confirmation before passing confirm_large=True, and always call web_interact_stop immediately after any interactive browser session.
```

### Card 4: Factual Verification & Anti-False-Negative Discipline
```text
For questions involving public external facts, official regulations, application requirements, deadlines, statistics, product specs, time-sensitive events, or community opinions, do not rely solely on internal memory—verify them using actual tool outputs from the current session (built-in web search/reading first, escalating to @Personal Notes Web web_search/web_scrape when built-in search is inconclusive) and cite the real sources. Never fabricate facts, links, or tool outputs. Before concluding that something does not exist, was not released, or has no official record, perform a second-opinion verification via @Personal Notes Web (or briefly ask me to reply with "@Personal Notes Web" if not mounted); only state that no verified evidence was found after verification genuinely fails. This web-verification requirement does not apply when answering directly from @Personal Notes Web course notes (when explicitly requested), @GitHub Personal, or pure logic/math derivation.
```

---

## 6. End-to-End Verification Checklist

Test each prompt in a **brand-new conversation** to verify pre-routing and tool execution:

1. **Notes + BM25 hidden tools**: `"Check my notes and show the folder tree and outline of a course."` → invokes `notes_list_shares`, then `search_tools` + `call_tool` for `notes_get_share_tree` / `notes_get_document_outline`.
2. **No unsolicited notes lookup**: `"Prove that F[x] over a field F is a principal ideal domain."` → answers directly without touching `@Personal Notes Web`.
3. **Two-stage search & second-opinion fallback**: Ask a routine factual question (stays on built-in Google Search at 0 credits), then ask a niche/recent verification or official admissions/release check with `@Personal Notes Web` mounted → runs `web_search` (2 credits) and selectively `web_scrape` on 1–2 URLs.
4. **Web scrape + cache pagination**: Ask to scrape a long webpage or PDF, then ask to continue reading via `offset` or `find_keyword` → returns `cached: true` on follow-up slices.
5. **GitHub read & archive link (Web & iOS)**: `"List my latest repositories on GitHub and give me a zip download link for one."` → locates `gh_get_archive_download_url` via `search_tools`.
6. **Write confirmation & owner guard**: `"Create a branch and open a PR on one of my repos"` → asks for explicit confirmation before calling `gh_create_branch` / `gh_create_pull_request`; mutating an owner outside `GITHUB_ALLOWED_WRITE_OWNERS` is rejected by the server.
