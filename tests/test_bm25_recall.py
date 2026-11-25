from __future__ import annotations

import base64
import json
import re
from typing import Any

import httpx
import pytest
from fastmcp import Client

from servers import etalib_web, github_personal


def _extract_tool_names_from_search_output(result: Any) -> list[str]:
    """Parse tool names from BM25SearchTransform markdown or JSON output."""
    texts: list[str] = []
    for item in getattr(result, "content", []) or []:
        txt = getattr(item, "text", None)
        if isinstance(txt, str):
            texts.append(txt)
    combined = "\n".join(texts)
    if not combined and getattr(result, "data", None) is not None:
        combined = str(result.data)

    # Markdown serializer formats headings like `### tool_name` or `**tool_name**` or `- **tool_name**`
    names: list[str] = []
    for match in re.finditer(r"(?:^|\n)\s*(?:#{1,4}\s+|[-*]\s+\*\*|\*\*)([a-z][a-z0-9_]+)", combined):
        candidate = match.group(1)
        if candidate not in names:
            names.append(candidate)
    if not names:
        for match in re.finditer(r"\b((?:notes|web|gh)_[a-z0-9_]+)\b", combined):
            candidate = match.group(1)
            if candidate not in names:
                names.append(candidate)
    return names


def _parse_tool_payload(result: Any) -> dict[str, Any]:
    if getattr(result, "structured_content", None) is not None:
        return dict(result.structured_content)
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    for item in getattr(result, "content", []) or []:
        txt = getattr(item, "text", None)
        if isinstance(txt, str):
            try:
                parsed = json.loads(txt)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
    raise AssertionError(f"Unexpected tool output shape: {result!r}")


@pytest.mark.asyncio
async def test_exposed_tool_counts_annotations_and_flat_schemas() -> None:
    async with Client(etalib_web.mcp) as c1:
        tools1 = await c1.list_tools()
    names1 = [t.name for t in tools1]
    assert len(names1) == 7, f"Expected 7 tools on Server 1, got {len(names1)}: {names1}"
    assert set(names1) == {
        "notes_search_notes",
        "notes_read_document",
        "notes_list_shares",
        "web_scrape",
        "web_map",
        "search_tools",
        "call_tool",
    }
    assert "firecrawl_search" not in names1

    for tool in tools1:
        schema = tool.inputSchema
        assert schema.get("type") == "object"
        assert "$defs" not in json.dumps(schema), f"Nested $defs found in {tool.name}: {schema}"

    async with Client(github_personal.mcp) as c2:
        tools2 = await c2.list_tools()
    names2 = [t.name for t in tools2]
    assert len(names2) == 12, f"Expected 12 tools on Server 2, got {len(names2)}: {names2}"
    expected_writes = {
        "gh_create_or_update_file",
        "gh_create_pull_request",
        "gh_create_branch",
        "gh_push_multiple_files",
        "gh_create_issue",
        "gh_comment_issue_or_pr",
    }
    assert expected_writes.issubset(set(names2))

    by_name2 = {t.name: t for t in tools2}
    for wname in expected_writes:
        ann = by_name2[wname].annotations
        assert ann is not None and ann.destructiveHint is True, f"{wname} must expose destructiveHint=True"
        assert ann.readOnlyHint is False, f"{wname} must expose readOnlyHint=False"

    for tool in tools2:
        schema = tool.inputSchema
        assert schema.get("type") == "object"
        assert "$defs" not in json.dumps(schema), f"Nested $defs found in {tool.name}: {schema}"


@pytest.mark.asyncio
async def test_bm25_english_recall_top3() -> None:
    server1_queries = [
        ("note folder tree directory", {"notes_get_share_tree"}),
        ("document outline headings sections", {"notes_get_document_outline"}),
        ("crawl multiple subpages", {"web_crawl"}),
        ("parse remote pdf file", {"web_scrape_pdf"}),
        ("extract structured json schema", {"web_extract_structured"}),
        ("click button interact browser", {"web_interact_action", "web_interact_start"}),
    ]
    async with Client(etalib_web.mcp) as c1:
        for query, expected_any in server1_queries:
            res = await c1.call_tool("search_tools", {"query": query})
            ranked = _extract_tool_names_from_search_output(res)
            top3 = ranked[:3]
            assert any(t in top3 for t in expected_any), (
                f"Server 1 BM25 failed for {query!r}: expected one of {expected_any} in top3, got {ranked}"
            )

        # Ensure firecrawl_search is never indexed or exposed
        fc_search_res = await c1.call_tool("search_tools", {"query": "firecrawl_search"})
        assert "firecrawl_search" not in _extract_tool_names_from_search_output(fc_search_res)

    server2_queries = [
        ("download repository zip archive", {"gh_get_archive_download_url"}),
        ("list pull requests and diff", {"gh_list_pull_requests", "gh_get_pull_request_detail"}),
        ("github actions workflow runs", {"gh_list_workflow_runs"}),
        ("commit history and changes", {"gh_list_commits"}),
    ]
    async with Client(github_personal.mcp) as c2:
        for query, expected_any in server2_queries:
            res = await c2.call_tool("search_tools", {"query": query})
            ranked = _extract_tool_names_from_search_output(res)
            top3 = ranked[:3]
            assert any(t in top3 for t in expected_any), (
                f"Server 2 BM25 failed for {query!r}: expected one of {expected_any} in top3, got {ranked}"
            )


