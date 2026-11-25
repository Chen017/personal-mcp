from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections import OrderedDict
from typing import Any, Literal
from weakref import WeakKeyDictionary

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.transforms.search import BM25SearchTransform
from fastmcp.server.transforms.search.base import serialize_tools_for_output_markdown

from common.utils import format_http_error, truncate_text

load_dotenv()

SERVER_INSTRUCTIONS = """
Personal notes and web-reading MCP hub combining:
1. Etalib Notes (note.etalib.space): read-only access to personal SiYuan shared notes. ONLY invoke notes_* tools when the user explicitly provides a note.etalib.space/etalib.space URL OR explicitly asks to check/search their personal notes; for general knowledge or conceptual questions without explicit mention of notes, answer directly using general knowledge without calling notes_* tools.
2. Firecrawl Web Tools: fallback web scraping/crawling for inaccessible URLs (including www.etalib.space, JS-rendered pages, remote PDFs, and GOOGLE_EXTENDED_OPT_OUT sites).
   - `web_scrape` and `web_scrape_pdf` cache full extracted Markdown locally in memory (TTL ~30 min) and support `offset` pagination + `find_keyword` jumping at ZERO extra Firecrawl credits.
   - Multi-page tools (`web_crawl`, `web_batch_scrape`, `web_extract_structured`) enforce soft confirmation limits to prevent accidental credit burn; if a call returns `needs_confirmation=True`, ask the user and retry with `confirm_large=True` (or use `web_map` + `web_scrape`).

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

READ_ONLY_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
}

mcp = FastMCP(
    name="Etalib Web",
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

_loop_clients: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, tuple[str, httpx.AsyncClient]]] = (
    WeakKeyDictionary()
)
_test_transports: dict[str, httpx.AsyncBaseTransport | None] = {
    "notes": None,
    "firecrawl": None,
}
_web_cache: OrderedDict[tuple[str, str, str], tuple[float, dict[str, Any]]] = OrderedDict()


def clear_web_cache() -> None:
    """Clear the in-memory Firecrawl markdown cache."""
    _web_cache.clear()


def set_test_transports(
    *,
    notes: httpx.AsyncBaseTransport | None = None,
    firecrawl: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Test hook to inject mock transports without opening real sockets."""
    _test_transports["notes"] = notes
    _test_transports["firecrawl"] = firecrawl
    _loop_clients.clear()
    clear_web_cache()


def _default_note_chars() -> int:
    return int(os.getenv("DEFAULT_NOTE_CHARS", "16000"))


def _default_web_chars() -> int:
    return int(os.getenv("DEFAULT_WEB_CHARS", "16000"))


def _max_single_read_chars() -> int:
    return max(2000, int(os.getenv("MAX_SINGLE_READ_CHARS", "32000")))


def _web_cache_ttl() -> int:
    return max(60, int(os.getenv("WEB_CACHE_TTL_SECONDS", "1800")))


def _web_cache_max_entries() -> int:
    return max(4, int(os.getenv("WEB_CACHE_MAX_ENTRIES", "32")))


def _crawl_soft_max_pages() -> int:
    return max(1, int(os.getenv("CRAWL_SOFT_MAX_PAGES", "20")))


def _batch_soft_max_urls() -> int:
    return max(1, int(os.getenv("BATCH_SOFT_MAX_URLS", "15")))


def _extract_soft_max_urls() -> int:
    return max(1, int(os.getenv("EXTRACT_SOFT_MAX_URLS", "10")))


def _get_cached_web(kind: str, url: str, variant: str) -> dict[str, Any] | None:
    key = (kind, url, variant)
    entry = _web_cache.get(key)
    if entry is None:
        return None
    cached_at, payload = entry
    if (time.monotonic() - cached_at) > _web_cache_ttl():
        _web_cache.pop(key, None)
        return None
    _web_cache.move_to_end(key)
    return payload


