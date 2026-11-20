#!/usr/bin/env python3
"""
doc_project.py — Universal, stack-agnostic project context packager for LLMs.

Key features:
  - Complete, untruncated file contents by default (view="full" for every file).
  - Optional token-budget truncation ONLY when explicitly requested via flags
    (--max-tokens, --max-file-tokens, or --truncate-generated).
  - Accurate token counting via tiktoken (if installed) with a tuned BPE-like
    fallback estimator, reported per file, per tier, and for the total XML.
  - Stack-agnostic project <overview> (JS/TS/React/Next/Expo, Flutter/Dart,
    Python, Go, Rust, Java/Kotlin/Android, Swift/iOS, C/C++, C#/.NET, Ruby,
    PHP, Terraform/Docker, and monorepos).
  - Git-aware context (<git_context>) and cross-stack priority tiering (Tiers 1–8).
  - Preserves all project Markdown docs and test configs/mocks while filtering
    binary files, build/cache artifacts (.dart_tool, node_modules, etc.), and lockfiles.
  - High-precision, entropy-aware secret redaction that avoids mangling key-name
    constants and test fixtures (and can be disabled with --no-redact).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple
from xml.sax.saxutils import escape as xml_escape

try:
    import tomllib  # Python 3.11+
except ImportError:
    tomllib = None  # type: ignore

try:
    import tiktoken  # Optional: exact BPE token counting
except ImportError:
    tiktoken = None  # type: ignore


# =============================================================================
# Configuration & Universal Ignore Rules
# =============================================================================

DEFAULT_IGNORED_DIRS: Set[str] = {
    ".git", ".hg", ".svn",
    ".dart_tool", ".flutter-plugins", ".pub-cache",
    "node_modules", ".npm", ".yarn", ".pnpm-store", ".turbo", ".parcel-cache",
    ".next", ".nuxt", ".svelte-kit", ".astro", ".expo", ".expo-shared",
    "dist", "build", "out", "coverage", ".nyc_output", ".cache",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    ".venv", "venv", "env", ".env", "site-packages", "*.egg-info",
    "target", "vendor", "Pods", "DerivedData", "xcuserdata",
    ".gradle", ".idea", ".vscode", ".vs", "bin", "obj",
    "tmp", "temp", ".terraform", ".serverless",
}

DEFAULT_IGNORED_FILES: Set[str] = {
    ".DS_Store", "Thumbs.db", "desktop.ini",
    ".flutter-plugins", ".flutter-plugins-dependencies", ".packages",
    ".metadata",
}

LOCKFILE_NAMES: Set[str] = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock",
    "pubspec.lock", "Cargo.lock", "poetry.lock", "Pipfile.lock", "uv.lock",
    "composer.lock", "Gemfile.lock", "Podfile.lock", "Package.resolved",
    "go.sum", "gradle.lockfile", "flake.lock", "mix.lock", "packages.lock.json",
}

BINARY_OR_NOISE_EXTENSIONS: Set[str] = {
    # Images & media
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tiff", ".tif",
    ".psd", ".ai", ".eps", ".heic", ".avif", ".icns",
    ".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac",
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv",
    # Fonts
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    # Archives & packages
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
    ".jar", ".war", ".ear", ".aar", ".apk", ".aab", ".ipa", ".dmg", ".exe", ".msi", ".deb", ".rpm",
    # Compiled / binary / DB / IDE artifacts
    ".pyc", ".pyo", ".pyd", ".so", ".dll", ".dylib", ".o", ".a", ".lib", ".obj",
    ".class", ".dex", ".wasm", ".bin", ".dat", ".unlinked2", ".dill", ".snap",
    ".sqlite", ".sqlite3", ".db", ".db-shm", ".db-wal", ".realm", ".parquet",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".iml", ".hprof", ".profraw", ".map", ".min.js", ".min.css",
    ".keystore", ".jks", ".p12", ".pfx", ".cer", ".crt", ".der",
}

ENV_ALLOWED_NAMES: Set[str] = {
    ".env.example", ".env.sample", ".env.template", ".env.dist", ".env.schema",
}

LANGUAGE_MAP: Dict[str, str] = {
    ".py": "python", ".pyi": "python", ".pyx": "cython",
    ".js": "javascript", ".jsx": "jsx", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx", ".mts": "typescript", ".cts": "typescript",
    ".dart": "dart",
    ".kt": "kotlin", ".kts": "kotlin", ".java": "java", ".groovy": "groovy",
    ".swift": "swift", ".m": "objective-c", ".mm": "objective-cpp",
    ".go": "go", ".rs": "rust",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".cs": "csharp", ".fs": "fsharp", ".vb": "vbnet",
    ".rb": "ruby", ".php": "php", ".ex": "elixir", ".exs": "elixir", ".erl": "erlang",
    ".scala": "scala", ".clj": "clojure", ".hs": "haskell", ".lua": "lua", ".r": "r", ".jl": "julia",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "scss", ".sass": "sass", ".less": "less",
    ".vue": "vue", ".svelte": "svelte", ".astro": "astro",
    ".json": "json", ".jsonc": "jsonc", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".xml": "xml", ".plist": "xml", ".arb": "json", ".ini": "ini", ".cfg": "ini", ".conf": "conf",
    ".sql": "sql", ".graphql": "graphql", ".gql": "graphql", ".proto": "protobuf",
    ".sh": "bash", ".bash": "bash", ".zsh": "zsh", ".fish": "fish", ".ps1": "powershell", ".bat": "batch",
    ".md": "markdown", ".mdx": "mdx", ".rst": "rst", ".txt": "text",
    ".tf": "hcl", ".tfvars": "hcl", ".hcl": "hcl",
    ".dockerfile": "dockerfile", ".cmake": "cmake", ".gradle": "groovy",
    ".prisma": "prisma", ".sol": "solidity",
}

SPECIAL_FILENAMES_LANG: Dict[str, str] = {
    "Dockerfile": "dockerfile",
    "Makefile": "makefile",
    "GNUmakefile": "makefile",
    "CMakeLists.txt": "cmake",
    "Jenkinsfile": "groovy",
    "Podfile": "ruby",
    "Fastfile": "ruby",
    "Appfile": "ruby",
    "Gemfile": "ruby",
    "Rakefile": "ruby",
    "Vagrantfile": "ruby",
    "Procfile": "procfile",
    "Justfile": "makefile",
    "BUILD": "starlark",
    "WORKSPACE": "starlark",
}

MANIFEST_FILENAMES: Set[str] = {
    "package.json", "pubspec.yaml", "pyproject.toml", "setup.py", "setup.cfg",
    "requirements.txt", "Pipfile", "Cargo.toml", "go.mod", "pom.xml",
    "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
    "Gemfile", "composer.json", "Package.swift", "Podfile", "CMakeLists.txt",
    "Makefile", "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "compose.yml", "compose.yaml", "app.json", "app.config.js", "app.config.ts",
    "eas.json", "turbo.json", "nx.json", "pnpm-workspace.yaml", "lerna.json",
}

ENTRY_POINT_NAMES: Set[str] = {
    "main.dart", "app.dart",
    "main.py", "app.py", "asgi.py", "wsgi.py", "manage.py", "cli.py", "__main__.py",
    "index.ts", "index.tsx", "index.js", "index.jsx", "main.ts", "main.tsx",
    "main.js", "main.jsx", "app.ts", "app.tsx", "app.js", "app.jsx",
    "App.tsx", "App.ts", "App.jsx", "App.js", "server.ts", "server.js",
    "_layout.tsx", "_layout.ts", "layout.tsx", "page.tsx",
    "main.go", "main.rs", "lib.rs",
    "Program.cs", "Startup.cs", "Application.java", "Main.java",
    "MainActivity.kt", "MainActivity.java", "AppDelegate.swift",
}

GENERATED_PATTERNS: Tuple[str, ...] = (
    "*.g.dart", "*.freezed.dart", "*.gr.dart", "*.mocks.dart",
    "app_localizations*.dart", "*.pb.go", "*.pb.dart", "*_pb2.py",
    "*.generated.ts", "*.generated.js", "*.generated.cs",
    "*.d.ts", "schema.prisma.d.ts",
)

# Strip illegal XML 1.0 control characters (keep \t, \n, \r)
_ILLEGAL_XML_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f]")


# =============================================================================
# Data Models
# =============================================================================

@dataclass
class FileRecord:
    rel_path: str
    abs_path: Path
    language: str
    tier: int
    tier_reason: str
    size_bytes: int
    loc: int
    raw_content: str
    content: str
    full_tokens: int
    is_generated: bool = False
    is_modified: bool = False
    redaction_count: int = 0
    outline: str = ""
    view: str = "full"  # "full" | "outline" | "head_tail" | "omitted"
    rendered_content: str = ""
    rendered_tokens: int = 0
    rank: int = 0


# =============================================================================
# Token Counting
# =============================================================================

class TokenCounter:
    """Uses tiktoken if available; falls back to a calibrated code-aware heuristic."""

    _TOKEN_CHUNK_RE = re.compile(
        r"""'s|'t|'re|'ve|'m|'ll|'d| ?[A-Za-z]+| ?\d+| ?[^\sA-Za-z\d]+|\s+"""
    )

    def __init__(self, encoding_name: str = "o200k_base", force_estimate: bool = False):
        self.encoding_name = encoding_name
        self._enc = None
        if tiktoken is not None and not force_estimate:
            try:
                self._enc = tiktoken.get_encoding(encoding_name)
            except Exception:
                try:
                    self._enc = tiktoken.get_encoding("cl100k_base")
                    self.encoding_name = "cl100k_base"
                except Exception:
                    self._enc = None

    @property
    def mode(self) -> str:
        if self._enc is not None:
            return f"exact (tiktoken:{self.encoding_name})"
        return "estimated (code-heuristic)"

    def count(self, text: str) -> int:
        if not text:
            return 0
        if self._enc is not None:
            return len(self._enc.encode(text, disallowed_special=()))
        # Code-aware fallback heuristic (typically within ~5-10% of o200k/cl100k)
        total = 0
        for chunk in self._TOKEN_CHUNK_RE.findall(text):
            n = len(chunk)
            if chunk.isspace():
                total += max(1, (n + 3) // 4)
            elif chunk.lstrip().isalpha():
                total += 1 if n <= 7 else math.ceil(n / 4.5)
            elif chunk.lstrip().isdigit():
                total += math.ceil(n / 3)
            else:
                total += max(1, math.ceil(n / 2.2))
        return total


# =============================================================================
# High-Precision Secret Redaction (Avoids False Positives)
# =============================================================================

_HIGH_CONFIDENCE_SECRET_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("private_key", re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----[\s\S]+?-----END (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"
    )),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA|AIDA|AROA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{32,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{20,}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{20,}\b")),
    ("sendgrid_key", re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{24,}\b")),
    ("jwt_token", re.compile(r"\beyJ[A-Za-z0-9_-]{15,}\.eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\b")),
]

_GENERIC_ASSIGN_RE = re.compile(
    r"""(?ix)
    (?P<prefix>
        (?:[A-Za-z0-9_.-]*(?:api[_-]?key|api[_-]?secret|access[_-]?token|refresh[_-]?token|
           auth[_-]?token|client[_-]?secret|private[_-]?key|secret[_-]?key|password|passwd|
           bearer|totp[_-]?secret|signing[_-]?key|webhook[_-]?secret)[A-Za-z0-9_.-]*)
        \s*[:=]\s*
        (?P<quote>["'`])
    )
    (?P<value>[^"'`\r\n]{16,256})
    (?P=quote)
    """
)

_PLACEHOLDER_HINTS = (
    "dummy", "example", "placeholder", "your_", "your-", "changeme", "replace_me",
    "test_", "mock_", "fake_", "sample", "xxxx", "todo", "redacted", "insert_",
    "enter_", "put_", "localhost", "127.0.0.1", "http://", "https://", "${", "{{",
)
_ALL_CAPS_CONST_RE = re.compile(r"^[A-Z][A-Z0-9_.:/-]+$")
_SNAKE_OR_KEBAB_LABEL_RE = re.compile(r"^[a-z]+(?:[_.-][a-z]+)+$")


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    length = len(s)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


def _looks_like_real_secret(val: str) -> bool:
    stripped = val.strip()
    if len(stripped) < 16 or " " in stripped:
        return False
    low = stripped.lower()
    if any(hint in low for hint in _PLACEHOLDER_HINTS):
        return False
    # Do not redact key-name constants like 'QUANTSAPP_API_SECRET' or 'secure_api_key'
    if _ALL_CAPS_CONST_RE.match(stripped) or _SNAKE_OR_KEBAB_LABEL_RE.match(stripped):
        return False
    # Require at least 2 character classes and solid entropy
    classes = sum([
        any(c.islower() for c in stripped),
        any(c.isupper() for c in stripped),
        any(c.isdigit() for c in stripped),
        any(not c.isalnum() for c in stripped),
    ])
    if classes < 2:
        return False
    return _shannon_entropy(stripped) >= 3.3


def redact_secrets(text: str) -> Tuple[str, int]:
    """Redacts real-looking secrets without corrupting key-name constants or dummy fixtures."""
    count = 0

    for label, pattern in _HIGH_CONFIDENCE_SECRET_PATTERNS:
        def _repl(_m: re.Match, lbl: str = label) -> str:
            nonlocal count
            count += 1
            return f"[REDACTED:{lbl}]"
        text = pattern.sub(_repl, text)

    def _generic_repl(m: re.Match) -> str:
        nonlocal count
        val = m.group("value")
        if _looks_like_real_secret(val):
            count += 1
            q = m.group("quote")
            return f"{m.group('prefix')}[REDACTED]{q}"
        return m.group(0)

    text = _GENERIC_ASSIGN_RE.sub(_generic_repl, text)
    return text, count


# =============================================================================
# Git Helpers
# =============================================================================

def _run_git(root: Path, args: Sequence[str], timeout: int = 10) -> Optional[str]:
    try:
        res = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if res.returncode == 0:
            return res.stdout
    except Exception:
        pass
    return None


def collect_git_context(root: Path, recent_commits: int = 10) -> Tuple[Dict[str, object], Set[str]]:
    """Returns git metadata dict and a set of modified/untracked relative POSIX paths."""
    is_repo = _run_git(root, ["rev-parse", "--is-inside-work-tree"])
    if not is_repo or is_repo.strip() != "true":
        return {"is_git_repo": False}, set()

    branch = (_run_git(root, ["rev-parse", "--abbrev-ref", "HEAD"]) or "").strip()
    head = (_run_git(root, ["rev-parse", "--short", "HEAD"]) or "").strip()
    status_raw = _run_git(root, ["status", "--porcelain=v1", "-uall"]) or ""
    diff_stat = (_run_git(root, ["diff", "--stat"]) or "").strip()
    staged_stat = (_run_git(root, ["diff", "--cached", "--stat"]) or "").strip()
    log_raw = (_run_git(
        root,
        ["log", f"-n{recent_commits}", "--date=short", "--pretty=format:%h | %ad | %an | %s"],
    ) or "").strip()

    modified_paths: Set[str] = set()
    status_lines: List[str] = []
    for line in status_raw.splitlines():
        if not line.strip():
            continue
        status_lines.append(line)
        # Format: XY path  OR  XY old -> new
        path_part = line[3:].strip()
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1].strip()
        path_part = path_part.strip('"')
        modified_paths.add(path_part.replace("\\", "/"))

    info = {
        "is_git_repo": True,
        "branch": branch,
        "head": head,
        "status_lines": status_lines,
        "diff_stat": diff_stat,
        "staged_stat": staged_stat,
        "recent_commits": [l for l in log_raw.splitlines() if l.strip()],
    }
    return info, modified_paths


# =============================================================================
# File Discovery & Filtering
# =============================================================================

def _is_binary_bytes(sample: bytes) -> bool:
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    # Check ratio of non-text bytes
    text_chars = bytearray({7, 8, 9, 10, 12, 13, 27} | set(range(0x20, 0x100)))
    non_text = sample.translate(None, text_chars)
    return (len(non_text) / len(sample)) > 0.30


def _detect_language(path: Path) -> str:
    name = path.name
    if name in SPECIAL_FILENAMES_LANG:
        return SPECIAL_FILENAMES_LANG[name]
    if name.lower().startswith("dockerfile"):
        return "dockerfile"
    suffix = path.suffix.lower()
    if suffix in LANGUAGE_MAP:
        return LANGUAGE_MAP[suffix]
    return "text"


def _is_generated_file(rel_posix: str, first_lines: str) -> bool:
    name = Path(rel_posix).name
    for pat in GENERATED_PATTERNS:
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel_posix, pat):
            return True
    header = first_lines[:600].lower()
    markers = (
        "generated code - do not modify",
        "do not edit by hand",
        "this file is automatically generated",
        "auto-generated",
        "autogenerated",
        "@generated",
    )
    return any(m in header for m in markers)