@pytest.mark.asyncio
async def test_etalib_web_mock_integration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ETALIB_NOTES_RPC_URL", "http://127.0.0.1:8081/mcp")
    monkeypatch.setenv("ETALIB_NOTES_INTERNAL_TOKEN", "internal-secret-token")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setenv("FIRECRAWL_API_BASE_URL", "https://api.firecrawl.dev/v2")

    recorded_notes: list[dict[str, Any]] = []
    recorded_fc: list[tuple[str, str, Any]] = []

    def notes_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-MCP-Internal-Token") == "internal-secret-token"
        body = json.loads(request.content.decode("utf-8"))
        recorded_notes.append(body)
        tool = body["params"]["name"]
        if tool == "list_shares":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "structuredContent": {
                            "shares": [{"slug": "sem5note", "title": "GTIIT 5th Semester"}],
                            "total_returned": 1,
                        },
                        "isError": False,
                    },
                },
            )
        if tool == "read_document":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "structuredContent": {
                            "title": "11.6 Tangent Space",
                            "content": "Let $M$ be a smooth manifold.",
                            "offset": 0,
                            "next_offset": None,
                        },
                        "isError": False,
                    },
                },
            )
        return httpx.Response(500, text="unexpected tool")

    def fc_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8")) if request.content else None
        recorded_fc.append((request.method, request.url.path, payload))
        if request.method == "POST" and request.url.path == "/v2/scrape":
            if payload.get("parsers"):
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "markdown": "PDF Page 1\n" + ("A" * 500),
                            "metadata": {"title": "Lecture.pdf"},
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "markdown": "Article content " + ("X" * 300),
                        "metadata": {"scrapeId": "scr_123", "title": "Blog"},
                    },
                },
            )
        if request.method == "POST" and request.url.path == "/v2/scrape/scr_123/interact":
            return httpx.Response(200, json={"success": True, "output": "Clicked button"})
        if request.method == "DELETE" and request.url.path == "/v2/scrape/scr_123/interact":
            return httpx.Response(200, json={"success": True, "creditsBilled": 2})
        if request.method == "GET" and request.url.path == "/v2/team/credit-usage":
            return httpx.Response(200, json={"success": True, "data": {"remainingCredits": 4500}})
        if request.method == "POST" and request.url.path == "/v2/crawl":
            return httpx.Response(200, json={"success": True, "id": "crawl_999"})
        if request.method == "POST" and request.url.path == "/v2/extract":
            return httpx.Response(200, json={"success": True, "status": "completed", "data": {"title": "OK"}})
        return httpx.Response(404, json={"error": "not found"})

    etalib_web.set_test_transports(
        notes=httpx.MockTransport(notes_handler),
        firecrawl=httpx.MockTransport(fc_handler),
    )
    try:
        async with Client(etalib_web.mcp) as client:
            r_shares = _parse_tool_payload(await client.call_tool("notes_list_shares", {}))
            assert r_shares["total_returned"] == 1
            assert recorded_notes[-1]["params"]["name"] == "list_shares"

            r_doc = _parse_tool_payload(
                await client.call_tool("notes_read_document", {"share": "sem5note", "doc_id": "d1"})
            )
            assert "smooth manifold" in r_doc["content"]

            r_scrape = _parse_tool_payload(
                await client.call_tool("web_scrape", {"url": "https://www.etalib.space", "max_chars": 50})
            )
            assert "[TRUNCATED:" in r_scrape["markdown"]

            r_pdf = _parse_tool_payload(
                await client.call_tool(
                    "call_tool",
                    {"name": "web_scrape_pdf", "arguments": {"url": "https://example.com/a.pdf", "max_chars": 0}},
                )
            )
            assert len(r_pdf["markdown"]) > 500  # max_chars <= 0 disables truncation

            r_start = _parse_tool_payload(
                await client.call_tool(
                    "call_tool",
                    {"name": "web_interact_start", "arguments": {"url": "https://example.com/app"}},
                )
            )
            assert r_start["scrape_id"] == "scr_123"

            r_act = _parse_tool_payload(
                await client.call_tool(
                    "call_tool",
                    {"name": "web_interact_action", "arguments": {"scrape_id": "scr_123", "prompt": "click Next"}},
                )
            )
            assert r_act["output"] == "Clicked button"

            r_stop = _parse_tool_payload(
                await client.call_tool("call_tool", {"name": "web_interact_stop", "arguments": {"scrape_id": "scr_123"}})
            )
            assert r_stop["creditsBilled"] == 2

            r_credits = _parse_tool_payload(
                await client.call_tool("call_tool", {"name": "web_check_credit_usage", "arguments": {}})
            )
            assert r_credits["data"]["remainingCredits"] == 4500

            r_extract = _parse_tool_payload(
                await client.call_tool(
                    "call_tool",
                    {
                        "name": "web_extract_structured",
                        "arguments": {"urls": ["https://example.com"], "prompt": "extract title"},
                    },
                )
            )
            assert r_extract["data"]["title"] == "OK"

        # Upstream connection error must return clean isError without crashing server
        def broken_notes(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        etalib_web.set_test_transports(notes=httpx.MockTransport(broken_notes), firecrawl=None)
        async with Client(etalib_web.mcp) as client:
            r_err = _parse_tool_payload(await client.call_tool("notes_list_shares", {}))
            assert r_err["isError"] is True
            assert "connection error" in r_err["error"]
    finally:
        etalib_web.set_test_transports(notes=None, firecrawl=None)


@pytest.mark.asyncio
async def test_github_personal_mock_integration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_PAT", "github_pat_test")
    monkeypatch.setenv("GITHUB_DEFAULT_OWNER", "Chen017")
    monkeypatch.setenv("GITHUB_ALLOWED_WRITE_OWNERS", "Chen017")
    monkeypatch.setenv("GITHUB_API_BASE_URL", "https://api.github.com")

    calls: list[tuple[str, str, dict[str, Any]]] = []

    def gh_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        calls.append((request.method, request.url.path, dict(request.url.params)))

        if request.method == "GET" and request.url.path == "/user/repos":
            assert request.url.params.get("affiliation") == "owner"
            return httpx.Response(
                200,
                json=[
                    {
                        "full_name": "Chen017/demo",
                        "private": True,
                        "default_branch": "main",
                        "description": "Demo repo",
                        "updated_at": "2026-11-25T00:00:00Z",
                        "html_url": "https://github.com/Chen017/demo",
                    }
                ],
            )
        if request.method == "GET" and request.url.path == "/repos/Chen017/demo":
            return httpx.Response(200, json={"default_branch": "main", "private": True})
        if request.method == "GET" and request.url.path == "/repos/Chen017/demo/git/trees/main":
            return httpx.Response(
                200,
                json={
                    "truncated": True,
                    "tree": [
                        {"path": "src/app.py", "type": "blob", "size": 100},
                        {"path": "node_modules/pkg/index.js", "type": "blob", "size": 50},
                    ],
                },
            )
        if request.method == "GET" and request.url.path == "/repos/Chen017/demo/contents/src":
            return httpx.Response(200, json=[{"name": "app.py", "path": "src/app.py", "type": "file", "size": 100}])
        if request.method == "GET" and request.url.path == "/repos/Chen017/demo/contents/big.txt":
            if request.headers.get("Accept") == "application/vnd.github.raw+json":
                return httpx.Response(200, content=b"line1\nline2\n")
            return httpx.Response(200, json={"path": "big.txt", "sha": "s1", "html_url": "https://gh/big.txt"})
        if request.method == "GET" and request.url.path == "/repos/Chen017/demo/contents/bin.dat":
            return httpx.Response(
                200,
                json={
                    "path": "bin.dat",
                    "encoding": "base64",
                    "content": base64.b64encode(b"\x89PNG\r\n\x1a\n\x00\x00").decode("ascii"),
                    "html_url": "https://gh/bin.dat",
                },
            )
        if request.method == "GET" and request.url.path == "/repos/Chen017/demo/zipball/main":
            return httpx.Response(302, headers={"Location": "https://codeload.github.com/signed-zip-url"})

        # Git Data API 6 steps for gh_push_multiple_files
        if request.method == "GET" and request.url.path == "/repos/Chen017/demo/git/ref/heads/feat":
            return httpx.Response(200, json={"object": {"sha": "commit_base"}})
        if request.method == "GET" and request.url.path == "/repos/Chen017/demo/git/commits/commit_base":
            return httpx.Response(200, json={"tree": {"sha": "tree_base"}})
        if request.method == "POST" and request.url.path == "/repos/Chen017/demo/git/blobs":
            return httpx.Response(201, json={"sha": f"blob_{len(body['content'])}"})
        if request.method == "POST" and request.url.path == "/repos/Chen017/demo/git/trees":
            assert body["base_tree"] == "tree_base"
            assert len(body["tree"]) == 2
            return httpx.Response(201, json={"sha": "tree_new"})
        if request.method == "POST" and request.url.path == "/repos/Chen017/demo/git/commits":
            assert body["tree"] == "tree_new"
            assert body["parents"] == ["commit_base"]
            return httpx.Response(201, json={"sha": "commit_new", "html_url": "https://github.com/Chen017/demo/commit/commit_new"})
        if request.method == "PATCH" and request.url.path == "/repos/Chen017/demo/git/refs/heads/feat":
            assert body["sha"] == "commit_new"
            return httpx.Response(200, json={"object": {"sha": "commit_new"}})

        return httpx.Response(404, json={"message": "Not Found"})

    github_personal.set_test_transport(httpx.MockTransport(gh_handler))
    try:
        async with Client(github_personal.mcp) as client:
            repos = _parse_tool_payload(await client.call_tool("gh_list_repos", {}))
            assert repos["count"] == 1
            assert repos["repos"][0]["full_name"] == "Chen017/demo"

            tree = _parse_tool_payload(
                await client.call_tool("gh_get_repo_tree", {"owner": "Chen017", "repo": "demo"})
            )
            assert tree["tree_truncated_by_github"] is True
            assert [e["path"] for e in tree["entries"]] == ["src/app.py"]

            dir_res = _parse_tool_payload(
                await client.call_tool("gh_get_file_contents", {"owner": "Chen017", "repo": "demo", "path": "src"})
            )
            assert dir_res["type"] == "directory"

            big_res = _parse_tool_payload(
                await client.call_tool("gh_get_file_contents", {"owner": "Chen017", "repo": "demo", "path": "big.txt"})
            )
            assert big_res["type"] == "file"
            assert "1: line1" in big_res["content"]

            bin_res = _parse_tool_payload(
                await client.call_tool("gh_get_file_contents", {"owner": "Chen017", "repo": "demo", "path": "bin.dat"})
            )
            assert bin_res["type"] == "binary"

            blocked = _parse_tool_payload(
                await client.call_tool(
                    "gh_create_branch",
                    {"owner": "someone-else", "repo": "demo", "new_branch": "feat"},
                )
            )
            assert blocked["isError"] is True
            assert "Write blocked" in blocked["error"]

            pushed = _parse_tool_payload(
                await client.call_tool(
                    "gh_push_multiple_files",
                    {
                        "owner": "Chen017",
                        "repo": "demo",
                        "branch": "feat",
                        "file_paths": ["a.py", "b.py"],
                        "file_contents": ["print(1)", "print(2)"],
                        "message": "update files",
                    },
                )
            )
            assert pushed["commit_sha"] == "commit_new"
            assert pushed["files_updated"] == ["a.py", "b.py"]

            dl = _parse_tool_payload(
                await client.call_tool(
                    "call_tool",
                    {
                        "name": "gh_get_archive_download_url",
                        "arguments": {"owner": "Chen017", "repo": "demo"},
                    },
                )
            )
            assert dl["direct_download_url"] == "https://codeload.github.com/signed-zip-url"
    finally:
        github_personal.set_test_transport(None)


@pytest.mark.asyncio
async def test_stateless_streamable_http_smoke() -> None:
    for server_mod, expected_count in ((etalib_web, 7), (github_personal, 12)):
        app = server_mod.create_http_app()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as http_client:
                init_resp = await http_client.post(
                    "/mcp",
                    headers={"Accept": "application/json, text/event-stream"},
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "pytest", "version": "1.0"},
                        },
                    },
                )
                assert init_resp.status_code == 200, init_resp.text

                # Send tools/list WITHOUT Mcp-Session-Id header to verify stateless_http=True
                list_resp = await http_client.post(
                    "/mcp",
                    headers={"Accept": "application/json, text/event-stream"},
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                )
                assert list_resp.status_code == 200, list_resp.text
                body_text = list_resp.text
                if list_resp.headers.get("content-type", "").startswith("text/event-stream"):
                    data_lines = [line[6:] for line in body_text.splitlines() if line.startswith("data: ")]
                    payload = json.loads(data_lines[-1])
                else:
                    payload = list_resp.json()
                assert len(payload["result"]["tools"]) == expected_count
