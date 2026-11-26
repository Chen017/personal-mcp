from __future__ import annotations

import base64
import json
import re
from typing import Any

import httpx
import pytest
from fastmcp import Client

from servers import github_personal, notes_web


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
    instructions1 = notes_web._build_server_instructions()
    for forbidden_phrase in ("ONLY invoke", "Do NOT use", "when built-in browsing fails", "fallback web scraping"):
        assert forbidden_phrase not in instructions1
    assert "check my notes" in instructions1
    assert "我的仓库" in github_personal.SERVER_INSTRUCTIONS

    async with Client(notes_web.mcp) as c1:
        tools1 = await c1.list_tools()
    names1 = [t.name for t in tools1]
    assert len(names1) == 8, f"Expected 8 tools on Server 1, got {len(names1)}: {names1}"
    assert set(names1) == {
        "notes_search_notes",
        "notes_read_document",
        "notes_list_shares",
        "web_search",
        "web_scrape",
        "web_map",
        "search_tools",
        "call_tool",
    }
    assert "firecrawl_search" not in names1

    for tool in tools1:
        schema = tool.input_schema
        assert schema.get("type") == "object"
        assert "$defs" not in json.dumps(schema), f"Nested $defs found in {tool.name}: {schema}"

    async with Client(github_personal.mcp) as c2:
        tools2 = await c2.list_tools()
    names2 = [t.name for t in tools2]
    assert len(names2) == 13, f"Expected 13 tools on Server 2, got {len(names2)}: {names2}"
    expected_writes = {
        "gh_create_or_update_file",
        "gh_patch_files",
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
        assert ann is not None and ann.destructive_hint is True, f"{wname} must expose destructive_hint=True"
        assert ann.read_only_hint is False, f"{wname} must expose read_only_hint=False"

    for tool in tools2:
        schema = tool.input_schema
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
    async with Client(notes_web.mcp) as c1:
        for query, expected_any in server1_queries:
            res = await c1.call_tool("search_tools", {"query": query})
            ranked = _extract_tool_names_from_search_output(res)
            top3 = ranked[:3]
            assert any(t in top3 for t in expected_any), (
                f"Server 1 BM25 failed for {query!r}: expected one of {expected_any} in top3, got {ranked}"
            )

        fc_search_res = await c1.call_tool("search_tools", {"query": "firecrawl_search"})
        assert "firecrawl_search" not in _extract_tool_names_from_search_output(fc_search_res)

    server2_queries = [
        ("download repository zip archive", {"gh_get_archive_download_url"}),
        ("list pull requests and diff", {"gh_list_pull_requests", "gh_get_pull_request_detail"}),
        ("pull request review comments feedback", {"gh_get_pull_request_detail"}),
        ("github actions workflow runs", {"gh_list_workflow_runs"}),
        ("failed workflow run job logs error", {"gh_get_workflow_run_jobs"}),
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
async def test_notes_web_mock_integration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTES_RPC_URL", "http://127.0.0.1:8081/mcp")
    monkeypatch.setenv("NOTES_INTERNAL_TOKEN", "internal-secret-token")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key")
    monkeypatch.setenv("FIRECRAWL_API_BASE_URL", "https://api.firecrawl.dev/v2")
    monkeypatch.setenv("MAX_SINGLE_READ_CHARS", "2000")
    monkeypatch.setenv("CRAWL_SOFT_MAX_PAGES", "20")
    monkeypatch.setenv("BATCH_SOFT_MAX_URLS", "3")
    monkeypatch.setenv("EXTRACT_SOFT_MAX_URLS", "2")

    recorded_notes: list[dict[str, Any]] = []
    recorded_fc: list[tuple[str, str, Any]] = []
    full_blog_md = ("BlogIntro " * 120) + "MIDDLE_MARKER " + ("BlogTail " * 200)
    full_pdf_md = ("Header line\n" * 80) + "Theorem 4.2: Stokes theorem on manifolds.\n" + ("Proof body " * 220)

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
                            "shares": [{"slug": "demo-notebook", "title": "Sample Course Notebook"}],
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
                            "markdown": full_pdf_md,
                            "metadata": {"title": "Lecture.pdf"},
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "markdown": full_blog_md,
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
        if request.method == "GET" and request.url.path == "/v2/crawl/crawl_999":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "status": "completed",
                    "data": [
                        {
                            "markdown": f"Crawled page {i} content",
                            "metadata": {"sourceURL": f"https://example.com/p{i}"},
                        }
                        for i in range(4)
                    ],
                },
            )
        if request.method == "POST" and request.url.path == "/v2/batch/scrape":
            return httpx.Response(200, json={"success": True, "id": "batch_1"})
        if request.method == "GET" and request.url.path == "/v2/batch/scrape/batch_1":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "status": "completed",
                    "data": [
                        {
                            "markdown": f"Batch item {i}",
                            "metadata": {"sourceURL": f"https://example.com/b{i}"},
                        }
                        for i in range(4)
                    ],
                },
            )
        if request.method == "POST" and request.url.path == "/v2/search":
            assert "scrapeOptions" not in (payload or {})
            q = (payload or {}).get("query")
            if q == "third party harness":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "web": [
                                {
                                    "url": "https://example.com/harness-docs",
                                    "title": "Harness Docs",
                                    "description": "Official harness guide",
                                }
                            ],
                            "news": [
                                {
                                    "url": "https://news.example.com/harness",
                                    "title": "Harness Released",
                                    "snippet": "Launched recently",
                                    "date": "2026-11-20",
                                }
                            ],
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "url": "https://example.com/list-result",
                            "title": "List Shape Result",
                            "highlights": ["Matched snippet"],
                        }
                    ],
                },
            )
        if request.method == "POST" and request.url.path == "/v2/extract":
            return httpx.Response(200, json={"success": True, "status": "completed", "data": {"title": "OK"}})
        return httpx.Response(404, json={"error": "not found"})

    notes_web.set_test_transports(
        notes=httpx.MockTransport(notes_handler),
        firecrawl=httpx.MockTransport(fc_handler),
    )
    try:
        async with Client(notes_web.mcp) as client:
            r_shares = _parse_tool_payload(await client.call_tool("notes_list_shares", {}))
            assert r_shares["total_returned"] == 1
            assert recorded_notes[-1]["params"]["name"] == "list_shares"

            r_doc = _parse_tool_payload(
                await client.call_tool("notes_read_document", {"share": "demo-notebook", "doc_id": "d1"})
            )
            assert "smooth manifold" in r_doc["content"]

            # 1) web_scrape: first read hits Firecrawl once; paginated reads hit cache (0 extra hits)
            chunks: list[str] = []
            r1 = _parse_tool_payload(
                await client.call_tool("web_scrape", {"url": "https://blog.example.com", "max_chars": 1200})
            )
            assert r1["cached"] is False
            assert r1["truncated"] is True
            assert r1["next_offset"] == 1200
            chunks.append(r1["markdown"])

            assert sum(1 for m, p, _ in recorded_fc if (m, p) == ("POST", "/v2/scrape")) == 1

            offset = r1["next_offset"]
            while offset is not None:
                rn = _parse_tool_payload(
                    await client.call_tool(
                        "web_scrape",
                        {"url": "https://blog.example.com", "offset": offset, "max_chars": 1200},
                    )
                )
                assert rn["cached"] is True
                chunks.append(rn["markdown"])
                offset = rn["next_offset"]
            assert "".join(chunks) == full_blog_md
            assert sum(1 for m, p, _ in recorded_fc if (m, p) == ("POST", "/v2/scrape")) == 1

            # 2) web_scrape_pdf: max_chars clamped to MAX_SINGLE_READ_CHARS (2000), find_keyword jumps in cache, refresh=True refetches
            r_pdf1 = _parse_tool_payload(
                await client.call_tool(
                    "call_tool",
                    {"name": "web_scrape_pdf", "arguments": {"url": "https://example.com/a.pdf", "max_chars": 999999}},
                )
            )
            assert r_pdf1["cached"] is False
            assert r_pdf1["returned_chars"] == 2000
            assert r_pdf1["truncated"] is True

            r_pdf_kw = _parse_tool_payload(
                await client.call_tool(
                    "call_tool",
                    {
                        "name": "web_scrape_pdf",
                        "arguments": {
                            "url": "https://example.com/a.pdf",
                            "find_keyword": "Stokes theorem",
                            "max_chars": 1000,
                        },
                    },
                )
            )
            assert r_pdf_kw["cached"] is True
            assert r_pdf_kw["keyword_search"]["matched"] is True
            assert "Stokes theorem" in r_pdf_kw["markdown"]
            assert sum(1 for m, p, _ in recorded_fc if (m, p) == ("POST", "/v2/scrape")) == 2

            r_pdf_refresh = _parse_tool_payload(
                await client.call_tool(
                    "call_tool",
                    {
                        "name": "web_scrape_pdf",
                        "arguments": {"url": "https://example.com/a.pdf", "refresh": True},
                    },
                )
            )
            assert r_pdf_refresh["cached"] is False
            assert sum(1 for m, p, _ in recorded_fc if (m, p) == ("POST", "/v2/scrape")) == 3

            # 3) web_crawl soft guardrail + confirm_large + web_crawl_status pagination & cache warming
            r_crawl_guard = _parse_tool_payload(
                await client.call_tool(
                    "call_tool",
                    {"name": "web_crawl", "arguments": {"url": "https://example.com", "limit": 50}},
                )
            )
            assert r_crawl_guard["needs_confirmation"] is True
            assert sum(1 for m, p, _ in recorded_fc if (m, p) == ("POST", "/v2/crawl")) == 0

            r_crawl_ok = _parse_tool_payload(
                await client.call_tool(
                    "call_tool",
                    {
                        "name": "web_crawl",
                        "arguments": {"url": "https://example.com", "limit": 50, "confirm_large": True},
                    },
                )
            )
            assert r_crawl_ok["id"] == "crawl_999"

            r_crawl_p1 = _parse_tool_payload(
                await client.call_tool(
                    "call_tool",
                    {
                        "name": "web_crawl_status",
                        "arguments": {"crawl_id": "crawl_999", "page_offset": 0, "page_limit": 2},
                    },
                )
            )
            assert r_crawl_p1["total_pages"] == 4
            assert r_crawl_p1["returned_pages"] == 2
            assert r_crawl_p1["next_page_offset"] == 2

            r_warm = _parse_tool_payload(await client.call_tool("web_scrape", {"url": "https://example.com/p3"}))
            assert r_warm["cached"] is True
            assert "Crawled page 3" in r_warm["markdown"]

            # 4) web_batch_scrape soft guardrail + pagination
            r_batch_guard = _parse_tool_payload(
                await client.call_tool(
                    "call_tool",
                    {
                        "name": "web_batch_scrape",
                        "arguments": {"urls": [f"https://example.com/b{i}" for i in range(4)]},
                    },
                )
            )
            assert r_batch_guard["needs_confirmation"] is True

            r_batch_ok = _parse_tool_payload(
                await client.call_tool(
                    "call_tool",
                    {
                        "name": "web_batch_scrape",
                        "arguments": {
                            "urls": [f"https://example.com/b{i}" for i in range(4)],
                            "confirm_large": True,
                            "page_offset": 2,
                            "page_limit": 2,
                        },
                    },
                )
            )
            assert r_batch_ok["total_pages"] == 4
            assert r_batch_ok["returned_pages"] == 2
            assert r_batch_ok["next_page_offset"] is None

            # 5) web_extract_structured soft guardrail + normal run
            r_ext_guard = _parse_tool_payload(
                await client.call_tool(
                    "call_tool",
                    {
                        "name": "web_extract_structured",
                        "arguments": {"urls": ["https://a.com", "https://b.com", "https://c.com"], "prompt": "extract"},
                    },
                )
            )
            assert r_ext_guard["needs_confirmation"] is True

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

            # 6) Interact session + credit check
            r_start = _parse_tool_payload(
                await client.call_tool(
                    "call_tool",
                    {"name": "web_interact_start", "arguments": {"url": "https://example.com/app"}},
                )
            )
            assert r_start["scrape_id"] == "scr_123"
            assert "web_interact_stop" in r_start["reminder"]

            r_act = _parse_tool_payload(
                await client.call_tool(
                    "call_tool",
                    {"name": "web_interact_action", "arguments": {"scrape_id": "scr_123", "prompt": "click Next"}},
                )
            )
            assert r_act["output"] == "Clicked button"

            r_stop = _parse_tool_payload(
                await client.call_tool(
                    "call_tool", {"name": "web_interact_stop", "arguments": {"scrape_id": "scr_123"}}
                )
            )
            assert r_stop["creditsBilled"] == 2

            r_credits = _parse_tool_payload(
                await client.call_tool("call_tool", {"name": "web_check_credit_usage", "arguments": {}})
            )
            assert r_credits["data"]["remainingCredits"] == 4500

            # 7) web_search: limit clamped <=10, no scrapeOptions, dict+list shapes, repeat cache & refresh
            r_ws1 = _parse_tool_payload(
                await client.call_tool(
                    "web_search",
                    {"query": "third party harness", "limit": 50, "tbs": "qdr:w", "country": "us"},
                )
            )
            assert r_ws1["limit"] == 10
            assert r_ws1["cached"] is False
            assert r_ws1["count"] == 2
            assert {item["source"] for item in r_ws1["results"]} == {"web", "news"}
            search_calls = [(m, p, body) for m, p, body in recorded_fc if (m, p) == ("POST", "/v2/search")]
            assert len(search_calls) == 1
            assert search_calls[0][2] == {
                "query": "third party harness",
                "limit": 10,
                "tbs": "qdr:w",
                "country": "US",
            }

            r_ws_cached = _parse_tool_payload(
                await client.call_tool(
                    "web_search",
                    {"query": "third party harness", "limit": 50, "tbs": "qdr:w", "country": "us"},
                )
            )
            assert r_ws_cached["cached"] is True
            assert sum(1 for m, p, _ in recorded_fc if (m, p) == ("POST", "/v2/search")) == 1

            r_ws_refresh = _parse_tool_payload(
                await client.call_tool(
                    "web_search",
                    {"query": "third party harness", "limit": 50, "tbs": "qdr:w", "country": "us", "refresh": True},
                )
            )
            assert r_ws_refresh["cached"] is False
            assert sum(1 for m, p, _ in recorded_fc if (m, p) == ("POST", "/v2/search")) == 2

            r_ws_list = _parse_tool_payload(await client.call_tool("web_search", {"query": "list shape"}))
            assert r_ws_list["count"] == 1
            assert r_ws_list["results"][0]["description"] == "Matched snippet"

            r_ws_err = _parse_tool_payload(await client.call_tool("web_search", {"query": "   "}))
            assert r_ws_err["isError"] is True

        def broken_notes(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        notes_web.set_test_transports(notes=httpx.MockTransport(broken_notes), firecrawl=None)
        async with Client(notes_web.mcp) as client:
            r_err = _parse_tool_payload(await client.call_tool("notes_list_shares", {}))
            assert r_err["isError"] is True
            assert "connection error" in r_err["error"]
    finally:
        notes_web.set_test_transports(notes=None, firecrawl=None)


@pytest.mark.asyncio
async def test_github_personal_mock_integration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_PAT", "github_pat_test")
    monkeypatch.setenv("GITHUB_DEFAULT_OWNER", "alice")
    monkeypatch.setenv("GITHUB_ALLOWED_WRITE_OWNERS", "alice")
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
                        "full_name": "alice/demo",
                        "private": True,
                        "default_branch": "main",
                        "description": "Demo repo",
                        "updated_at": "2026-11-25T00:00:00Z",
                        "html_url": "https://github.com/alice/demo",
                    }
                ],
            )
        if request.method == "GET" and request.url.path == "/repos/alice/demo":
            return httpx.Response(200, json={"default_branch": "main", "private": True})
        if request.method == "GET" and request.url.path == "/repos/alice/demo/git/trees/main":
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
        if request.method == "GET" and request.url.path == "/repos/alice/demo/contents/src":
            return httpx.Response(200, json=[{"name": "app.py", "path": "src/app.py", "type": "file", "size": 100}])
        if request.method == "GET" and request.url.path == "/repos/alice/demo/contents/big.txt":
            if request.headers.get("Accept") == "application/vnd.github.raw+json":
                return httpx.Response(200, content=b"line1\nline2\n")
            return httpx.Response(200, json={"path": "big.txt", "sha": "s1", "html_url": "https://gh/big.txt"})
        if request.method == "GET" and request.url.path == "/repos/alice/demo/contents/bin.dat":
            return httpx.Response(
                200,
                json={
                    "path": "bin.dat",
                    "encoding": "base64",
                    "content": base64.b64encode(b"\x89PNG\r\n\x1a\n\x00\x00").decode("ascii"),
                    "html_url": "https://gh/bin.dat",
                },
            )
        if request.method == "GET" and request.url.path == "/repos/alice/demo/zipball/main":
            return httpx.Response(302, headers={"Location": "https://codeload.github.com/signed-zip-url"})

        if request.method == "GET" and request.url.path == "/repos/alice/demo/git/ref/heads/feat":
            return httpx.Response(200, json={"object": {"sha": "commit_base"}})
        if request.method == "GET" and request.url.path == "/repos/alice/demo/git/commits/commit_base":
            return httpx.Response(200, json={"tree": {"sha": "tree_base"}})
        if request.method == "GET" and request.url.path == "/repos/alice/demo/git/trees/tree_base":
            return httpx.Response(200, json={"truncated": False, "tree": []})
        if request.method == "POST" and request.url.path == "/repos/alice/demo/git/blobs":
            return httpx.Response(201, json={"sha": f"blob_{len(body['content'])}"})
        if request.method == "POST" and request.url.path == "/repos/alice/demo/git/trees":
            assert body["base_tree"] == "tree_base"
            assert len(body["tree"]) == 2
            return httpx.Response(201, json={"sha": "tree_new"})
        if request.method == "POST" and request.url.path == "/repos/alice/demo/git/commits":
            assert body["tree"] == "tree_new"
            assert body["parents"] == ["commit_base"]
            return httpx.Response(
                201, json={"sha": "commit_new", "html_url": "https://github.com/alice/demo/commit/commit_new"}
            )
        if request.method == "PATCH" and request.url.path == "/repos/alice/demo/git/refs/heads/feat":
            assert body["sha"] == "commit_new"
            return httpx.Response(200, json={"object": {"sha": "commit_new"}})

        return httpx.Response(404, json={"message": "Not Found"})

    github_personal.set_test_transport(httpx.MockTransport(gh_handler))
    try:
        async with Client(github_personal.mcp) as client:
            repos = _parse_tool_payload(await client.call_tool("gh_list_repos", {}))
            assert repos["count"] == 1
            assert repos["repos"][0]["full_name"] == "alice/demo"

            tree = _parse_tool_payload(await client.call_tool("gh_get_repo_tree", {"owner": "alice", "repo": "demo"}))
            assert tree["tree_truncated_by_github"] is True
            assert [e["path"] for e in tree["entries"]] == ["src/app.py"]

            dir_res = _parse_tool_payload(
                await client.call_tool("gh_get_file_contents", {"owner": "alice", "repo": "demo", "path": "src"})
            )
            assert dir_res["type"] == "directory"

            big_res = _parse_tool_payload(
                await client.call_tool("gh_get_file_contents", {"owner": "alice", "repo": "demo", "path": "big.txt"})
            )
            assert big_res["type"] == "file"
            assert "1: line1" in big_res["content"]

            bin_res = _parse_tool_payload(
                await client.call_tool("gh_get_file_contents", {"owner": "alice", "repo": "demo", "path": "bin.dat"})
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
                        "owner": "alice",
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
                        "arguments": {"owner": "alice", "repo": "demo"},
                    },
                )
            )
            assert dl["direct_download_url"] == "https://codeload.github.com/signed-zip-url"
    finally:
        github_personal.set_test_transport(None)