def _should_ignore_path(
    rel_posix: str,
    include_lockfiles: bool,
    include_env: bool,
    include_tests: bool,
    extra_excludes: Sequence[str],
    extra_includes: Sequence[str],
    output_rel: Optional[str],
) -> bool:
    if output_rel and rel_posix == output_rel:
        return True

    parts = rel_posix.split("/")
    name = parts[-1]
    lower_name = name.lower()

    # Explicit user includes override default ignores (except .git)
    if ".git" in parts:
        return True
    if extra_includes and any(fnmatch.fnmatch(rel_posix, g) or fnmatch.fnmatch(name, g) for g in extra_includes):
        return False

    # User excludes
    if extra_excludes and any(fnmatch.fnmatch(rel_posix, g) or fnmatch.fnmatch(name, g) for g in extra_excludes):
        return True

    # Directory ignores
    for part in parts[:-1]:
        if part in DEFAULT_IGNORED_DIRS or any(fnmatch.fnmatch(part, d) for d in DEFAULT_IGNORED_DIRS if "*" in d):
            return True

    # Exact file ignores
    if name in DEFAULT_IGNORED_FILES:
        return True

    # Env secrets protection
    if not include_env and (lower_name == ".env" or (lower_name.startswith(".env.") and lower_name not in ENV_ALLOWED_NAMES)):
        return True

    # Lockfiles
    if not include_lockfiles and name in LOCKFILE_NAMES:
        return True

    # Tests exclusion (only if user explicitly passed --no-tests)
    if not include_tests and _is_test_path(rel_posix):
        return True

    # Multi-part and single extensions
    for ext in (".min.js", ".min.css"):
        if lower_name.endswith(ext):
            return True
    suffix = Path(name).suffix.lower()
    if suffix in BINARY_OR_NOISE_EXTENSIONS:
        return True

    return False