def _set_cached_web(kind: str, url: str, variant: str, markdown: str, metadata: dict[str, Any]) -> None:
    key = (kind, url, variant)
    _web_cache[key] = (time.monotonic(), {"markdown": markdown, "metadata": metadata})
    _web_cache.move_to_end(key)
    max_entries = _web_cache_max_entries()
    while len(_web_cache) > max_entries:
        _web_cache.popitem(last=False)


def _cache_crawl_pages(items: list[dict[str, Any]]) -> None:
    for item in items:
        if not isinstance(item, dict):
            continue
        md = item.get("markdown")
        if not isinstance(md, str) or not md:
            continue
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        page_url = ""
        for candidate in (meta.get("sourceURL"), meta.get("url"), item.get("url")):
            if isinstance(candidate, str) and candidate.strip():
                page_url = candidate.strip()
                break
        if page_url:
            _set_cached_web("scrape", page_url, "main=True", md, meta)


def _clamp_single_read_chars(requested: int | None, default_chars: int | None = None) -> int:
    ceiling = _max_single_read_chars()
    base = default_chars if default_chars is not None else _default_web_chars()
    val = requested if (requested is not None and requested > 0) else base
    return max(1000, min(val, ceiling))


def _slice_cached_markdown(
    full_md: str,
    *,
    offset: int = 0,
    max_chars: int | None = None,
    find_keyword: str = "",
) -> dict[str, Any]:
    total = len(full_md)
    window = _clamp_single_read_chars(max_chars)
    start = max(0, min(offset, total))
    keyword = find_keyword.strip()
    keyword_info: dict[str, Any] | None = None

    if keyword:
        lower_md = full_md.lower()
        lower_kw = keyword.lower()
        total_matches = lower_md.count(lower_kw)
        match_idx = lower_md.find(lower_kw, start)
        if match_idx == -1:
            keyword_info = {
                "keyword": keyword,
                "matched": False,
                "match_offset": None,
                "total_matches": total_matches,
                "note": f"Keyword '{keyword}' not found at or after offset {start}.",
            }
        else:
            start = max(0, match_idx - 300)
            keyword_info = {
                "keyword": keyword,
                "matched": True,
                "match_offset": match_idx,
                "total_matches": total_matches,
            }

    end = min(total, start + window)
    chunk = full_md[start:end]
    next_offset = end if end < total else None
    truncated = next_offset is not None

    hint = ""
    if truncated:
        hint = (
            f"Showing chars {start:,}..{end:,} of {total:,} (capped at <= {_max_single_read_chars():,} per read). "
            f"Full text is cached locally: call again with offset={next_offset} or find_keyword='...' "
            f"to continue reading at 0 extra Firecrawl credits."
        )

    out: dict[str, Any] = {
        "total_chars": total,
        "offset": start,
        "returned_chars": len(chunk),
        "next_offset": next_offset,
        "truncated": truncated,
        "hint": hint,
        "markdown": chunk,
    }
    if keyword_info is not None:
        out["keyword_search"] = keyword_info
    return out


def _paginate_page_items(
    items: list[dict[str, Any]],
    *,
    page_offset: int,
    page_limit: int,
    max_chars_per_page: int,
) -> dict[str, Any]:
    total_pages = len(items)
    p_start = max(0, min(page_offset, total_pages))
    p_limit = max(1, min(page_limit, 20))
    p_end = min(total_pages, p_start + p_limit)
    clamped_chars = max(500, min(max_chars_per_page, _max_single_read_chars()))

    paged: list[dict[str, Any]] = []
    for item in items[p_start:p_end]:
        if not isinstance(item, dict):
            continue
        copy = dict(item)
        if isinstance(copy.get("markdown"), str):
            copy["markdown"] = truncate_text(
                copy["markdown"],
                max_chars=clamped_chars,
                hint="Full page cached locally; call web_scrape(url, offset=...) to read the rest at 0 credits.",
            )
        paged.append(copy)

    next_page_offset = p_end if p_end < total_pages else None
    return {
        "total_pages": total_pages,
        "page_offset": p_start,
        "returned_pages": len(paged),
        "next_page_offset": next_page_offset,
        "pages": paged,
    }


