from __future__ import annotations

import asyncio
import base64
import os
from typing import Literal
from weakref import WeakKeyDictionary

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.transforms.search import BM25SearchTransform
from fastmcp.server.transforms.search.base import serialize_tools_for_output_markdown

from common.utils import IGNORED_DIRS, IGNORED_FILES, format_http_error, is_binary_bytes, slice_lines, truncate_text

load_dotenv()

READ_ANNOTATIONS = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
WRITE_ANNOTATIONS = {"readOnlyHint": False, "destructiveHint": True}

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


def _default_owner() -> str:
    val = os.getenv("GITHUB_DEFAULT_OWNER", "").strip()
    if val in {"your-github-username", "your_username"}:
        return ""
    return val


def _allowed_write_owners() -> set[str]:
    raw = os.getenv("GITHUB_ALLOWED_WRITE_OWNERS", "").strip()
    if raw:
        return {
            x.strip().lower()
            for x in raw.split(",")
            if x.strip() and x.strip() not in {"your-github-username", "your_username"}
        }
    default_owner = _default_owner()
    return {default_owner.lower()} if default_owner else set()


def _default_file_lines() -> int:
    return int(os.getenv("DEFAULT_FILE_LINES", "350"))


def _default_diff_chars() -> int:
    return int(os.getenv("DEFAULT_DIFF_CHARS", "15000"))


def _build_server_instructions() -> str:
    owner_label = _default_owner() or "the configured account"
    return f"""
Personal GitHub & code repository MCP server for {owner_label} (个人代码仓库 / 我的仓库 / GitHub 仓库 / 项目与主题源码管理工具):
Directly list personal repositories (public & private), inspect repository directory trees and source code files, search code, view commits/diffs/issues/PRs/releases/Actions, download repository archives, edit files, create branches, and submit Pull Requests (支持查看我的仓库、项目文件目录、主题源码、README、分支、提交记录、Issue、PR、Actions、下载代码、修改文件与提 PR).

Ask for explicit user confirmation before calling write/mutating tools (all 6 write tools are directly exposed with destructiveHint=True).
For hidden read-only & download tools, always pass ENGLISH keywords to `search_tools`, then invoke via `call_tool`:
- Repository inspection: repo metadata/stars/permissions (`gh_get_repo_info`), branches (`gh_list_branches`), commit history (`gh_list_commits`), commit diff (`gh_get_commit_diff`), compare refs (`gh_compare_commits`), issues (`gh_list_issues`, `gh_get_issue`), pull requests (`gh_list_pull_requests`, `gh_get_pull_request_detail`), releases (`gh_list_releases`), GitHub Actions workflows & runs (`gh_list_workflow_runs`).
- Code download & batch reading: generate zip/tarball download URL (`gh_get_archive_download_url`), fetch multiple files in a directory bundle (`gh_fetch_directory_bundle`).
""".strip()


SERVER_INSTRUCTIONS = _build_server_instructions()