def discover_files(
    root: Path,
    use_gitignore: bool,
    include_lockfiles: bool,
    include_env: bool,
    include_tests: bool,
    extra_excludes: Sequence[str],
    extra_includes: Sequence[str],
    output_path: Optional[Path],
    max_file_bytes: int,
) -> Tuple[List[Path], List[Tuple[str, str]]]:
    """Discovers candidate text files using git (when available) or filesystem walk."""
    output_rel: Optional[str] = None
    if output_path is not None:
        try:
            output_rel = output_path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            output_rel = None

    candidates: List[Path] = []
    used_git = False

    if use_gitignore:
        git_out = _run_git(root, ["ls-files", "--cached", "--others", "--exclude-standard", "-z"])
        if git_out is not None:
            used_git = True
            for entry in git_out.split("\0"):
                if not entry:
                    continue
                full = root / entry
                if full.is_file() and not full.is_symlink():
                    candidates.append(full)

    if not used_git:
        for dirpath, dirnames, filenames in os.walk(root):
            # Prune ignored directories early for speed
            dirnames[:] = [
                d for d in dirnames
                if d not in DEFAULT_IGNORED_DIRS
                and not any(fnmatch.fnmatch(d, pat) for pat in DEFAULT_IGNORED_DIRS if "*" in pat)
            ]
            for fname in filenames:
                full = Path(dirpath) / fname
                if full.is_file() and not full.is_symlink():
                    candidates.append(full)

    kept: List[Path] = []
    skipped: List[Tuple[str, str]] = []
    seen: Set[str] = set()

    for full in candidates:
        rel_posix = full.relative_to(root).as_posix()
        if rel_posix in seen:
            continue
        seen.add(rel_posix)

        if _should_ignore_path(
            rel_posix,
            include_lockfiles=include_lockfiles,
            include_env=include_env,
            include_tests=include_tests,
            extra_excludes=extra_excludes,
            extra_includes=extra_includes,
            output_rel=output_rel,
        ):
            continue

        try:
            size = full.stat().st_size
        except OSError:
            continue

        if size > max_file_bytes:
            skipped.append((rel_posix, f"exceeds {max_file_bytes // 1024}KB safety limit"))
            continue

        try:
            with full.open("rb") as fh:
                sample = fh.read(8192)
            if _is_binary_bytes(sample):
                skipped.append((rel_posix, "binary file"))
                continue
        except OSError:
            continue

        kept.append(full)

    return kept, skipped


# =============================================================================
# Cross-Stack Priority Tiering (Tiers 1–8)
# =============================================================================

def _is_test_path(rel_posix: str) -> bool:
    parts = [p.lower() for p in rel_posix.split("/")]
    name = parts[-1]
    if any(p in {"test", "tests", "__tests__", "spec", "specs", "e2e", "integration_test", "androidtest", "testfixtures"} for p in parts[:-1]):
        return True
    if any(tok in name for tok in (".test.", ".spec.", "_test.", "_spec.", "jest.config", "vitest.config", "pytest.ini", "conftest.py")):
        return True
    if name.startswith("test_") and name.endswith(".py"):
        return True
    return False


