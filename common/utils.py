from __future__ import annotations

import httpx

IGNORED_DIRS = {
    ".git",
    "node_modules",
    "dist",
    "build",
    ".next",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "target",
}

IGNORED_FILES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "uv.lock",
    "Cargo.lock",
}


def truncate_text(text: str, max_chars: int = 16000, hint: str = "") -> str:
    """Truncate text to max_chars unless max_chars <= 0."""
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
    """Slice 1-based line ranges with line numbers prefixed."""
    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        return {
            "total_lines": 0,
            "start_line": 1,
            "end_line": 0,
            "truncated": False,
            "content": "",
        }
    s = max(1, start_line)
    if s > total:
        return {
            "total_lines": total,
            "start_line": s,
            "end_line": total,
            "truncated": False,
            "content": "",
        }
    e = min(total, end_line) if end_line is not None else min(total, s + max(1, default_max_lines) - 1)
    if e < s:
        e = s
    chunk = "\n".join(f"{idx}: {line}" for idx, line in enumerate(lines[s - 1 : e], start=s))
    return {
        "total_lines": total,
        "start_line": s,
        "end_line": e,
        "truncated": e < total,
        "content": chunk,
    }


def is_binary_bytes(data: bytes) -> bool:
    """Heuristically detect binary payload by inspecting NUL bytes in the leading 4 KiB."""
    return b"\x00" in data[:4096]


def format_http_error(service: str, exc: Exception) -> str:
    """Convert httpx and runtime errors into readable diagnostics for LLM tool consumers."""
    if isinstance(exc, httpx.HTTPStatusError):
        resp = exc.response
        code = resp.status_code
        remaining = resp.headers.get("X-RateLimit-Remaining")
        reset = resp.headers.get("X-RateLimit-Reset")
        rate_parts = []
        if remaining is not None:
            rate_parts.append(f"rate_limit_remaining={remaining}")
        if reset is not None:
            rate_parts.append(f"reset={reset}")
        rate_hint = f" ({', '.join(rate_parts)})" if rate_parts else ""
        detail = resp.text[:400].strip()
        if code == 401:
            return f"{service} HTTP 401 Unauthorized{rate_hint}: check API key/token. {detail}".strip()
        if code == 403:
            return f"{service} HTTP 403 Forbidden{rate_hint}: permission denied or rate limit reached. {detail}".strip()
        if code == 404:
            return f"{service} HTTP 404 Not Found: target resource does not exist or is inaccessible. {detail}".strip()
        if code == 422:
            return f"{service} HTTP 422 Unprocessable Entity: {detail}".strip()
        return f"{service} HTTP {code}{rate_hint}: {detail}".strip()
    if isinstance(exc, httpx.TimeoutException):
        return f"{service} request timed out: {exc}"
    if isinstance(exc, httpx.RequestError):
        return f"{service} connection error (upstream may be restarting or unreachable): {exc}"
    return f"{service} error: {exc}"