mcp = FastMCP(
    name="GitHub Personal",
    instructions=SERVER_INSTRUCTIONS,
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

_loop_clients: WeakKeyDictionary[asyncio.AbstractEventLoop, tuple[str, httpx.AsyncClient]] = WeakKeyDictionary()
_test_transport: httpx.AsyncBaseTransport | None = None


def set_test_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Test hook to inject an httpx mock transport."""
    global _test_transport
    _test_transport = transport
    _loop_clients.clear()


def _get_gh_client() -> tuple[httpx.AsyncClient | None, str | None]:
    pat = os.getenv("GITHUB_PAT", "").strip()
    base_url = os.getenv("GITHUB_API_BASE_URL", "https://api.github.com").strip().rstrip("/")
    if not pat:
        return None, "GITHUB_PAT is not configured."
    loop = asyncio.get_running_loop()
    cache_key = f"{base_url}|{pat}|{id(_test_transport)}"
    cached = _loop_clients.get(loop)
    if cached and cached[0] == cache_key and not cached[1].is_closed:
        return cached[1], None
    client = httpx.AsyncClient(
        base_url=base_url,
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "personal-fastmcp-github/1.0",
        },
        timeout=30.0,
        transport=_test_transport,
    )
    _loop_clients[loop] = (cache_key, client)
    return client, None


def _check_write_allowed(owner: str) -> str | None:
    allowed = _allowed_write_owners()
    if not allowed:
        return (
            "Write blocked: configure GITHUB_ALLOWED_WRITE_OWNERS (or GITHUB_DEFAULT_OWNER) "
            "in .env before performing mutating operations."
        )
    if owner.lower() not in allowed:
        return f"Write blocked: owner '{owner}' is not in GITHUB_ALLOWED_WRITE_OWNERS ({sorted(allowed)})."
    return None


async def _gh_request(method: str, path: str, **kwargs) -> dict | list:
    client, err = _get_gh_client()
    if err or client is None:
        raise RuntimeError(err or "GitHub client unavailable.")
    resp = await client.request(method, path, **kwargs)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


async def _gh_raw_bytes(path: str, params: dict | None = None) -> bytes:
    client, err = _get_gh_client()
    if err or client is None:
        raise RuntimeError(err or "GitHub client unavailable.")
    resp = await client.get(
        path,
        params=params,
        headers={"Accept": "application/vnd.github.raw+json"},
    )
    resp.raise_for_status()
    return resp.content


# ==================== 置顶读工具（4 个） ====================


@mcp.tool(annotations={"title": "List GitHub Repositories", **READ_ANNOTATIONS})
async def gh_list_repos(
    owner: str = "",
    visibility: Literal["all", "public", "private"] = "all",
    limit: int = 30,
) -> dict:
    """List personal repositories (owned public & private repos of the configured account) or any public GitHub user/org repositories (列出我的个人仓库、代码项目、主题仓库等，含私有仓库).
    Keywords: github list repos repositories projects private public personal.
    """
    default_owner = _default_owner()
    target_owner = owner.strip() or default_owner
    if not target_owner:
        return {
            "isError": True,
            "error": "Provide an explicit owner or configure GITHUB_DEFAULT_OWNER in .env.",
        }
    try:
        per_page = max(1, min(limit, 100))
        if default_owner and target_owner.lower() == default_owner.lower():
            data = await _gh_request(
                "GET",
                "/user/repos",
                params={
                    "visibility": visibility,
                    "affiliation": "owner",
                    "sort": "updated",
                    "per_page": per_page,
                },
            )
        else:
            try:
                data = await _gh_request(
                    "GET",
                    f"/users/{target_owner}/repos",
                    params={"sort": "updated", "per_page": per_page},
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    data = await _gh_request(
                        "GET",
                        f"/orgs/{target_owner}/repos",
                        params={"sort": "updated", "per_page": per_page},
                    )
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
        return {"owner": target_owner, "count": len(repos), "repos": repos}
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
    """List files and directory structure of a code repository (查看我的仓库或任意 GitHub 仓库的目录结构和文件列表), automatically ignoring .git, node_modules, dist, and lockfiles.
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
    """Read a source code file or README (or list a directory) in a personal or public code repository with line-range slicing (读取仓库源码、配置文件或 README 内容).
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
                "entries": [
                    {"name": e["name"], "path": e["path"], "type": e["type"], "size": e.get("size", 0)} for e in data
                ],
            }
        if data.get("encoding") == "base64" and data.get("content"):
            raw_bytes = base64.b64decode(data["content"])
        else:
            raw_bytes = await _gh_raw_bytes(f"/repos/{owner}/{repo}/contents/{clean_path}", params=params)

        if is_binary_bytes(raw_bytes):
            return {
                "type": "binary",
                "path": data.get("path", clean_path),
                "size": len(raw_bytes),
                "html_url": data.get("html_url", ""),
                "note": "Binary file cannot be displayed as text.",
            }
        raw_text = raw_bytes.decode("utf-8", errors="replace")
        sliced = slice_lines(
            raw_text,
            start_line=start_line,
            end_line=end_line,
            default_max_lines=_default_file_lines(),
        )
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
    """Search source code across personal or public GitHub repositories (在我的代码仓库或指定仓库中搜索源码、函数和关键词; pass repo as 'owner/name').
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
                existing = await _gh_request(
                    "GET", f"/repos/{owner}/{repo}/contents/{clean_path}", params={"ref": branch}
                )
                if isinstance(existing, dict):
                    sha = existing.get("sha", "")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
        payload: dict = {
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
    if not file_paths or len(file_paths) != len(file_contents):
        return {
            "isError": True,
            "error": "file_paths and file_contents must be non-empty lists of equal length.",
        }
    try:
        ref_data = await _gh_request("GET", f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
        parent_commit_sha = ref_data["object"]["sha"]
        commit_data = await _gh_request("GET", f"/repos/{owner}/{repo}/git/commits/{parent_commit_sha}")
        base_tree_sha = commit_data["tree"]["sha"]

        tree_entries = []
        for raw_path, content in zip(file_paths, file_contents, strict=True):
            clean_path = raw_path.lstrip("/")
            blob = await _gh_request(
                "POST",
                f"/repos/{owner}/{repo}/git/blobs",
                json={"content": content, "encoding": "utf-8"},
            )
            tree_entries.append(
                {
                    "path": clean_path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob["sha"],
                }
            )

        new_tree = await _gh_request(
            "POST",
            f"/repos/{owner}/{repo}/git/trees",
            json={"base_tree": base_tree_sha, "tree": tree_entries},
        )
        new_commit = await _gh_request(
            "POST",
            f"/repos/{owner}/{repo}/git/commits",
            json={
                "message": message,
                "tree": new_tree["sha"],
                "parents": [parent_commit_sha],
            },
        )
        await _gh_request(
            "PATCH",
            f"/repos/{owner}/{repo}/git/refs/heads/{branch}",
            json={"sha": new_commit["sha"]},
        )
        return {
            "branch": branch,
            "commit_sha": new_commit["sha"],
            "files_updated": [e["path"] for e in tree_entries],
            "html_url": new_commit.get("html_url", ""),
        }
    except Exception as exc:
        return {"isError": True, "error": format_http_error("GitHub", exc)}


@mcp.tool(annotations={"title": "Create Issue (Write)", **WRITE_ANNOTATIONS})
async def gh_create_issue(
    owner: str,
    repo: str,
    title: str,
    body: str = "",
    labels: list[str] | None = None,
) -> dict:
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


# ==================== BM25 隐藏只读/下载工具（13 个） ====================


@mcp.tool(annotations={"title": "Get Repository Info", **READ_ANNOTATIONS})
async def gh_get_repo_info(owner: str, repo: str) -> dict:
    """Get metadata for a GitHub repository including visibility, stars, language, default branch, and permissions.
    Keywords: github repo info metadata stars license default branch visibility.
    """
    try:
        r = await _gh_request("GET", f"/repos/{owner}/{repo}")
        return {
            "full_name": r["full_name"],
            "private": r["private"],
            "description": r.get("description") or "",
            "default_branch": r["default_branch"],
            "language": r.get("language") or "",
            "stargazers_count": r.get("stargazers_count", 0),
            "forks_count": r.get("forks_count", 0),
            "open_issues_count": r.get("open_issues_count", 0),
            "license": (r.get("license") or {}).get("spdx_id") if r.get("license") else "",
            "permissions": r.get("permissions") or {},
            "html_url": r["html_url"],
            "updated_at": r["updated_at"],
        }
    except Exception as exc:
        return {"isError": True, "error": format_http_error("GitHub", exc)}


@mcp.tool(annotations={"title": "List Repository Branches", **READ_ANNOTATIONS})
async def gh_list_branches(owner: str, repo: str, limit: int = 30) -> dict:
    """List branches of a GitHub repository.
    Keywords: github list branches refs heads.
    """
    try:
        data = await _gh_request("GET", f"/repos/{owner}/{repo}/branches", params={"per_page": max(1, min(limit, 100))})
        branches = [
            {"name": b["name"], "sha": b["commit"]["sha"], "protected": bool(b.get("protected", False))}
            for b in data[:limit]
        ]
        return {"repo": f"{owner}/{repo}", "count": len(branches), "branches": branches}
    except Exception as exc:
        return {"isError": True, "error": format_http_error("GitHub", exc)}


@mcp.tool(annotations={"title": "List Repository Commits", **READ_ANNOTATIONS})
async def gh_list_commits(owner: str, repo: str, ref: str = "", path: str = "", limit: int = 20) -> dict:
    """List recent commits for a repository branch/tag/SHA, optionally filtered by file path.
    Keywords: github list commits history log changes author.
    """
    try:
        params: dict = {"per_page": max(1, min(limit, 100))}
        if ref:
            params["sha"] = ref
        if path:
            params["path"] = path.lstrip("/")
        data = await _gh_request("GET", f"/repos/{owner}/{repo}/commits", params=params)
        commits = [
            {
                "sha": c["sha"],
                "message": (c["commit"].get("message") or "").splitlines()[0],
                "author": (c["commit"].get("author") or {}).get("name", ""),
                "date": (c["commit"].get("author") or {}).get("date", ""),
                "html_url": c.get("html_url", ""),
            }
            for c in data[:limit]
        ]
        return {"repo": f"{owner}/{repo}", "count": len(commits), "commits": commits}
    except Exception as exc:
        return {"isError": True, "error": format_http_error("GitHub", exc)}


@mcp.tool(annotations={"title": "Get Commit Diff", **READ_ANNOTATIONS})
async def gh_get_commit_diff(owner: str, repo: str, sha: str, max_chars: int | None = None) -> dict:
    """Get changed files and truncated patches for a specific commit SHA.
    Keywords: github commit diff patch changes inspect.
    """
    effective_max = max_chars if max_chars is not None else _default_diff_chars()
    try:
        c = await _gh_request("GET", f"/repos/{owner}/{repo}/commits/{sha}")
        files_out = []
        remaining = effective_max
        for f in c.get("files", []):
            patch = f.get("patch") or ""
            if remaining <= 0 and patch:
                patch = "[TRUNCATED: diff budget exhausted]"
            elif patch and effective_max > 0:
                if len(patch) > remaining:
                    patch = truncate_text(patch, max_chars=max(200, remaining))
                remaining -= len(patch)
            files_out.append(
                {
                    "filename": f["filename"],
                    "status": f["status"],
                    "additions": f.get("additions", 0),
                    "deletions": f.get("deletions", 0),
                    "patch": patch,
                }
            )
        return {
            "sha": c["sha"],
            "message": c["commit"].get("message", ""),
            "stats": c.get("stats") or {},
            "files": files_out,
        }
    except Exception as exc:
        return {"isError": True, "error": format_http_error("GitHub", exc)}


@mcp.tool(annotations={"title": "Compare Branches or Commits", **READ_ANNOTATIONS})
async def gh_compare_commits(owner: str, repo: str, base: str, head: str, max_chars: int | None = None) -> dict:
    """Compare two branches, tags, or commits (base...head) and show changed files with truncated patches.
    Keywords: github compare branches commits diff ahead behind.
    """
    effective_max = max_chars if max_chars is not None else _default_diff_chars()
    try:
        res = await _gh_request("GET", f"/repos/{owner}/{repo}/compare/{base}...{head}")
        files_out = []
        remaining = effective_max
        for f in res.get("files", []):
            patch = f.get("patch") or ""
            if remaining <= 0 and patch:
                patch = "[TRUNCATED: diff budget exhausted]"
            elif patch and effective_max > 0:
                if len(patch) > remaining:
                    patch = truncate_text(patch, max_chars=max(200, remaining))
                remaining -= len(patch)
            files_out.append(
                {
                    "filename": f["filename"],
                    "status": f["status"],
                    "additions": f.get("additions", 0),
                    "deletions": f.get("deletions", 0),
                    "patch": patch,
                }
            )
        return {
            "status": res.get("status", ""),
            "ahead_by": res.get("ahead_by", 0),
            "behind_by": res.get("behind_by", 0),
            "total_commits": res.get("total_commits", 0),
            "html_url": res.get("html_url", ""),
            "files": files_out,
        }
    except Exception as exc:
        return {"isError": True, "error": format_http_error("GitHub", exc)}


@mcp.tool(annotations={"title": "List Repository Issues", **READ_ANNOTATIONS})
async def gh_list_issues(
    owner: str,
    repo: str,
    state: Literal["open", "closed", "all"] = "open",
    limit: int = 20,
) -> dict:
    """List issues (excluding pull requests) in a GitHub repository.
    Keywords: github list issues bug tickets open closed.
    """
    try:
        data = await _gh_request(
            "GET",
            f"/repos/{owner}/{repo}/issues",
            params={"state": state, "per_page": max(1, min(limit, 100))},
        )
        issues = [
            {
                "number": i["number"],
                "title": i["title"],
                "state": i["state"],
                "author": (i.get("user") or {}).get("login", ""),
                "labels": [lb["name"] for lb in i.get("labels", [])],
                "comments": i.get("comments", 0),
                "updated_at": i.get("updated_at", ""),
                "html_url": i["html_url"],
            }
            for i in data
            if "pull_request" not in i
        ][:limit]
        return {"repo": f"{owner}/{repo}", "count": len(issues), "issues": issues}
    except Exception as exc:
        return {"isError": True, "error": format_http_error("GitHub", exc)}


@mcp.tool(annotations={"title": "Get Issue Detail & Comments", **READ_ANNOTATIONS})
async def gh_get_issue(owner: str, repo: str, issue_number: int) -> dict:
    """Get full details and comments of a GitHub Issue.
    Keywords: github issue detail comments thread body.
    """
    try:
        issue = await _gh_request("GET", f"/repos/{owner}/{repo}/issues/{issue_number}")
        comments = await _gh_request(
            "GET",
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            params={"per_page": 50},
        )
        return {
            "number": issue["number"],
            "title": issue["title"],
            "state": issue["state"],
            "author": (issue.get("user") or {}).get("login", ""),
            "body": truncate_text(issue.get("body") or "", max_chars=8000),
            "html_url": issue["html_url"],
            "comments": [
                {
                    "id": cm["id"],
                    "author": (cm.get("user") or {}).get("login", ""),
                    "created_at": cm.get("created_at", ""),
                    "body": truncate_text(cm.get("body") or "", max_chars=4000),
                }
                for cm in comments
            ],
        }
    except Exception as exc:
        return {"isError": True, "error": format_http_error("GitHub", exc)}


@mcp.tool(annotations={"title": "List Pull Requests", **READ_ANNOTATIONS})
async def gh_list_pull_requests(
    owner: str,
    repo: str,
    state: Literal["open", "closed", "all"] = "open",
    limit: int = 20,
) -> dict:
    """List Pull Requests in a GitHub repository.
    Keywords: github list pull requests pr open merged.
    """
    try:
        data = await _gh_request(
            "GET",
            f"/repos/{owner}/{repo}/pulls",
            params={"state": state, "per_page": max(1, min(limit, 100))},
        )
        prs = [
            {
                "number": p["number"],
                "title": p["title"],
                "state": p["state"],
                "draft": bool(p.get("draft", False)),
                "author": (p.get("user") or {}).get("login", ""),
                "head": p["head"]["ref"],
                "base": p["base"]["ref"],
                "updated_at": p.get("updated_at", ""),
                "html_url": p["html_url"],
            }
            for p in data[:limit]
        ]
        return {"repo": f"{owner}/{repo}", "count": len(prs), "pull_requests": prs}
    except Exception as exc:
        return {"isError": True, "error": format_http_error("GitHub", exc)}


@mcp.tool(annotations={"title": "Get Pull Request Detail & Diff", **READ_ANNOTATIONS})
async def gh_get_pull_request_detail(
    owner: str,
    repo: str,
    pr_number: int,
    include_files: bool = True,
    max_patch_chars: int | None = None,
) -> dict:
    """Get Pull Request metadata, description, and optionally changed files with truncated patches.
    Keywords: github pull request pr detail files diff patch review.
    """
    effective_max = max_patch_chars if max_patch_chars is not None else _default_diff_chars()
    try:
        pr = await _gh_request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}")
        out: dict = {
            "number": pr["number"],
            "title": pr["title"],
            "state": pr["state"],
            "merged": bool(pr.get("merged", False)),
            "head": pr["head"]["ref"],
            "base": pr["base"]["ref"],
            "author": (pr.get("user") or {}).get("login", ""),
            "body": truncate_text(pr.get("body") or "", max_chars=8000),
            "additions": pr.get("additions", 0),
            "deletions": pr.get("deletions", 0),
            "changed_files": pr.get("changed_files", 0),
            "html_url": pr["html_url"],
        }
        if include_files:
            files = await _gh_request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}/files", params={"per_page": 100})
            files_out = []
            remaining = effective_max
            for f in files:
                patch = f.get("patch") or ""
                if remaining <= 0 and patch:
                    patch = "[TRUNCATED: diff budget exhausted]"
                elif patch and effective_max > 0:
                    if len(patch) > remaining:
                        patch = truncate_text(patch, max_chars=max(200, remaining))
                    remaining -= len(patch)
                files_out.append(
                    {
                        "filename": f["filename"],
                        "status": f["status"],
                        "additions": f.get("additions", 0),
                        "deletions": f.get("deletions", 0),
                        "patch": patch,
                    }
                )
            out["files"] = files_out
        return out
    except Exception as exc:
        return {"isError": True, "error": format_http_error("GitHub", exc)}


@mcp.tool(annotations={"title": "List Releases", **READ_ANNOTATIONS})
async def gh_list_releases(owner: str, repo: str, limit: int = 10) -> dict:
    """List releases and release assets for a GitHub repository.
    Keywords: github releases tags versions changelog assets.
    """
    try:
        data = await _gh_request("GET", f"/repos/{owner}/{repo}/releases", params={"per_page": max(1, min(limit, 50))})
        releases = [
            {
                "tag_name": rel["tag_name"],
                "name": rel.get("name") or rel["tag_name"],
                "prerelease": bool(rel.get("prerelease", False)),
                "published_at": rel.get("published_at", ""),
                "body": truncate_text(rel.get("body") or "", max_chars=2000),
                "html_url": rel["html_url"],
                "assets": [
                    {"name": a["name"], "size": a["size"], "download_url": a["browser_download_url"]}
                    for a in rel.get("assets", [])
                ],
            }
            for rel in data[:limit]
        ]
        return {"repo": f"{owner}/{repo}", "count": len(releases), "releases": releases}
    except Exception as exc:
        return {"isError": True, "error": format_http_error("GitHub", exc)}


@mcp.tool(annotations={"title": "List GitHub Actions Workflow Runs", **READ_ANNOTATIONS})
async def gh_list_workflow_runs(owner: str, repo: str, limit: int = 10) -> dict:
    """List recent GitHub Actions workflow runs and their CI/CD status.
    Keywords: github actions workflow runs ci cd build status logs.
    """
    try:
        data = await _gh_request(
            "GET",
            f"/repos/{owner}/{repo}/actions/runs",
            params={"per_page": max(1, min(limit, 50))},
        )
        runs = [
            {
                "id": run["id"],
                "name": run.get("name") or "",
                "head_branch": run.get("head_branch") or "",
                "status": run.get("status") or "",
                "conclusion": run.get("conclusion") or "",
                "event": run.get("event") or "",
                "created_at": run.get("created_at") or "",
                "html_url": run.get("html_url") or "",
            }
            for run in data.get("workflow_runs", [])[:limit]
        ]
        return {"repo": f"{owner}/{repo}", "total_count": data.get("total_count", len(runs)), "runs": runs}
    except Exception as exc:
        return {"isError": True, "error": format_http_error("GitHub", exc)}


@mcp.tool(annotations={"title": "Get Repository Archive Download URL", **READ_ANNOTATIONS})
async def gh_get_archive_download_url(
    owner: str,
    repo: str,
    ref: str = "",
    archive_format: Literal["zipball", "tarball"] = "zipball",
) -> dict:
    """Generate a direct zip/tarball download URL (including short-lived signed URL for private repos) plus git clone / curl commands.
    Keywords: github download code zip zipball tarball archive clone export repository.
    """
    try:
        meta = await _gh_request("GET", f"/repos/{owner}/{repo}")
        target_ref = ref.strip() or meta["default_branch"]
        is_private = bool(meta.get("private", False))

        client, err = _get_gh_client()
        if err or client is None:
            return {"isError": True, "error": err or "GitHub client unavailable."}

        signed_url = ""
        resp = await client.get(
            f"/repos/{owner}/{repo}/{archive_format}/{target_ref}",
            follow_redirects=False,
        )
        if resp.status_code in {301, 302, 303, 307, 308} and resp.headers.get("Location"):
            signed_url = resp.headers["Location"]
        elif resp.status_code >= 400:
            resp.raise_for_status()

        ext = "zip" if archive_format == "zipball" else "tar.gz"
        public_web_url = f"https://github.com/{owner}/{repo}/archive/{target_ref}.{ext}"
        return {
            "repo": f"{owner}/{repo}",
            "ref": target_ref,
            "private": is_private,
            "archive_format": archive_format,
            "direct_download_url": signed_url or ("" if is_private else public_web_url),
            "public_archive_url": "" if is_private else public_web_url,
            "git_clone_cmd": f"git clone -b {target_ref} https://github.com/{owner}/{repo}.git",
            "curl_cmd": (
                f'curl -L -H "Authorization: Bearer $GITHUB_PAT" '
                f'"https://api.github.com/repos/{owner}/{repo}/{archive_format}/{target_ref}" '
                f"-o {repo}-{target_ref}.{ext}"
            ),
            "note": (
                "For private repos, direct_download_url is a short-lived signed link from GitHub; "
                "use git_clone_cmd or curl_cmd if it expires."
                if is_private
                else ""
            ),
        }
    except Exception as exc:
        return {"isError": True, "error": format_http_error("GitHub", exc)}


@mcp.tool(annotations={"title": "Fetch Directory Code Bundle", **READ_ANNOTATIONS})
async def gh_fetch_directory_bundle(
    owner: str,
    repo: str,
    path: str = "",
    ref: str = "",
    max_files: int = 10,
    max_total_chars: int = 18000,
) -> dict:
    """Batch-fetch multiple text source files inside a repository directory in one call for code review.
    Keywords: github download folder batch read multiple files bundle directory.
    """
    try:
        if not ref:
            meta = await _gh_request("GET", f"/repos/{owner}/{repo}")
            ref = meta["default_branch"]
        tree_data = await _gh_request("GET", f"/repos/{owner}/{repo}/git/trees/{ref}", params={"recursive": "1"})
        prefix = path.strip("/")

        candidates = []
        for entry in tree_data.get("tree", []):
            if entry.get("type") != "blob":
                continue
            p = entry["path"]
            parts = p.split("/")
            if set(parts) & IGNORED_DIRS or parts[-1] in IGNORED_FILES:
                continue
            if prefix and p != prefix and not p.startswith(prefix + "/"):
                continue
            candidates.append(p)

        bundled = []
        skipped = []
        used_chars = 0
        for file_path in candidates[: max(1, max_files)]:
            if used_chars >= max_total_chars:
                skipped.append({"path": file_path, "reason": "max_total_chars_reached"})
                continue
            try:
                raw_bytes = await _gh_raw_bytes(f"/repos/{owner}/{repo}/contents/{file_path}", params={"ref": ref})
            except Exception as exc:
                skipped.append({"path": file_path, "reason": format_http_error("GitHub", exc)})
                continue
            if is_binary_bytes(raw_bytes):
                skipped.append({"path": file_path, "reason": "binary_file"})
                continue
            text = raw_bytes.decode("utf-8", errors="replace")
            budget = max_total_chars - used_chars
            if len(text) > budget:
                text = truncate_text(text, max_chars=max(200, budget))
            used_chars += len(text)
            bundled.append({"path": file_path, "content": text})

        return {
            "repo": f"{owner}/{repo}",
            "ref": ref,
            "path_prefix": prefix,
            "total_candidates": len(candidates),
            "returned_files": len(bundled),
            "used_chars": used_chars,
            "files": bundled,
            "skipped": skipped,
        }
    except Exception as exc:
        return {"isError": True, "error": format_http_error("GitHub", exc)}


def create_http_app():
    return mcp.http_app(path="/mcp", stateless_http=True)


if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8102, path="/mcp", stateless_http=True)