def classify_tier(rel_posix: str, is_modified: bool, is_generated: bool) -> Tuple[int, str]:
    """Assigns a stack-agnostic priority tier (1 = highest, 8 = lowest)."""
    parts = rel_posix.split("/")
    lower_parts = [p.lower() for p in parts]
    name = parts[-1]
    lower_name = name.lower()
    depth = len(parts) - 1

    # Tier 1: Actively modified uncommitted source/doc files
    if is_modified and not is_generated:
        return 1, "uncommitted_change"

    # Tier 2: Root docs (*.md), primary manifests, and entry points
    if depth == 0 and lower_name.endswith((".md", ".mdx")):
        return 2, "root_doc"
    if name in MANIFEST_FILENAMES and depth <= 2:
        return 2, "manifest"
    if name in ENTRY_POINT_NAMES and not _is_test_path(rel_posix):
        return 2, "entry_point"

    # Tier 7: Tests, test configs, mocks, fixtures (checked before generic dir rules)
    if _is_test_path(rel_posix):
        return 7, "test_or_test_config"

    # Tier 6: Localization, nested docs, scripts, generated files
    if is_generated:
        return 6, "generated_code"
    if any(p in {"l10n", "i18n", "locales", "locale", "translations"} for p in lower_parts[:-1]) or lower_name.endswith(".arb"):
        return 6, "localization"
    if any(p in {"docs", "doc", "documentation", "adr", "rfcs"} for p in lower_parts[:-1]) or lower_name.endswith((".md", ".mdx", ".rst")):
        return 6, "documentation"

    # Tier 3: Domain, models, schemas, types, state, entities
    if any(p in {"domain", "models", "model", "schemas", "schema", "types", "entities", "entity", "state", "store", "stores", "reducers", "slices", "proto", "graphql"} for p in lower_parts[:-1]):
        return 3, "domain_or_state"

    # Tier 4: Core infrastructure, services, API, repositories, DB, hooks, utils
    if any(p in {"core", "services", "service", "api", "apis", "repositories", "repository", "repos", "repo", "data", "db", "database", "storage", "auth", "crypto", "security", "network", "client", "clients", "engine", "middleware", "hooks", "utils", "util", "helpers", "shared", "common", "providers", "pkg", "internal"} for p in lower_parts[:-1]):
        return 4, "core_or_service"

    # Tier 5: Features, screens, UI, pages, routes, controllers, components, general source
    if any(p in {"features", "feature", "screens", "screen", "pages", "page", "views", "view", "components", "component", "widgets", "widget", "ui", "routes", "router", "navigation", "controllers", "controller", "handlers", "handler", "commands", "cmd", "app", "src", "lib"} for p in lower_parts[:-1]):
        return 5, "feature_or_source"

    # Tier 6: Scripts / tooling
    if any(p in {"scripts", "script", "tools", "bin", "migrations", "seeds"} for p in lower_parts[:-1]):
        return 6, "script_or_migration"

    # Tier 8: Platform folders, CI, lint/build/editor configs
    if any(p in {"android", "ios", "macos", "windows", "linux", "web", ".github", ".gitlab", ".circleci", "deploy", "infra", "terraform", "k8s", "helm", "charts"} for p in lower_parts[:-1]):
        return 8, "platform_or_infra"

    # Remaining root code vs root config
    lang = _detect_language(Path(rel_posix))
    if lang in {"json", "jsonc", "yaml", "toml", "xml", "ini", "conf", "text"} or name.startswith("."):
        return 8, "config"

    return 5, "source"


# =============================================================================
# Stack-Agnostic Overview & Manifest Parsing
# =============================================================================

def _parse_package_json(path: Path) -> Dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    deps = data.get("dependencies", {}) or {}
    dev_deps = data.get("devDependencies", {}) or {}
    scripts = data.get("scripts", {}) or {}
    all_deps = {**deps, **dev_deps}
    frameworks: List[str] = []
    fw_checks = [
        ("expo", "Expo"), ("react-native", "React Native"), ("next", "Next.js"),
        ("nuxt", "Nuxt"), ("react", "React"), ("vue", "Vue"), ("svelte", "Svelte"),
        ("@angular/core", "Angular"), ("express", "Express"), ("fastify", "Fastify"),
        ("@nestjs/core", "NestJS"), ("hono", "Hono"), ("electron", "Electron"),
        ("tailwindcss", "Tailwind CSS"), ("prisma", "Prisma"), ("drizzle-orm", "Drizzle"),
        ("jest", "Jest"), ("vitest", "Vitest"), ("typescript", "TypeScript"),
    ]
    for pkg, label in fw_checks:
        if pkg in all_deps:
            frameworks.append(f"{label} ({all_deps[pkg]})")
    return {
        "name": data.get("name"),
        "version": data.get("version"),
        "frameworks": frameworks,
        "scripts": scripts,
        "dependencies": deps,
        "dev_dependencies": dev_deps,
    }


def _parse_pubspec_yaml(path: Path) -> Dict[str, object]:
    text = path.read_text(encoding="utf-8", errors="replace")
    name_m = re.search(r"^name:\s*([^\s#]+)", text, re.M)
    ver_m = re.search(r"^version:\s*([^\s#]+)", text, re.M)
    sdk_m = re.search(r"^\s*sdk:\s*['\"]?([^'\"\n#]+)", text, re.M)
    deps: Dict[str, str] = {}
    section = None
    for line in text.splitlines():
        if re.match(r"^(dependencies|dev_dependencies):", line):
            section = line.split(":")[0]
            continue
        if re.match(r"^[a-zA-Z0-9_]+:", line):
            section = None
            continue
        if section and re.match(r"^  [a-zA-Z0-9_]+:", line):
            k, _, v = line.strip().partition(":")
            deps[k] = v.strip() or "sdk/complex"
    frameworks = ["Flutter / Dart"] if "flutter" in deps else ["Dart"]
    for pkg, label in [
        ("riverpod", "Riverpod"), ("flutter_riverpod", "Riverpod"),
        ("flutter_bloc", "BLoC"), ("provider", "Provider"), ("get", "GetX"),
        ("go_router", "GoRouter"), ("drift", "Drift"), ("sqflite", "SQLite"),
        ("hive", "Hive"), ("dio", "Dio"),
    ]:
        if pkg in deps:
            frameworks.append(f"{label} ({deps[pkg]})")
    return {
        "name": name_m.group(1) if name_m else None,
        "version": ver_m.group(1) if ver_m else None,
        "sdk": sdk_m.group(1).strip() if sdk_m else None,
        "frameworks": frameworks,
        "dependencies": deps,
    }