@pytest.mark.asyncio
async def test_github_coding_workflow_features(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_PAT", "github_pat_test")
    monkeypatch.setenv("GITHUB_DEFAULT_OWNER", "alice")
    monkeypatch.setenv("GITHUB_ALLOWED_WRITE_OWNERS", "alice")
    monkeypatch.setenv("GITHUB_API_BASE_URL", "https://api.github.com")
    monkeypatch.setenv("DEFAULT_FILE_LINES", "50")
    monkeypatch.setenv("DEFAULT_FILE_CHARS", "32000")
    monkeypatch.setenv("DEFAULT_DIFF_CHARS", "15000")

    numbered_js = "\n".join(
        [
            *(f"const line_{i} = {i};" for i in range(1, 70)),
            "function targetHandler(item) {",
            "  return item.id;",
            "}",
            *(f"const tail_{i} = {i};" for i in range(73, 140)),
            "function targetHandlerSecond() { return true; }",
        ]
    )
    minified_js = "var a=1;" * 500
    script_sh = "#!/usr/bin/env bash\necho start\nPORT=8080\necho middle\nRETRY=3\necho done\n"
    crlf_txt = "alpha\r\nbeta\r\ngamma\r\ndelta\r\n"
    dup_txt = "item()\nitem()\n"
    large_60k_txt = "x" * 60_000
    shrink_25k_txt = ("const row = 1;\n" * 1670)[:25_000]
    bad_utf8_bytes = b"\xff\xfeinvalid_utf8_content"
    bin_bytes = b"\x89PNG\r\n\x1a\n\x00\x00"

    long_log = ("INFO running step...\n" * 400) + "ERROR: AssertionError in test_app.py:42\nEXIT CODE 1\n"

    uploaded_blobs: list[str] = []
    created_trees: list[dict[str, Any]] = []
    put_requests: list[dict[str, Any]] = []
    commit_counter = 0

    def gh_handler(request: httpx.Request) -> httpx.Response:
        nonlocal commit_counter
        if request.url.host == "pipelines.actions.githubusercontent.com":
            assert "authorization" not in {k.lower() for k in request.headers.keys()}
            return httpx.Response(200, content=long_log.encode("utf-8"))

        body = json.loads(request.content.decode("utf-8")) if request.content else None
        path = request.url.path

        if request.method == "GET" and path == "/repos/alice/app/contents/src/app.js":
            return httpx.Response(
                200,
                json={
                    "path": "src/app.js",
                    "sha": "sha_app_js",
                    "encoding": "base64",
                    "content": base64.b64encode(numbered_js.encode("utf-8")).decode("ascii"),
                    "html_url": "https://github.com/alice/app/blob/main/src/app.js",
                },
            )
        if request.method == "GET" and path == "/repos/alice/app/contents/src/min.js":
            return httpx.Response(
                200,
                json={
                    "path": "src/min.js",
                    "sha": "sha_min_js",
                    "encoding": "base64",
                    "content": base64.b64encode(minified_js.encode("utf-8")).decode("ascii"),
                    "html_url": "https://github.com/alice/app/blob/main/src/min.js",
                },
            )
        if request.method == "GET" and path == "/search/code":
            assert request.headers.get("Accept") == "application/vnd.github.text-match+json"
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "items": [
                        {
                            "repository": {"full_name": "alice/app"},
                            "path": "src/app.js",
                            "html_url": "https://github.com/alice/app/blob/main/src/app.js",
                            "text_matches": [{"fragment": "function targetHandler(item) {\n  return item.id;\n}"}],
                        }
                    ],
                },
            )

        if request.method == "GET" and path == "/repos/alice/app/git/ref/heads/feat":
            return httpx.Response(200, json={"object": {"sha": "head_sha_1"}})
        if request.method == "GET" and path == "/repos/alice/app/git/commits/head_sha_1":
            return httpx.Response(200, json={"tree": {"sha": "tree_sha_1"}})
        if request.method == "GET" and path == "/repos/alice/app/git/trees/tree_sha_1":
            return httpx.Response(
                200,
                json={
                    "truncated": False,
                    "tree": [
                        {"path": "scripts/deploy.sh", "type": "blob", "mode": "100755", "size": len(script_sh)},
                        {"path": "docs/win.txt", "type": "blob", "mode": "100644", "size": len(crlf_txt)},
                        {"path": "src/dup.txt", "type": "blob", "mode": "100644", "size": len(dup_txt)},
                        {"path": "src/large.js", "type": "blob", "mode": "100644", "size": len(large_60k_txt)},
                        {"path": "src/old.js", "type": "blob", "mode": "100644", "size": 120},
                    ],
                },
            )

        contents_map: dict[str, tuple[bytes, str]] = {
            "scripts/deploy.sh": (script_sh.encode("utf-8"), "sha_sh"),
            "docs/win.txt": (crlf_txt.encode("utf-8"), "sha_win"),
            "src/dup.txt": (dup_txt.encode("utf-8"), "sha_dup"),
            "src/bin.dat": (bin_bytes, "sha_bin"),
            "src/bad_utf8.bin": (bad_utf8_bytes, "sha_bad_utf8"),
            "src/large.js": (large_60k_txt.encode("utf-8"), "sha_large"),
            "src/shrink.js": (shrink_25k_txt.encode("utf-8"), "sha_shrink"),
        }
        if request.method == "GET" and path.startswith("/repos/alice/app/contents/"):
            rel = path.removeprefix("/repos/alice/app/contents/")
            if rel in contents_map:
                raw_b, sha_val = contents_map[rel]
                return httpx.Response(
                    200,
                    json={
                        "path": rel,
                        "sha": sha_val,
                        "size": len(raw_b),
                        "encoding": "base64",
                        "content": base64.b64encode(raw_b).decode("ascii"),
                        "html_url": f"https://github.com/alice/app/blob/feat/{rel}",
                    },
                )
            return httpx.Response(404, json={"message": "Not Found"})

        if request.method == "PUT" and path.startswith("/repos/alice/app/contents/"):
            put_requests.append({"path": path, "body": body})
            rel = path.removeprefix("/repos/alice/app/contents/")
            return httpx.Response(
                200,
                json={
                    "content": {"path": rel, "html_url": f"https://github.com/alice/app/blob/feat/{rel}"},
                    "commit": {"sha": "put_commit_1"},
                },
            )

        if request.method == "POST" and path == "/repos/alice/app/git/blobs":
            uploaded_blobs.append(body["content"])
            return httpx.Response(201, json={"sha": f"blob_{len(uploaded_blobs)}"})
        if request.method == "POST" and path == "/repos/alice/app/git/trees":
            created_trees.append(body)
            return httpx.Response(201, json={"sha": f"tree_{len(created_trees)}"})
        if request.method == "POST" and path == "/repos/alice/app/git/commits":
            commit_counter += 1
            return httpx.Response(
                201,
                json={
                    "sha": f"commit_{commit_counter}",
                    "html_url": f"https://github.com/alice/app/commit/commit_{commit_counter}",
                },
            )
        if request.method == "PATCH" and path == "/repos/alice/app/git/refs/heads/feat":
            return httpx.Response(200, json={"object": {"sha": body["sha"]}})

        if request.method == "GET" and path == "/repos/alice/app/actions/runs/9001/jobs":
            return httpx.Response(
                200,
                json={
                    "total_count": 2,
                    "jobs": [
                        {
                            "id": 41,
                            "name": "lint",
                            "status": "completed",
                            "conclusion": "success",
                            "html_url": "https://github.com/alice/app/actions/runs/9001/job/41",
                            "steps": [{"number": 1, "name": "ruff", "status": "completed", "conclusion": "success"}],
                        },
                        {
                            "id": 42,
                            "name": "pytest",
                            "status": "completed",
                            "conclusion": "failure",
                            "html_url": "https://github.com/alice/app/actions/runs/9001/job/42",
                            "steps": [
                                {"number": 1, "name": "run tests", "status": "completed", "conclusion": "failure"}
                            ],
                        },
                    ],
                },
            )
        if request.method == "GET" and path == "/repos/alice/app/actions/jobs/42/logs":
            assert request.headers.get("Authorization") == "Bearer github_pat_test"
            return httpx.Response(
                302,
                headers={"Location": "https://pipelines.actions.githubusercontent.com/logs/job_42?sig=abc"},
            )

        if request.method == "GET" and path == "/repos/alice/app/pulls/7":
            return httpx.Response(
                200,
                json={
                    "number": 7,
                    "title": "Fix handler",
                    "state": "open",
                    "merged": False,
                    "head": {"ref": "feat"},
                    "base": {"ref": "main"},
                    "user": {"login": "alice"},
                    "body": "PR description",
                    "additions": 3,
                    "deletions": 1,
                    "changed_files": 1,
                    "html_url": "https://github.com/alice/app/pull/7",
                },
            )
        if request.method == "GET" and path == "/repos/alice/app/pulls/7/files":
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": "src/app.js",
                        "status": "modified",
                        "additions": 3,
                        "deletions": 1,
                        "patch": "@@ -1 +1 @@",
                    }
                ],
            )
        if request.method == "GET" and path == "/repos/alice/app/pulls/7/reviews":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 101,
                        "user": {"login": "reviewer"},
                        "state": "CHANGES_REQUESTED",
                        "submitted_at": "2026-11-26T00:00:00Z",
                        "body": "Please rename helper.",
                    }
                ],
            )
        if request.method == "GET" and path == "/repos/alice/app/pulls/7/comments":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 201,
                        "user": {"login": "reviewer"},
                        "path": "src/app.js",
                        "line": 71,
                        "side": "RIGHT",
                        "in_reply_to_id": None,
                        "diff_hunk": "@@ -70,3 +70,3 @@",
                        "body": "Guard against null item here.",
                        "created_at": "2026-11-26T00:01:00Z",
                    }
                ],
            )
        if request.method == "GET" and path == "/repos/alice/app/issues/7/comments":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 301,
                        "user": {"login": "alice"},
                        "created_at": "2026-11-26T00:02:00Z",
                        "body": "Will address in next commit.",
                    }
                ],
            )

        return httpx.Response(404, json={"message": "Not Found"})

    github_personal.set_test_transport(httpx.MockTransport(gh_handler))
    try:
        async with Client(github_personal.mcp) as client:
            # 1) gh_get_file_contents: find_keyword hit, jump after start_line, miss, and max_chars cap
            kw_hit = _parse_tool_payload(
                await client.call_tool(
                    "gh_get_file_contents",
                    {"owner": "alice", "repo": "app", "path": "src/app.js", "find_keyword": "targetHandler"},
                )
            )
            assert kw_hit["keyword_search"]["matched"] is True
            assert kw_hit["keyword_search"]["first_match_line"] == 70
            assert kw_hit["keyword_search"]["total_matching_lines"] == 2
            assert kw_hit["start_line"] == 50
            assert "70: function targetHandler(item)" in kw_hit["content"]

            kw_jump = _parse_tool_payload(
                await client.call_tool(
                    "gh_get_file_contents",
                    {
                        "owner": "alice",
                        "repo": "app",
                        "path": "src/app.js",
                        "start_line": 75,
                        "find_keyword": "targetHandler",
                    },
                )
            )
            assert kw_jump["keyword_search"]["first_match_line"] == 140
            assert "140: function targetHandlerSecond()" in kw_jump["content"]

            kw_miss = _parse_tool_payload(
                await client.call_tool(
                    "gh_get_file_contents",
                    {"owner": "alice", "repo": "app", "path": "src/app.js", "find_keyword": "nonexistentToken"},
                )
            )
            assert kw_miss["keyword_search"]["matched"] is False
            assert kw_miss["content"] == ""

            min_sliced = _parse_tool_payload(
                await client.call_tool(
                    "gh_get_file_contents",
                    {"owner": "alice", "repo": "app", "path": "src/min.js", "max_chars": 1000},
                )
            )
            assert min_sliced["truncated"] is True
            assert min_sliced["truncated_by_chars"] is True
            assert "LINE TRUNCATED" in min_sliced["content"]

            # 2) gh_search_code returns text-match fragments
            search_res = _parse_tool_payload(
                await client.call_tool("gh_search_code", {"query": "targetHandler", "repo": "alice/app"})
            )
            assert search_res["items"][0]["matches"] == ["function targetHandler(item) {\n  return item.id;\n}"]

            # 3) gh_patch_files: dry_run makes 0 write calls
            dry = _parse_tool_payload(
                await client.call_tool(
                    "gh_patch_files",
                    {
                        "owner": "alice",
                        "repo": "app",
                        "branch": "feat",
                        "file_paths": ["scripts/deploy.sh"],
                        "old_strings": ["PORT=8080"],
                        "new_strings": ["PORT=9090"],
                        "message": "dry run",
                        "dry_run": True,
                    },
                )
            )
            assert dry["dry_run"] is True
            assert "-PORT=8080" in dry["diff_preview"]
            assert "+PORT=9090" in dry["diff_preview"]
            assert len(uploaded_blobs) == 0

            # 4) gh_patch_files: multi-edit single file + CRLF file in 1 atomic commit, preserving mode & CRLF
            patched = _parse_tool_payload(
                await client.call_tool(
                    "gh_patch_files",
                    {
                        "owner": "alice",
                        "repo": "app",
                        "branch": "feat",
                        "file_paths": ["scripts/deploy.sh", "scripts/deploy.sh", "docs/win.txt"],
                        "old_strings": ["PORT=8080", "RETRY=3", "beta"],
                        "new_strings": ["PORT=9090", "RETRY=5", "BETA_UPDATED"],
                        "message": "patch script and crlf doc",
                        "expected_head_sha": "head_sha_1",
                    },
                )
            )
            assert patched["dry_run"] is False
            assert patched["commit_sha"] == "commit_1"
            assert patched["files_updated"] == ["scripts/deploy.sh", "docs/win.txt"]
            assert patched["files_summary"][0]["edits_applied"] == 2
            assert patched["files_summary"][0]["mode"] == "100755"
            assert patched["files_summary"][1]["edits_applied"] == 1
            assert "\r\n" in uploaded_blobs[-1]
            assert "BETA_UPDATED\r\n" in uploaded_blobs[-1]
            assert created_trees[-1]["tree"][0]["mode"] == "100755"

            # 5) gh_patch_files error guards
            sha_mismatch = _parse_tool_payload(
                await client.call_tool(
                    "gh_patch_files",
                    {
                        "owner": "alice",
                        "repo": "app",
                        "branch": "feat",
                        "file_paths": ["scripts/deploy.sh"],
                        "old_strings": ["PORT=8080"],
                        "new_strings": ["PORT=9090"],
                        "message": "bad sha",
                        "expected_head_sha": "stale_sha",
                    },
                )
            )
            assert sha_mismatch["isError"] is True
            assert "head SHA mismatch" in sha_mismatch["error"]

            zero_match = _parse_tool_payload(
                await client.call_tool(
                    "gh_patch_files",
                    {
                        "owner": "alice",
                        "repo": "app",
                        "branch": "feat",
                        "file_paths": ["scripts/deploy.sh"],
                        "old_strings": ["3: PORT=8080"],
                        "new_strings": ["PORT=9090"],
                        "message": "bad prefix",
                    },
                )
            )
            assert zero_match["isError"] is True
            assert "line-number prefixes" in zero_match["error"]

            ambig = _parse_tool_payload(
                await client.call_tool(
                    "gh_patch_files",
                    {
                        "owner": "alice",
                        "repo": "app",
                        "branch": "feat",
                        "file_paths": ["src/dup.txt"],
                        "old_strings": ["item()"],
                        "new_strings": ["other()"],
                        "message": "ambiguous",
                    },
                )
            )
            assert ambig["isError"] is True
            assert "matched 2 locations" in ambig["error"]

            for bad_path, expected_msg in (
                ("src/bin.dat", "binary file"),
                ("src/bad_utf8.bin", "not valid UTF-8"),
            ):
                err_res = _parse_tool_payload(
                    await client.call_tool(
                        "gh_patch_files",
                        {
                            "owner": "alice",
                            "repo": "app",
                            "branch": "feat",
                            "file_paths": [bad_path],
                            "old_strings": ["invalid"],
                            "new_strings": ["fixed"],
                            "message": "fail",
                        },
                    )
                )
                assert err_res["isError"] is True
                assert expected_msg in err_res["error"]

            owner_blocked = _parse_tool_payload(
                await client.call_tool(
                    "gh_patch_files",
                    {
                        "owner": "bob",
                        "repo": "app",
                        "branch": "feat",
                        "file_paths": ["scripts/deploy.sh"],
                        "old_strings": ["PORT=8080"],
                        "new_strings": ["PORT=9090"],
                        "message": "blocked",
                    },
                )
            )
            assert owner_blocked["isError"] is True
            assert "Write blocked" in owner_blocked["error"]

            # 6) Overwrite guardrails on gh_create_or_update_file & gh_push_multiple_files
            blk_50k = _parse_tool_payload(
                await client.call_tool(
                    "gh_create_or_update_file",
                    {
                        "owner": "alice",
                        "repo": "app",
                        "path": "src/large.js",
                        "content": large_60k_txt,
                        "message": "overwrite large",
                        "branch": "feat",
                    },
                )
            )
            assert blk_50k["isError"] is True
            assert "gh_patch_files" in blk_50k["error"]

            blk_shrink = _parse_tool_payload(
                await client.call_tool(
                    "gh_create_or_update_file",
                    {
                        "owner": "alice",
                        "repo": "app",
                        "path": "src/shrink.js",
                        "content": "const x = 1;\n" * 100,
                        "message": "shrunk",
                        "branch": "feat",
                    },
                )
            )
            assert blk_shrink["isError"] is True
            assert "shrinks file excessively" in blk_shrink["error"]
            assert len(put_requests) == 0

            allow_put = _parse_tool_payload(
                await client.call_tool(
                    "gh_create_or_update_file",
                    {
                        "owner": "alice",
                        "repo": "app",
                        "path": "src/large.js",
                        "content": "replaced",
                        "message": "force replace",
                        "branch": "feat",
                        "allow_large_overwrite": True,
                    },
                )
            )
            assert allow_put["commit_sha"] == "put_commit_1"
            assert len(put_requests) == 1

            blk_push = _parse_tool_payload(
                await client.call_tool(
                    "gh_push_multiple_files",
                    {
                        "owner": "alice",
                        "repo": "app",
                        "branch": "feat",
                        "file_paths": ["src/large.js"],
                        "file_contents": ["short"],
                        "message": "overwrite large via push",
                    },
                )
            )
            assert blk_push["isError"] is True
            assert "gh_patch_files" in blk_push["error"]

            allow_push = _parse_tool_payload(
                await client.call_tool(
                    "gh_push_multiple_files",
                    {
                        "owner": "alice",
                        "repo": "app",
                        "branch": "feat",
                        "file_paths": ["src/large.js"],
                        "file_contents": ["short"],
                        "message": "allow overwrite large via push",
                        "allow_large_overwrite": True,
                    },
                )
            )
            assert allow_push["commit_sha"] == "commit_2"

            # 7) gh_push_multiple_files with delete_paths (create + delete, delete-only, error cases)
            atomic_rename = _parse_tool_payload(
                await client.call_tool(
                    "gh_push_multiple_files",
                    {
                        "owner": "alice",
                        "repo": "app",
                        "branch": "feat",
                        "file_paths": ["src/new.js"],
                        "file_contents": ["export const ok = true;\n"],
                        "delete_paths": ["src/old.js"],
                        "message": "rename old.js to new.js",
                    },
                )
            )
            assert atomic_rename["files_updated"] == ["src/new.js"]
            assert atomic_rename["files_deleted"] == ["src/old.js"]
            assert {"path": "src/old.js", "mode": "100644", "type": "blob", "sha": None} in created_trees[-1]["tree"]

            del_only = _parse_tool_payload(
                await client.call_tool(
                    "gh_push_multiple_files",
                    {
                        "owner": "alice",
                        "repo": "app",
                        "branch": "feat",
                        "file_paths": [],
                        "file_contents": [],
                        "delete_paths": ["src/old.js"],
                        "message": "delete old.js",
                    },
                )
            )
            assert del_only["files_updated"] == []
            assert del_only["files_deleted"] == ["src/old.js"]

            del_missing = _parse_tool_payload(
                await client.call_tool(
                    "gh_push_multiple_files",
                    {
                        "owner": "alice",
                        "repo": "app",
                        "branch": "feat",
                        "file_paths": [],
                        "file_contents": [],
                        "delete_paths": ["src/missing.js"],
                        "message": "delete missing",
                    },
                )
            )
            assert del_missing["isError"] is True
            assert "does not exist" in del_missing["error"]

            del_overlap = _parse_tool_payload(
                await client.call_tool(
                    "gh_push_multiple_files",
                    {
                        "owner": "alice",
                        "repo": "app",
                        "branch": "feat",
                        "file_paths": ["src/old.js"],
                        "file_contents": ["x"],
                        "delete_paths": ["src/old.js"],
                        "message": "overlap",
                    },
                )
            )
            assert del_overlap["isError"] is True
            assert "both file_paths and delete_paths" in del_overlap["error"]

            # 8) gh_get_workflow_run_jobs: 302 redirect without Auth header + tail log preservation
            jobs_res = _parse_tool_payload(
                await client.call_tool(
                    "call_tool",
                    {
                        "name": "gh_get_workflow_run_jobs",
                        "arguments": {"owner": "alice", "repo": "app", "run_id": 9001, "max_log_chars": 1200},
                    },
                )
            )
            assert jobs_res["total_jobs"] == 2
            assert jobs_res["failed_jobs_count"] == 1
            failed_job = jobs_res["jobs"][1]
            assert "log_tail" in failed_job
            assert "AssertionError in test_app.py:42" in failed_job["log_tail"]
            assert "EXIT CODE 1" in failed_job["log_tail"]
            assert "log_tail" not in jobs_res["jobs"][0]

            # 9) gh_get_pull_request_detail: includes reviews, inline review comments, and issue comments
            pr_res = _parse_tool_payload(
                await client.call_tool(
                    "call_tool",
                    {
                        "name": "gh_get_pull_request_detail",
                        "arguments": {"owner": "alice", "repo": "app", "pr_number": 7},
                    },
                )
            )
            assert pr_res["reviews"][0]["state"] == "CHANGES_REQUESTED"
            assert pr_res["review_comments"][0]["path"] == "src/app.js"
            assert pr_res["review_comments"][0]["line"] == 71
            assert pr_res["issue_comments"][0]["body"] == "Will address in next commit."
    finally:
        github_personal.set_test_transport(None)


@pytest.mark.asyncio
async def test_stateless_streamable_http_smoke() -> None:
    for server_mod, expected_count in ((notes_web, 8), (github_personal, 13)):
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