def _get_notes_client() -> tuple[httpx.AsyncClient | None, str | None, str]:
    rpc_url = os.getenv("ETALIB_NOTES_RPC_URL", "http://127.0.0.1:8081/mcp").strip()
    token = os.getenv("ETALIB_NOTES_INTERNAL_TOKEN", "").strip()
    if not token:
        return None, "ETALIB_NOTES_INTERNAL_TOKEN is not configured.", rpc_url
    loop = asyncio.get_running_loop()
    bucket = _loop_clients.setdefault(loop, {})
    cache_key = f"{rpc_url}|{token}|{id(_test_transports['notes'])}"
    cached = bucket.get("notes")
    if cached and cached[0] == cache_key and not cached[1].is_closed:
        return cached[1], None, rpc_url
    client = httpx.AsyncClient(
        headers={
            "X-MCP-Internal-Token": token,
            "Content-Type": "application/json",
        },
        timeout=30.0,
        transport=_test_transports["notes"],
    )
    bucket["notes"] = (cache_key, client)
    return client, None, rpc_url


def _get_firecrawl_client() -> tuple[httpx.AsyncClient | None, str | None]:
    api_key = os.getenv("FIRECRAWL_API_KEY", "").strip()
    base_url = os.getenv("FIRECRAWL_API_BASE_URL", "https://api.firecrawl.dev/v2").strip().rstrip("/")
    if not api_key:
        return None, "FIRECRAWL_API_KEY is not configured."
    loop = asyncio.get_running_loop()
    bucket = _loop_clients.setdefault(loop, {})
    cache_key = f"{base_url}|{api_key}|{id(_test_transports['firecrawl'])}"
    cached = bucket.get("firecrawl")
    if cached and cached[0] == cache_key and not cached[1].is_closed:
        return cached[1], None
    client = httpx.AsyncClient(
        base_url=base_url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=120.0,
        transport=_test_transports["firecrawl"],
    )
    bucket["firecrawl"] = (cache_key, client)
    return client, None