def _parse_pyproject_toml(path: Path) -> Dict[str, object]:
    raw = path.read_bytes()
    frameworks: List[str] = []
    deps: List[str] = []
    scripts: Dict[str, str] = {}
    name = None
    version = None
    if tomllib is not None:
        try:
            data = tomllib.loads(raw.decode("utf-8", errors="replace"))
            proj = data.get("project", {})
            poetry = data.get("tool", {}).get("poetry", {})
            name = proj.get("name") or poetry.get("name")
            version = proj.get("version") or poetry.get("version")
            deps = list(proj.get("dependencies", []))
            if not deps and isinstance(poetry.get("dependencies"), dict):
                deps = [f"{k} {v}" for k, v in poetry["dependencies"].items() if k != "python"]
            scripts = dict(proj.get("scripts", {}) or poetry.get("scripts", {}) or {})
        except Exception:
            pass
    dep_blob = " ".join(deps).lower()
    for pkg, label in [
        ("fastapi", "FastAPI"), ("django", "Django"), ("flask", "Flask"),
        ("pydantic", "Pydantic"), ("sqlalchemy", "SQLAlchemy"), ("torch", "PyTorch"),
        ("transformers", "Transformers"), ("pytest", "Pytest"), ("celery", "Celery"),
    ]:
        if pkg in dep_blob:
            frameworks.append(label)
    return {
        "name": name, "version": version, "frameworks": frameworks,
        "scripts": scripts, "dependencies": deps[:40],
    }


def _parse_requirements_txt(path: Path) -> Dict[str, object]:
    deps = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.split("#", 1)[0].strip()
        if s and not s.startswith("-"):
            deps.append(s)
    return {"dependencies": deps[:40]}


def _parse_cargo_toml(path: Path) -> Dict[str, object]:
    text = path.read_text(encoding="utf-8", errors="replace")
    name_m = re.search(r'^name\s*=\s*"([^"]+)"', text, re.M)
    ver_m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    frameworks = ["Rust"]
    for crate, label in [("tokio", "Tokio"), ("axum", "Axum"), ("actix-web", "Actix-Web"), ("serde", "Serde"), ("tauri", "Tauri")]:
        if re.search(rf"^\s*{re.escape(crate)}\s*=", text, re.M):
            frameworks.append(label)
    return {"name": name_m.group(1) if name_m else None, "version": ver_m.group(1) if ver_m else None, "frameworks": frameworks}


def _parse_go_mod(path: Path) -> Dict[str, object]:
    text = path.read_text(encoding="utf-8", errors="replace")
    mod_m = re.search(r"^module\s+(\S+)", text, re.M)
    go_m = re.search(r"^go\s+(\S+)", text, re.M)
    frameworks = [f"Go {go_m.group(1)}"] if go_m else ["Go"]
    for pkg, label in [("gin-gonic/gin", "Gin"), ("gofiber/fiber", "Fiber"), ("labstack/echo", "Echo"), ("gorm.io/gorm", "GORM")]:
        if pkg in text:
            frameworks.append(label)
    return {"name": mod_m.group(1) if mod_m else None, "frameworks": frameworks}


def build_overview(root: Path, records: List[FileRecord]) -> Dict[str, object]:
    manifests: List[Dict[str, object]] = []
    frameworks_seen: List[str] = []
    entry_points: List[str] = []
    lang_loc: Counter = Counter()
    lang_files: Counter = Counter()
    lang_tokens: Counter = Counter()

    for rec in records:
        lang_loc[rec.language] += rec.loc
        lang_files[rec.language] += 1
        lang_tokens[rec.language] += rec.full_tokens

        fname = Path(rec.rel_path).name
        if rec.tier_reason == "entry_point":
            entry_points.append(rec.rel_path)

        if fname in MANIFEST_FILENAMES:
            parsed: Dict[str, object] = {"path": rec.rel_path}
            try:
                if fname == "package.json":
                    parsed.update(_parse_package_json(rec.abs_path))
                elif fname == "pubspec.yaml":
                    parsed.update(_parse_pubspec_yaml(rec.abs_path))
                elif fname == "pyproject.toml":
                    parsed.update(_parse_pyproject_toml(rec.abs_path))
                elif fname == "requirements.txt":
                    parsed.update(_parse_requirements_txt(rec.abs_path))
                elif fname == "Cargo.toml":
                    parsed.update(_parse_cargo_toml(rec.abs_path))
                elif fname == "go.mod":
                    parsed.update(_parse_go_mod(rec.abs_path))
                elif fname in {"build.gradle", "build.gradle.kts"}:
                    parsed["frameworks"] = ["Gradle / JVM / Android"]
                elif fname == "pom.xml":
                    parsed["frameworks"] = ["Maven / Java"]
                elif fname == "Dockerfile":
                    parsed["frameworks"] = ["Docker"]
            except Exception:
                pass
            for fw in parsed.get("frameworks", []) or []:
                if fw not in frameworks_seen:
                    frameworks_seen.append(str(fw))
            manifests.append(parsed)

    return {
        "project_name": root.resolve().name,
        "root_path": str(root.resolve()),
        "frameworks": frameworks_seen,
        "entry_points": entry_points,
        "manifests": manifests,
        "languages": [
            {"language": lang, "files": lang_files[lang], "loc": loc, "tokens": lang_tokens[lang]}
            for lang, loc in lang_loc.most_common()
        ],
    }


# =============================================================================
# Cross-Language Outline Extractor (Used ONLY When Truncated or --always-outline)
# =============================================================================

