from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DISALLOWED_GLOBAL = ("etalib", "gtiit", "racknerd", "9898f37d", "tglchen")
DISALLOWED_PRIVATE_HANDLE_DIRS = ("servers", "tests", "deploy/systemd", "deploy/nginx", "deploy/caddy", "docs")


def test_markdown_relative_links_resolve() -> None:
    md_files = [ROOT / "README.md", ROOT / "README_zh_CN.md", *sorted((ROOT / "docs").glob("*.md"))]
    link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    for md_path in md_files:
        assert md_path.exists(), f"Missing documentation file: {md_path}"
        content = md_path.read_text(encoding="utf-8")
        for target in link_pattern.findall(content):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            rel = target.split("#", 1)[0]
            resolved = (md_path.parent / rel).resolve()
            assert resolved.exists(), f"Broken relative link in {md_path.relative_to(ROOT)}: {target}"


def test_shell_and_deploy_files_use_lf() -> None:
    lf_targets = [
        ROOT / "deploy" / "deploy.sh",
        *sorted((ROOT / "deploy" / "systemd").glob("*")),
        *sorted((ROOT / "deploy" / "nginx").glob("*")),
        *sorted((ROOT / "deploy" / "caddy").glob("*")),
        *sorted((ROOT / ".github" / "workflows").glob("*.yml")),
    ]
    for path in lf_targets:
        assert b"\r" not in path.read_bytes(), f"CRLF detected in {path.relative_to(ROOT)}"


def test_no_personal_identifiers_leaked() -> None:
    self_path = Path(__file__).resolve()
    scan_roots = [
        ROOT / "README.md",
        ROOT / "README_zh_CN.md",
        ROOT / ".env.example",
        *sorted((ROOT / "common").glob("*.py")),
        *sorted((ROOT / "servers").glob("*.py")),
        *sorted((ROOT / "tests").glob("*.py")),
        *sorted((ROOT / "docs").glob("*.md")),
        *sorted((ROOT / "deploy").rglob("*")),
    ]
    for path in scan_roots:
        if path.is_dir() or path.resolve() == self_path:
            continue
        lower = path.read_text(encoding="utf-8").lower()
        for token in DISALLOWED_GLOBAL:
            assert token not in lower, f"Personal identifier {token!r} found in {path.relative_to(ROOT)}"

    for rel_dir in DISALLOWED_PRIVATE_HANDLE_DIRS:
        base = ROOT / rel_dir
        for path in base.rglob("*"):
            if path.is_dir() or path.resolve() == self_path:
                continue
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            lower = path.read_text(encoding="utf-8").lower()
            assert "chen017" not in lower, f"Personal GitHub handle found in {path.relative_to(ROOT)}"