async def _call_siyuan_notes(tool_name: str, arguments: dict) -> dict:
    client, err, rpc_url = _get_notes_client()
    if err or client is None:
        return {"isError": True, "error": err or "Etalib Notes client unavailable."}
    payload = {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    try:
        resp = await client.post(rpc_url, json=payload)
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        return {"isError": True, "error": format_http_error("Etalib Notes upstream", exc)}

    if "error" in body:
        return {"isError": True, "error": f"Etalib Notes RPC error: {body['error']}"}
    result = body.get("result") or {}
    if result.get("isError"):
        texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        return {"isError": True, "error": "\n".join(texts) or "Unknown notes tool error."}
    if "structuredContent" in result and result["structuredContent"] is not None:
        return result["structuredContent"]
    return {"content": result.get("content", [])}


async def _fc_request(method: str, path: str, **kwargs) -> dict:
    client, err = _get_firecrawl_client()
    if err or client is None:
        return {"isError": True, "error": err or "Firecrawl client unavailable."}
    try:
        resp = await client.request(method, path, **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else {}
    except Exception as exc:
        return {"isError": True, "error": format_http_error("Firecrawl v2", exc)}


def _extract_scrape_id(scrape_resp: dict) -> str:
    data = scrape_resp.get("data") if isinstance(scrape_resp.get("data"), dict) else {}
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    for candidate in (
        metadata.get("scrapeId"),
        metadata.get("scrape_id"),
        data.get("scrapeId"),
        data.get("scrape_id"),
        data.get("id"),
        scrape_resp.get("scrapeId"),
        scrape_resp.get("id"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


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
    max_chars: int | None = None,
) -> dict:
    """Read clean Markdown of a SiYuan shared note (with LaTeX preserved). Supports chunked reading via offset and max_chars.
    Keywords: siyuan notes read document content markdown latex offset.
    """
    effective_max = max_chars if max_chars is not None else _default_note_chars()
    clamped = max(1000, min(effective_max, 50000))
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


@mcp.tool(annotations={"title": "Scrape Webpage (Firecrawl v2 + Cache Pagination)", **READ_ONLY_ANNOTATIONS})
async def web_scrape(
    url: str,
    only_main_content: bool = True,
    offset: int = 0,
    max_chars: int | None = None,
    find_keyword: str = "",
    wait_for_ms: int = 0,
    refresh: bool = False,
) -> dict:
    """Scrape a webpage into clean Markdown and cache it locally for zero-cost `offset` pagination and `find_keyword` jumping.
    Use when built-in browsing fails or hits GOOGLE_EXTENDED_OPT_OUT.
    Keywords: firecrawl scrape webpage url read article markdown js render opt_out etalib blog offset cache.
    """
    variant = f"main={only_main_content}"
    cached_entry = None if refresh else _get_cached_web("scrape", url, variant)
    if cached_entry is not None:
        sliced = _slice_cached_markdown(
            cached_entry["markdown"],
            offset=offset,
            max_chars=max_chars,
            find_keyword=find_keyword,
        )
        return {"url": url, "cached": True, "metadata": cached_entry["metadata"], **sliced}

    payload: dict[str, Any] = {"url": url, "formats": ["markdown"], "onlyMainContent": only_main_content}
    if wait_for_ms > 0:
        payload["waitFor"] = wait_for_ms
    res = await _fc_request("POST", "/scrape", json=payload)
    if res.get("isError"):
        return res
    data = res.get("data") or {}
    full_md = data.get("markdown") or ""
    metadata = data.get("metadata") or {}
    _set_cached_web("scrape", url, variant, full_md, metadata)
    sliced = _slice_cached_markdown(full_md, offset=offset, max_chars=max_chars, find_keyword=find_keyword)
    return {"url": url, "cached": False, "metadata": metadata, **sliced}


@mcp.tool(annotations={"title": "Map Website Sub-links (Firecrawl v2)", **READ_ONLY_ANNOTATIONS})
async def web_map(url: str, search: str = "", limit: int = 50) -> dict:
    """Discover and enumerate sub-page URLs under a website before selectively scraping target pages.
    Keywords: firecrawl map sitemap links directory subpages discover urls.
    """
    payload: dict[str, Any] = {"url": url, "limit": max(1, limit)}
    if search:
        payload["search"] = search
    return await _fc_request("POST", "/map", json=payload)


@mcp.tool(annotations={"title": "Start Multi-Page Crawl (Firecrawl v2)", **READ_ONLY_ANNOTATIONS})
async def web_crawl(
    url: str,
    limit: int = 10,
    max_depth: int = 2,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    only_main_content: bool = True,
    confirm_large: bool = False,
) -> dict:
    """Start an asynchronous multi-page crawl job across a website and return its crawl_id for web_crawl_status.
    If limit exceeds the soft guardrail, returns needs_confirmation=True unless confirm_large=True.
    Keywords: firecrawl crawl multiple subpages spider site recursive.
    """
    soft_limit = _crawl_soft_max_pages()
    if limit > soft_limit and not confirm_large:
        return {
            "needs_confirmation": True,
            "requested_limit": limit,
            "soft_limit": soft_limit,
            "message": (
                f"Crawl limit {limit} exceeds soft guardrail ({soft_limit} pages). "
                f"Ask the user to confirm before calling again with confirm_large=True, "
                f"or use web_map + selective web_scrape instead."
            ),
        }
    payload: dict[str, Any] = {
        "url": url,
        "limit": max(1, limit),
        "maxDiscoveryDepth": max(0, max_depth),
        "scrapeOptions": {"formats": ["markdown"], "onlyMainContent": only_main_content},
    }
    if include_paths:
        payload["includePaths"] = include_paths
    if exclude_paths:
        payload["excludePaths"] = exclude_paths
    return await _fc_request("POST", "/crawl", json=payload)


@mcp.tool(annotations={"title": "Get Crawl Job Status & Paginated Pages (Firecrawl v2)", **READ_ONLY_ANNOTATIONS})
async def web_crawl_status(
    crawl_id: str,
    page_offset: int = 0,
    page_limit: int = 5,
    max_chars_per_page: int = 6000,
) -> dict:
    """Check progress and retrieve crawled Markdown pages with page_offset/page_limit pagination.
    Also caches each crawled page locally so web_scrape(url, offset=...) can read truncated pages at 0 extra credits.
    Keywords: firecrawl crawl status job progress results pages pagination.
    """
    res = await _fc_request("GET", f"/crawl/{crawl_id}")
    if res.get("isError"):
        return res
    out = dict(res)
    raw_pages = out.pop("data", None)
    if isinstance(raw_pages, list):
        _cache_crawl_pages(raw_pages)
        paged = _paginate_page_items(
            raw_pages,
            page_offset=page_offset,
            page_limit=page_limit,
            max_chars_per_page=max_chars_per_page,
        )
        out.update(paged)
    return out


@mcp.tool(annotations={"title": "Batch Scrape Multiple URLs (Firecrawl v2)", **READ_ONLY_ANNOTATIONS})
async def web_batch_scrape(
    urls: list[str] | None = None,
    only_main_content: bool = True,
    wait: bool = True,
    poll_interval_sec: float = 2.0,
    timeout_sec: float = 90.0,
    page_offset: int = 0,
    page_limit: int = 5,
    max_chars_per_page: int = 6000,
    batch_id: str = "",
    confirm_large: bool = False,
) -> dict:
    """Batch scrape multiple URLs in parallel or poll an existing batch_id with page_offset/page_limit pagination.
    Keywords: firecrawl batch scrape multiple urls list parallel.
    """
    if batch_id:
        res = await _fc_request("GET", f"/batch/scrape/{batch_id}")
        if res.get("isError"):
            return res
        out = dict(res)
        raw_pages = out.pop("data", None)
        if isinstance(raw_pages, list):
            _cache_crawl_pages(raw_pages)
            out.update(
                _paginate_page_items(
                    raw_pages,
                    page_offset=page_offset,
                    page_limit=page_limit,
                    max_chars_per_page=max_chars_per_page,
                )
            )
        return out

    if not urls:
        return {"isError": True, "error": "Provide either non-empty urls or an existing batch_id."}

    soft_limit = _batch_soft_max_urls()
    if len(urls) > soft_limit and not confirm_large:
        return {
            "needs_confirmation": True,
            "requested_urls": len(urls),
            "soft_limit": soft_limit,
            "message": (
                f"Batch scrape has {len(urls)} URLs, exceeding soft guardrail ({soft_limit}). "
                f"Ask the user to confirm before calling again with confirm_large=True."
            ),
        }

    start = await _fc_request(
        "POST",
        "/batch/scrape",
        json={"urls": urls, "formats": ["markdown"], "onlyMainContent": only_main_content},
    )
    if start.get("isError") or not wait:
        return start

    job_id = start.get("id")
    if not isinstance(job_id, str) or not job_id:
        return start

    deadline = time.monotonic() + max(1.0, timeout_sec)
    while time.monotonic() < deadline:
        status_res = await _fc_request("GET", f"/batch/scrape/{job_id}")
        if status_res.get("isError"):
            return status_res
        status = str(status_res.get("status") or "").lower()
        if status in {"completed", "failed", "cancelled"}:
            out = dict(status_res)
            raw_pages = out.pop("data", None)
            if isinstance(raw_pages, list):
                _cache_crawl_pages(raw_pages)
                out.update(
                    _paginate_page_items(
                        raw_pages,
                        page_offset=page_offset,
                        page_limit=page_limit,
                        max_chars_per_page=max_chars_per_page,
                    )
                )
            return out
        await asyncio.sleep(max(0.2, poll_interval_sec))

    return {
        "id": job_id,
        "status": "polling_timeout",
        "note": "Batch job is still running; call web_batch_scrape(batch_id=...) to fetch results.",
    }


@mcp.tool(annotations={"title": "Extract Structured JSON (Firecrawl v2)", **READ_ONLY_ANNOTATIONS})
async def web_extract_structured(
    urls: list[str],
    prompt: str,
    schema_json: str = "",
    wait: bool = True,
    poll_interval_sec: float = 2.0,
    timeout_sec: float = 90.0,
    confirm_large: bool = False,
) -> dict:
    """Extract structured JSON data from one or more URLs (supports wildcards) using a natural language prompt and optional JSON Schema.
    Keywords: firecrawl extract structured json schema data parse fields.
    """
    if not urls:
        return {"isError": True, "error": "urls must be a non-empty list."}
    soft_limit = _extract_soft_max_urls()
    if len(urls) > soft_limit and not confirm_large:
        return {
            "needs_confirmation": True,
            "requested_urls": len(urls),
            "soft_limit": soft_limit,
            "message": (
                f"Structured extract targets {len(urls)} URLs, exceeding soft guardrail ({soft_limit}). "
                f"Ask the user to confirm before calling again with confirm_large=True."
            ),
        }

    payload: dict[str, Any] = {"urls": urls, "prompt": prompt}
    if schema_json.strip():
        try:
            parsed_schema = json.loads(schema_json)
        except json.JSONDecodeError as exc:
            return {"isError": True, "error": f"Invalid schema_json: {exc}"}
        if not isinstance(parsed_schema, dict):
            return {"isError": True, "error": "schema_json must decode to a JSON object."}
        payload["schema"] = parsed_schema

    res = await _fc_request("POST", "/extract", json=payload)
    if res.get("isError") or not wait:
        return res

    job_id = res.get("id")
    status = str(res.get("status") or "").lower()
    if not isinstance(job_id, str) or not job_id or status == "completed" or "data" in res:
        return res

    deadline = time.monotonic() + max(1.0, timeout_sec)
    while time.monotonic() < deadline:
        poll = await _fc_request("GET", f"/extract/{job_id}")
        if poll.get("isError"):
            return poll
        poll_status = str(poll.get("status") or "").lower()
        if poll_status in {"completed", "failed", "cancelled"} or "data" in poll:
            return poll
        await asyncio.sleep(max(0.2, poll_interval_sec))

    return {
        "id": job_id,
        "status": "polling_timeout",
        "note": "Extraction job is still running on Firecrawl.",
    }


@mcp.tool(annotations={"title": "Scrape & Parse Remote PDF (Firecrawl v2 + Cache Pagination)", **READ_ONLY_ANNOTATIONS})
async def web_scrape_pdf(
    url: str,
    mode: Literal["auto", "fast", "ocr"] = "auto",
    offset: int = 0,
    max_chars: int | None = None,
    find_keyword: str = "",
    refresh: bool = False,
) -> dict:
    """Download and parse a remote PDF document into Markdown, caching full text locally for 0-credit `offset` pagination and `find_keyword` jumping.
    Keywords: firecrawl parse remote pdf document paper file extract text offset cache.
    """
    variant = f"mode={mode}"
    cached_entry = None if refresh else _get_cached_web("pdf", url, variant)
    if cached_entry is not None:
        sliced = _slice_cached_markdown(
            cached_entry["markdown"],
            offset=offset,
            max_chars=max_chars,
            find_keyword=find_keyword,
        )
        return {"url": url, "cached": True, "metadata": cached_entry["metadata"], **sliced}

    payload = {
        "url": url,
        "formats": ["markdown"],
        "parsers": [{"type": "pdf", "mode": mode}],
    }
    res = await _fc_request("POST", "/scrape", json=payload)
    if res.get("isError"):
        return res
    data = res.get("data") or {}
    full_md = data.get("markdown") or ""
    metadata = data.get("metadata") or {}
    _set_cached_web("pdf", url, variant, full_md, metadata)
    sliced = _slice_cached_markdown(full_md, offset=offset, max_chars=max_chars, find_keyword=find_keyword)
    return {"url": url, "cached": False, "metadata": metadata, **sliced}


@mcp.tool(annotations={"title": "Start Interactive Browser Session (Firecrawl v2)", **READ_ONLY_ANNOTATIONS})
async def web_interact_start(url: str, max_chars: int | None = None) -> dict:
    """Open a webpage and return its scrape_id so you can run interactive clicks/forms with web_interact_action.
    Always call web_interact_stop(scrape_id) when finished.
    Keywords: firecrawl browser interact start session open click login dynamic.
    """
    effective_max = _clamp_single_read_chars(max_chars)
    res = await _fc_request("POST", "/scrape", json={"url": url, "formats": ["markdown"]})
    if res.get("isError"):
        return res
    scrape_id = _extract_scrape_id(res)
    data = res.get("data") or {}
    md = truncate_text(data.get("markdown") or "", max_chars=effective_max)
    return {
        "url": url,
        "scrape_id": scrape_id,
        "metadata": data.get("metadata") or {},
        "markdown": md,
        "reminder": "Remember to call web_interact_stop(scrape_id) immediately when done.",
    }


@mcp.tool(
    annotations={
        "title": "Run Browser Interaction Action (Firecrawl v2)",
        "readOnlyHint": False,
        "destructiveHint": False,
    }
)
async def web_interact_action(
    scrape_id: str,
    prompt: str = "",
    code: str = "",
    language: Literal["node", "python", "bash"] = "node",
    max_chars: int | None = None,
) -> dict:
    """Execute clicks, form inputs, scrolling, or Playwright code inside an active Firecrawl interact session.
    Keywords: firecrawl click button interact browser form scroll action.
    """
    if not prompt.strip() and not code.strip():
        return {"isError": True, "error": "Provide either prompt or code for web_interact_action."}
    effective_max = _clamp_single_read_chars(max_chars)
    payload: dict[str, Any] = {}
    if prompt.strip():
        payload["prompt"] = prompt
    if code.strip():
        payload["code"] = code
        payload["language"] = language
    res = await _fc_request("POST", f"/scrape/{scrape_id}/interact", json=payload)
    if res.get("isError"):
        return res
    out = dict(res)
    for key in ("output", "markdown", "stdout", "result"):
        if isinstance(out.get(key), str):
            out[key] = truncate_text(out[key], max_chars=effective_max)
    out["reminder"] = "Remember to call web_interact_stop(scrape_id) immediately when done."
    return out


@mcp.tool(
    annotations={
        "title": "Stop Interactive Browser Session (Firecrawl v2)",
        "readOnlyHint": False,
        "destructiveHint": False,
    }
)
async def web_interact_stop(scrape_id: str) -> dict:
    """Terminate an active Firecrawl interact browser session and finalize billing.
    Keywords: firecrawl interact stop close browser session end cleanup.
    """
    return await _fc_request("DELETE", f"/scrape/{scrape_id}/interact")


@mcp.tool(annotations={"title": "Check Firecrawl Credit Usage", **READ_ONLY_ANNOTATIONS})
async def web_check_credit_usage() -> dict:
    """Check remaining Firecrawl API credits and billing cycle quota.
    Keywords: firecrawl credit usage quota balance remaining billing.
    """
    return await _fc_request("GET", "/team/credit-usage")


def create_http_app():
    return mcp.http_app(path="/mcp", stateless_http=True)


if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8101, path="/mcp", stateless_http=True)