_OUTLINE_REGEXES: Dict[str, re.Pattern] = {
    "python": re.compile(r"^\s*(?:async\s+def|def|class)\s+[A-Za-z_][A-Za-z0-9_]*.*:$"),
    "typescript": re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?(?:async\s+)?(?:class|interface|type|enum|function|const\s+\w+\s*=\s*(?:async\s*)?\().*"),
    "tsx": re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?(?:async\s+)?(?:class|interface|type|enum|function|const\s+\w+\s*=\s*(?:async\s*)?\().*"),
    "javascript": re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:class|function|const\s+\w+\s*=\s*(?:async\s*)?\().*"),
    "jsx": re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:class|function|const\s+\w+\s*=\s*(?:async\s*)?\().*"),
    "dart": re.compile(r"^\s*(?:abstract\s+)?(?:sealed\s+)?(?:base\s+)?(?:class|mixin|extension|enum|typedef|Future<[^>]+>|Stream<[^>]+>|Widget|void|[A-Z][A-Za-z0-9_<>,?]*)\s+[A-Za-z_][A-Za-z0-9_]*[\(<{].*"),
    "kotlin": re.compile(r"^\s*(?:public|private|internal|protected|open|abstract|sealed|data|override|suspend|\s)*(?:class|interface|object|enum\s+class|fun)\s+.*"),
    "java": re.compile(r"^\s*(?:public|private|protected|static|final|abstract|\s)*(?:class|interface|enum|record|[\w<>[\]]+\s+\w+\s*\().*"),
    "swift": re.compile(r"^\s*(?:public|private|internal|fileprivate|open|final|static|override|\s)*(?:class|struct|enum|protocol|extension|actor|func)\s+.*"),
    "go": re.compile(r"^(?:func|type)\s+.*"),
    "rust": re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:fn|struct|enum|trait|impl|mod|type)\s+.*"),
    "csharp": re.compile(r"^\s*(?:public|private|protected|internal|static|async|virtual|override|sealed|partial|\s)*(?:namespace|class|struct|interface|record|enum|[\w<>[\]?]+\s+\w+\s*\().*"),
    "markdown": re.compile(r"^#{1,4}\s+.+"),
}


def extract_outline(content: str, language: str, max_items: int = 80) -> str:
    pattern = _OUTLINE_REGEXES.get(language)
    if pattern is None:
        return ""
    lines_out: List[str] = []
    for idx, line in enumerate(content.splitlines(), start=1):
        if len(line) > 200:
            continue
        if pattern.match(line):
            cleaned = line.rstrip(" {")
            lines_out.append(f"L{idx}: {cleaned}")
            if len(lines_out) >= max_items:
                lines_out.append("... (outline truncated)")
                break
    return "\n".join(lines_out)


# =============================================================================
# Optional Truncation Engine (Disabled by Default!)
# =============================================================================

def _make_head_tail(content: str, counter: TokenCounter, target_tokens: int) -> str:
    lines = content.splitlines()
    if len(lines) <= 20:
        return content

    # Allocate ~65% budget to head, ~35% to tail
    head_budget = max(40, int(target_tokens * 0.65))
    tail_budget = max(25, target_tokens - head_budget - 15)

    head_lines: List[str] = []
    used = 0
    for line in lines:
        t = counter.count(line + "\n")
        if used + t > head_budget and head_lines:
            break
        head_lines.append(line)
        used += t

    tail_lines: List[str] = []
    used_tail = 0
    for line in reversed(lines[len(head_lines):]):
        t = counter.count(line + "\n")
        if used_tail + t > tail_budget and tail_lines:
            break
        tail_lines.append(line)
        used_tail += t
    tail_lines.reverse()

    omitted = len(lines) - len(head_lines) - len(tail_lines)
    if omitted <= 0:
        return content
    marker = f"\n... [{omitted} lines omitted due to explicit truncation flag] ...\n"
    return "\n".join(head_lines) + marker + "\n".join(tail_lines)


def apply_optional_truncation(
    records: List[FileRecord],
    counter: TokenCounter,
    max_tokens: Optional[int],
    max_file_tokens: Optional[int],
    truncate_generated: bool,
    overhead_tokens: int,
) -> None:
    """
    Leaves every file at view="full" unless the user explicitly passed
    --max-tokens, --max-file-tokens, or --truncate-generated.
    """
    # Step 0: Initialize everything as full
    for rec in records:
        rec.view = "full"
        rec.rendered_content = rec.content
        rec.rendered_tokens = rec.full_tokens

    # Step 1: Optional generated-file compression
    if truncate_generated:
        for rec in records:
            if rec.is_generated and rec.rendered_tokens > 400:
                rec.outline = rec.outline or extract_outline(rec.content, rec.language)
                if rec.outline:
                    rec.view = "outline"
                    rec.rendered_content = ""
                    rec.rendered_tokens = counter.count(rec.outline) + 25
                else:
                    rec.view = "head_tail"
                    rec.rendered_content = _make_head_tail(rec.content, counter, 300)
                    rec.rendered_tokens = counter.count(rec.rendered_content)

    # Step 2: Optional per-file token cap
    if max_file_tokens is not None and max_file_tokens > 0:
        for rec in records:
            if rec.rendered_tokens > max_file_tokens and rec.view == "full":
                rec.outline = rec.outline or extract_outline(rec.content, rec.language)
                rec.view = "head_tail"
                rec.rendered_content = _make_head_tail(rec.content, counter, max_file_tokens)
                rec.rendered_tokens = counter.count(rec.rendered_content) + counter.count(rec.outline)

    # Step 3: Optional global token budget
    if max_tokens is None or max_tokens <= 0:
        return

    budget = max(500, max_tokens - overhead_tokens)

    def current_total() -> int:
        return sum(r.rendered_tokens + 20 for r in records)

    if current_total() <= budget:
        return

    # Downgrade lowest-priority (highest tier number, largest files first) to outline/head_tail
    downgrade_order = sorted(records, key=lambda r: (-r.tier, -r.rendered_tokens, r.rel_path))

    for rec in downgrade_order:
        if current_total() <= budget:
            break
        if rec.view != "full" or rec.rendered_tokens < 120:
            continue
        rec.outline = rec.outline or extract_outline(rec.content, rec.language)
        if rec.outline and counter.count(rec.outline) < rec.rendered_tokens * 0.65:
            rec.view = "outline"
            rec.rendered_content = ""
            rec.rendered_tokens = counter.count(rec.outline) + 20
        else:
            target = max(100, min(rec.rendered_tokens // 3, 600))
            rec.view = "head_tail"
            rec.rendered_content = _make_head_tail(rec.content, counter, target)
            rec.rendered_tokens = counter.count(rec.rendered_content) + counter.count(rec.outline)

    # If still over budget, omit lowest-priority files (still kept in <file_index>)
    if current_total() > budget:
        for rec in downgrade_order:
            if current_total() <= budget:
                break
            if rec.tier <= 1:
                continue
            rec.view = "omitted"
            rec.outline = ""
            rec.rendered_content = ""
            rec.rendered_tokens = 0


# =============================================================================
# XML Rendering
# =============================================================================

def _clean_xml_text(text: str) -> str:
    return _ILLEGAL_XML_RE.sub("", text)


def _cdata(text: str) -> str:
    cleaned = _clean_xml_text(text)
    safe = cleaned.replace("]]>", "]]]]><![CDATA[>")
    return f"<![CDATA[\n{safe}\n]]>"


def _attr(val: object) -> str:
    return xml_escape(str(val), {'"': "&quot;", "'": "&apos;"})


def render_directory_tree(rel_paths: Sequence[str]) -> str:
    tree: Dict[str, dict] = {}
    for p in sorted(rel_paths):
        node = tree
        for part in p.split("/"):
            node = node.setdefault(part, {})

    lines: List[str] = []

    def _walk(node: Dict[str, dict], prefix: str = "") -> None:
        # Directories first, then files
        items = sorted(node.items(), key=lambda kv: (len(kv[1]) == 0, kv[0].lower()))
        for i, (name, child) in enumerate(items):
            is_last = i == len(items) - 1
            connector = "└── " if is_last else "├── "
            suffix = "/" if child else ""
            lines.append(f"{prefix}{connector}{name}{suffix}")
            if child:
                ext = "    " if is_last else "│   "
                _walk(child, prefix + ext)

    _walk(tree)
    return "\n".join(lines)


def render_header_sections(
    overview: Dict[str, object],
    git_info: Dict[str, object],
    records: List[FileRecord],
) -> Tuple[str, str, str]:
    # 1. <overview>
    ov: List[str] = ["  <overview>"]
    ov.append(f"    <project_name>{xml_escape(str(overview['project_name']))}</project_name>")
    if overview["frameworks"]:
        ov.append("    <detected_stack>")
        for fw in overview["frameworks"]:  # type: ignore
            ov.append(f"      <item>{xml_escape(str(fw))}</item>")
        ov.append("    </detected_stack>")
    if overview["entry_points"]:
        ov.append("    <entry_points>")
        for ep in overview["entry_points"]:  # type: ignore
            ov.append(f"      <entry>{xml_escape(str(ep))}</entry>")
        ov.append("    </entry_points>")
    if overview["manifests"]:
        ov.append("    <manifests>")
        for m in overview["manifests"]:  # type: ignore
            m_attrs = f'path="{_attr(m["path"])}"'
            if m.get("name"):
                m_attrs += f' name="{_attr(m["name"])}"'
            if m.get("version"):
                m_attrs += f' version="{_attr(m["version"])}"'
            ov.append(f"      <manifest {m_attrs}>")
            if m.get("scripts"):
                ov.append("        <scripts>")
                for sk, sv in m["scripts"].items():
                    ov.append(f'          <script name="{_attr(sk)}">{xml_escape(str(sv))}</script>')
                ov.append("        </scripts>")
            if m.get("dependencies"):
                deps = m["dependencies"]
                ov.append("        <dependencies>")
                if isinstance(deps, dict):
                    for dk, dv in list(deps.items())[:50]:
                        ov.append(f'          <dep name="{_attr(dk)}" version="{_attr(dv)}" />')
                elif isinstance(deps, list):
                    for d in deps[:50]:
                        ov.append(f"          <dep>{xml_escape(str(d))}</dep>")
                ov.append("        </dependencies>")
            ov.append("      </manifest>")
        ov.append("    </manifests>")

    ov.append("    <languages>")
    for item in overview["languages"]:  # type: ignore
        ov.append(
            f'      <language name="{_attr(item["language"])}" '
            f'files="{item["files"]}" loc="{item["loc"]}" tokens="{item["tokens"]}" />'
        )
    ov.append("    </languages>")
    ov.append("  </overview>")

    # 2. <git_context>
    gc: List[str] = []
    if git_info.get("is_git_repo"):
        gc.append(
            f'  <git_context branch="{_attr(git_info["branch"])}" head="{_attr(git_info["head"])}">'
        )
        if git_info.get("status_lines"):
            gc.append("    <uncommitted_status>")
            gc.append(_cdata("\n".join(git_info["status_lines"])))  # type: ignore
            gc.append("    </uncommitted_status>")
        if git_info.get("diff_stat") or git_info.get("staged_stat"):
            combined = "\n".join(filter(None, [
                f"# Staged:\n{git_info['staged_stat']}" if git_info.get("staged_stat") else "",
                f"# Unstaged:\n{git_info['diff_stat']}" if git_info.get("diff_stat") else "",
            ]))
            gc.append("    <diff_stat>")
            gc.append(_cdata(combined))
            gc.append("    </diff_stat>")
        if git_info.get("recent_commits"):
            gc.append("    <recent_commits>")
            for c in git_info["recent_commits"]:  # type: ignore
                gc.append(f"      <commit>{xml_escape(str(c))}</commit>")
            gc.append("    </recent_commits>")
        gc.append("  </git_context>")

    # 3. <directory_tree>
    tree_str = render_directory_tree([r.rel_path for r in records])
    dt = f"  <directory_tree>\n{_cdata(tree_str)}\n  </directory_tree>"

    return "\n".join(ov), "\n".join(gc), dt


def build_xml_document(
    overview: Dict[str, object],
    git_info: Dict[str, object],
    records: List[FileRecord],
    counter: TokenCounter,
    always_outline: bool,
    truncation_enabled: bool,
    redaction_enabled: bool,
) -> Tuple[str, int]:
    ov_xml, gc_xml, dt_xml = render_header_sections(overview, git_info, records)

    # <file_index>
    idx_lines: List[str] = ["  <file_index>"]
    for rec in records:
        flags = []
        if rec.is_modified:
            flags.append('modified="true"')
        if rec.is_generated:
            flags.append('generated="true"')
        if rec.redaction_count > 0:
            flags.append(f'redactions="{rec.redaction_count}"')
        extra = (" " + " ".join(flags)) if flags else ""
        idx_lines.append(
            f'    <entry rank="{rec.rank}" tier="{rec.tier}" reason="{_attr(rec.tier_reason)}" '
            f'path="{_attr(rec.rel_path)}" language="{_attr(rec.language)}" '
            f'loc="{rec.loc}" tokens="{rec.rendered_tokens}" full_tokens="{rec.full_tokens}" '
            f'view="{rec.view}"{extra} />'
        )
    idx_lines.append("  </file_index>")
    idx_xml = "\n".join(idx_lines)

    # <files>
    file_blocks: List[str] = ["  <files>"]
    for rec in records:
        if rec.view == "omitted":
            continue
        file_blocks.append(
            f'    <file rank="{rec.rank}" tier="{rec.tier}" path="{_attr(rec.rel_path)}" '
            f'language="{_attr(rec.language)}" loc="{rec.loc}" tokens="{rec.rendered_tokens}" '
            f'view="{rec.view}">'
        )
        # Emit <outline> ONLY if truncated or explicitly requested via --always-outline
        show_outline = (rec.view != "full") or always_outline
        if show_outline:
            outline_text = rec.outline or extract_outline(rec.content, rec.language)
            if outline_text:
                file_blocks.append(f"      <outline>\n{_cdata(outline_text)}\n      </outline>")
        if rec.rendered_content:
            file_blocks.append(f"      <content>\n{_cdata(rec.rendered_content)}\n      </content>")
        file_blocks.append("    </file>")
    file_blocks.append("  </files>")
    files_xml = "\n".join(file_blocks)

    body_parts = [ov_xml]
    if gc_xml:
        body_parts.append(gc_xml)
    body_parts.extend([dt_xml, idx_xml, files_xml])
    body = "\n".join(body_parts)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    total_loc = sum(r.loc for r in records)
    included_full = sum(1 for r in records if r.view == "full")
    truncated_count = sum(1 for r in records if r.view != "full")

    # Compute exact/estimated total XML tokens including wrapper
    header_placeholder = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<project_context project="{_attr(overview["project_name"])}" '
        f'generated_at="{generated_at}" '
        f'total_files="{len(records)}" full_files="{included_full}" truncated_files="{truncated_count}" '
        f'total_loc="{total_loc}" token_counting="{_attr(counter.mode)}" '
        f'truncation_mode="{"enabled" if truncation_enabled else "disabled"}" '
        f'secret_redaction="{"enabled" if redaction_enabled else "disabled"}" '
        f'total_tokens="__TOTAL_TOKENS__">\n'
    )
    footer = "\n</project_context>\n"
    total_xml_tokens = counter.count(header_placeholder + body + footer)
    final_xml = header_placeholder.replace("__TOTAL_TOKENS__", str(total_xml_tokens)) + body + footer
    return final_xml, total_xml_tokens


# =============================================================================
# CLI Entry Point
# =============================================================================

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Package any codebase into a complete, structured, token-counted XML context for LLMs."
    )
    p.add_argument(
        "project_dir",
        nargs="?",
        default=".",
        help="Path to the project root directory (default: current directory).",
    )
    p.add_argument(
        "-o", "--output",
        default=None,
        help="Output XML file path (default: <project_name>_context.xml). Use '-' for stdout.",
    )

    # Optional truncation flags (ALL OFF by default)
    trunc = p.add_argument_group("Optional Truncation (disabled by default — all files full)")
    trunc.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="Enable token-budget truncation to fit output within approximately N tokens.",
    )
    trunc.add_argument(
        "--max-file-tokens",
        type=int,
        default=None,
        help="Optionally cap individual files larger than N tokens using head+tail + outline.",
    )
    trunc.add_argument(
        "--truncate-generated",
        action="store_true",
        help="Optionally outline/truncate auto-generated files (e.g. *.g.dart, *.pb.go, l10n).",
    )

    # Token counting options
    tok = p.add_argument_group("Token Counting")
    tok.add_argument(
        "--encoding",
        default="o200k_base",
        help="tiktoken encoding if installed (default: o200k_base; e.g., cl100k_base).",
    )
    tok.add_argument(
        "--estimate-tokens",
        action="store_true",
        help="Force fast built-in heuristic token counting even if tiktoken is installed.",
    )
    tok.add_argument(
        "--stats-only",
        action="store_true",
        help="Print token/file breakdown without writing an XML file.",
    )

    # Inclusion / exclusion & formatting options
    filt = p.add_argument_group("Filtering & Formatting")
    filt.add_argument("--no-gitignore", action="store_true", help="Ignore .gitignore rules and walk the filesystem.")
    filt.add_argument("--no-tests", action="store_true", help="Exclude test files and test configs.")
    filt.add_argument("--include-lockfiles", action="store_true", help="Include dependency lockfiles.")
    filt.add_argument("--include-env", action="store_true", help="Include .env files (use with caution!).")
    filt.add_argument("--include-glob", action="append", default=[], help="Force-include matching glob (repeatable).")
    filt.add_argument("--exclude-glob", action="append", default=[], help="Exclude matching glob (repeatable).")
    filt.add_argument(
        "--max-file-kb",
        type=int,
        default=2048,
        help="Skip single files larger than N KB (default: 2048 KB).",
    )
    filt.add_argument("--always-outline", action="store_true", help="Include <outline> even for full files.")
    filt.add_argument("--no-redact", action="store_true", help="Disable secret redaction completely.")
    filt.add_argument("--commits", type=int, default=10, help="Number of recent git commits to include (default: 10).")

    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = Path(args.project_dir).resolve()
    if not root.is_dir():
        print(f"Error: '{root}' is not a valid directory.", file=sys.stderr)
        return 1

    output_to_stdout = args.output == "-"
    if args.output is None:
        out_path: Optional[Path] = Path.cwd() / f"{root.name}_context.xml"
    elif output_to_stdout:
        out_path = None
    else:
        out_path = Path(args.output).resolve()

    counter = TokenCounter(encoding_name=args.encoding, force_estimate=args.estimate_tokens)
    git_info, modified_paths = collect_git_context(root, recent_commits=args.commits)

    file_paths, skipped = discover_files(
        root=root,
        use_gitignore=not args.no_gitignore,
        include_lockfiles=args.include_lockfiles,
        include_env=args.include_env,
        include_tests=not args.no_tests,
        extra_excludes=args.exclude_glob,
        extra_includes=args.include_glob,
        output_path=out_path,
        max_file_bytes=args.max_file_kb * 1024,
    )

    records: List[FileRecord] = []
    for full_path in file_paths:
        rel_posix = full_path.relative_to(root).as_posix()
        try:
            raw_text = full_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        redactions = 0
        processed = raw_text
        if not args.no_redact:
            processed, redactions = redact_secrets(raw_text)

        is_gen = _is_generated_file(rel_posix, raw_text[:600])
        is_mod = rel_posix in modified_paths
        tier, reason = classify_tier(rel_posix, is_modified=is_mod, is_generated=is_gen)
        lang = _detect_language(full_path)
        loc = processed.count("\n") + (1 if processed and not processed.endswith("\n") else 0)
        toks = counter.count(processed)

        records.append(FileRecord(
            rel_path=rel_posix,
            abs_path=full_path,
            language=lang,
            tier=tier,
            tier_reason=reason,
            size_bytes=len(raw_text.encode("utf-8", errors="replace")),
            loc=loc,
            raw_content=raw_text,
            content=processed,
            full_tokens=toks,
            is_generated=is_gen,
            is_modified=is_mod,
            redaction_count=redactions,
        ))

    # Sort deterministically: Tier ascending, then alphabetical path
    records.sort(key=lambda r: (r.tier, r.rel_path.lower()))
    for idx, rec in enumerate(records, start=1):
        rec.rank = idx

    overview = build_overview(root, records)
    ov_xml, gc_xml, dt_xml = render_header_sections(overview, git_info, records)
    est_index_tokens = len(records) * 32
    overhead_tokens = counter.count(ov_xml + gc_xml + dt_xml) + est_index_tokens + 200

    truncation_enabled = bool(args.max_tokens or args.max_file_tokens or args.truncate_generated)
    apply_optional_truncation(
        records=records,
        counter=counter,
        max_tokens=args.max_tokens,
        max_file_tokens=args.max_file_tokens,
        truncate_generated=args.truncate_generated,
        overhead_tokens=overhead_tokens,
    )

    xml_doc, total_xml_tokens = build_xml_document(
        overview=overview,
        git_info=git_info,
        records=records,
        counter=counter,
        always_outline=args.always_outline,
        truncation_enabled=truncation_enabled,
        redaction_enabled=not args.no_redact,
    )

    if not args.stats_only:
        if output_to_stdout:
            sys.stdout.write(xml_doc)
        elif out_path is not None:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(xml_doc, encoding="utf-8")

    # Summary report (printed to stderr if writing XML to stdout, else stdout)
    stream = sys.stderr if output_to_stdout else sys.stdout
    total_loc = sum(r.loc for r in records)
    total_full_content_tokens = sum(r.full_tokens for r in records)
    total_redactions = sum(r.redaction_count for r in records)
    view_counts = Counter(r.view for r in records)

    print(f"\nProject:      {overview['project_name']} ({root})", file=stream)
    if overview["frameworks"]:
        print(f"Stack:        {', '.join(overview['frameworks'][:10])}", file=stream)  # type: ignore
    print(f"Files:        {len(records)} included ({dict(view_counts)}), {len(skipped)} skipped", file=stream)
    print(f"Lines (LOC):  {total_loc:,}", file=stream)
    print(f"Token mode:   {counter.mode}", file=stream)
    print(f"Code tokens:  {total_full_content_tokens:,} (untruncated content)", file=stream)
    print(f"XML tokens:   {total_xml_tokens:,} (complete final output)", file=stream)
    if total_redactions:
        redacted_files = [f"{r.rel_path} ({r.redaction_count})" for r in records if r.redaction_count]
        print(f"Redactions:   {total_redactions} across {', '.join(redacted_files)}", file=stream)
    if not args.stats_only and out_path is not None:
        size_kb = out_path.stat().st_size / 1024
        print(f"Output file:  {out_path} ({size_kb:,.1f} KB)", file=stream)

    # Tier breakdown
    tier_tokens: Dict[int, int] = defaultdict(int)
    tier_counts: Dict[int, int] = defaultdict(int)
    for r in records:
        tier_tokens[r.tier] += r.rendered_tokens
        tier_counts[r.tier] += 1
    print("\nBreakdown by Tier:", file=stream)
    for t in sorted(tier_counts):
        print(f"  Tier {t}: {tier_counts[t]:>4} files | {tier_tokens[t]:>9,} tokens", file=stream)

    # Top 8 largest files so bloat (like generated l10n files) is immediately visible
    top_files = sorted(records, key=lambda r: r.rendered_tokens, reverse=True)[:8]
    if top_files:
        print("\nTop Files by Token Count:", file=stream)
        for r in top_files:
            tag = f" [{r.view}]" if r.view != "full" else ""
            gen = " (generated)" if r.is_generated else ""
            print(f"  {r.rendered_tokens:>9,} tokens | T{r.tier} | {r.rel_path}{gen}{tag}", file=stream)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())