#!/usr/bin/env python3
"""
doc_project.py - Project Code Documenter & LLM Context Packer

Scans a project directory and writes a priority-sorted, token-aware XML (or
Markdown) pack that can be attached to LLM prompts (Gemini, Claude, GPT, IDE
agents, ...).  Single file, standard library only, Python 3.10+.

Optional dependencies (all degrade gracefully when missing):
  pathspec   exact gitignore semantics for .docignore   (fallback: fnmatch-style)
  tiktoken   real token counts                          (fallback: len(text)/4)
  tomli      TOML parsing on Python 3.10                (fallback: built-in mini parser)
  pyperclip  --copy                                     (fallback: pbcopy/xclip/wl-copy/clip)

Usage:
  GUI mode:  python doc_project.py
  CLI mode:  python doc_project.py --cli --path /path/to/project

Options:
  --no-compress           keep original whitespace
  --include-tests         include test/spec/mock files and test directories
  --include-docs          include auxiliary markdown/text docs (README*, CLAUDE.md,
                          GEMINI.md, AGENTS.md, ARCHITECTURE.md, CONTRIBUTING.md,
                          SECURITY.md and requirements/CMake files are always kept)
  --include-native-config include top-level android/ and ios/ folders
  --only-changed          only files with uncommitted git changes (+ their `git diff HEAD`)
  --focus SUBSTR,...      promote paths containing any substring to Tier 0
  --max-file-kb N         skip files larger than N KB (default 100)
  --max-tokens N          enforce a token budget (full -> outline -> path-only)
  --format xml|md         output format (default xml)
  --output-dir PATH       output directory (default: DEFAULT_OUTPUT_DIR below; GUI mode asks
                          where to save and opens its dialog in that folder)
  --copy                  also copy the result to the clipboard
  --dry-run               print the 25 costliest files and per-tier totals; write nothing
  --no-redact             disable secret redaction of file content
  -v, --verbose           print every included file

Optional .docproject.toml in the project root (every key optional):
  max_tokens            = 120000
  max_file_kb           = 100
  include_native_config = false
  exclude_dts           = false          # drop *.d.ts files
  output_dir            = "../packs"     # relative to the project
  core_docs             = ["RULES.md"]   # ADDED to the built-in core doc names/globs
  extra_excludes        = ["generated/", "*.fixture.json"]   # gitignore syntax
  [tiers]                                # extra glob rules, checked before the stack profiles
  2 = ["src/rules/**"]
  4 = ["src/engine/**", "**/pricing.ts"]

Optional .docignore in the project root uses gitignore syntax.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import NamedTuple
from xml.sax.saxutils import escape as _escape_text

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

DEFAULT_MAX_FILE_KB = 100
GIT_TIMEOUT = 30
OUTPUT_DIR_NAME = "output_docs"
# Where output goes when nothing else says otherwise; also where the GUI "save to" dialog opens.
DEFAULT_OUTPUT_DIR = r"D:\Projects\Document_Project\output_docs"
CONFIG_FILENAME = ".docproject.toml"
IGNORE_FILENAME = ".docignore"
EST_PLACEHOLDER = "__EST_TOKENS__"
DIFF_CAP_LINES = 150
DIFF_CAP_CHARS = 12000
MAX_DIFF_FILES = 300
OUTLINE_MAX_LINES = 300
MIN_TRUNCATE_TOKENS = 80
OUTLINE_SHARE = 0.7  # share of the post-"full" budget that outlines may use
FULL_SHARE = 0.6  # share of a tight token budget that top tiers may spend on full content

# Well-known dependency / cache / tooling dirs: ALWAYS skipped (git mode and walk mode).
DEPENDENCY_DIRS = {
    "node_modules", "bower_components", "venv", ".venv", "__pycache__", "site-packages",
    "__pypackages__", ".git", ".hg", ".svn", "Pods", "DerivedData", ".gradle", ".kotlin",
    ".dart_tool", ".pub-cache", ".idea", ".vscode", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", ".parcel-cache", ".next", ".nuxt", ".svelte-kit", ".turbo",
    ".expo", ".vercel", ".netlify", "htmlcov", OUTPUT_DIR_NAME,
}
# Generic names: only skipped when we have no git (walk mode). In git mode .gitignore decides.
GENERIC_BUILD_DIRS = {"build", "dist", "target", "env", "out", "coverage"}
NATIVE_DIRS = {"android", "ios"}  # top-level only, unless include_native_config
TEST_DIRS = {"test", "tests", "__tests__", "__mocks__", "__snapshots__", "spec", "specs",
             "e2e", "cypress", "playwright"}

EXCLUDED_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".webp", ".ico", ".tiff", ".tif",
    ".psd", ".ai", ".eps", ".log", ".csv", ".sqlite", ".sqlite3", ".db", ".parquet",
    ".pkl", ".pickle", ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".mp3", ".wav", ".ogg", ".flac", ".mp4", ".webm", ".mkv", ".avi", ".mov",
    ".pdf", ".zip", ".tar", ".gz", ".rar", ".7z", ".dmg", ".exe", ".dll", ".jar", ".war",
    ".pyc", ".pyo", ".class", ".o", ".so", ".dylib", ".wasm", ".map",
    ".pem", ".key", ".cert", ".p12", ".jks", ".keystore", ".mobileprovision",
    ".aab", ".apk", ".ipa", ".lock", ".snap", ".tsbuildinfo",
}
# Compound suffixes can't be seen by os.path.splitext, so they are matched with endswith.
EXCLUDED_SUFFIXES = (".min.js", ".min.css")
DTS_SUFFIX = ".d.ts"  # excluded only when exclude_dts is set

EXCLUDED_FILES = {
    "package-lock.json", "yarn.lock", "poetry.lock", "pnpm-lock.yaml", "Pipfile.lock",
    "composer.lock", "Gemfile.lock", "Cargo.lock", "bun.lockb", "go.sum",
    ".DS_Store", "Thumbs.db", ".gitignore", ".gitattributes", ".editorconfig",
    ".prettierignore", ".eslintignore", ".npmrc", ".nvmrc",
    "LICENSE", "LICENSE.md", "LICENSE.txt",
    "google-services.json", "GoogleService-Info.plist",
}

SECRET_FILENAME_PATTERNS = [
    re.compile(r"^\.env(\..+)?$", re.IGNORECASE),
    re.compile(r".*secret.*\.(json|yaml|yml|txt|ini)$", re.IGNORECASE),
    re.compile(r".*credentials.*\.(json|yaml|yml|txt|ini)$", re.IGNORECASE),
]
ALLOWED_ENV_FILES = {".env.example", ".env.sample", ".env.template"}

TEST_FILE_PATTERNS = [
    re.compile(r"\.test\.[a-z0-9]+$", re.IGNORECASE),
    re.compile(r"\.spec\.[a-z0-9]+$", re.IGNORECASE),
    re.compile(r"_test\.[a-z0-9]+$", re.IGNORECASE),
    re.compile(r"^test_[^/\\]+\.[a-z0-9]+$", re.IGNORECASE),
    re.compile(r"^conftest\.py$", re.IGNORECASE),
    re.compile(r"^jest\.config\.[a-z0-9]+$", re.IGNORECASE),
    re.compile(r"^vitest\.config\.[a-z0-9]+$", re.IGNORECASE),
    re.compile(r"[a-z0-9]Tests?\.(kt|java|swift|cs)$"),  # case-sensitive: FooTest.kt
]

KNOWN_EXTENSIONLESS = {
    "dockerfile", "containerfile", "makefile", "gnumakefile", "procfile", "gemfile",
    "jenkinsfile", "rakefile", "vagrantfile", "justfile", "brewfile", "podfile",
    "fastfile", "appfile", "caddyfile",
}
DOC_EXTENSIONS = {".md", ".markdown", ".mdx", ".rst", ".txt", ".adoc"}
CORE_DOC_DEFAULTS = ["README*", "CLAUDE.md", "GEMINI.md", "AGENTS.md", "ARCHITECTURE.md",
                     "CONTRIBUTING.md", "SECURITY.md"]
CONFIG_LIKE_EXTS = {".env", ".ini", ".cfg", ".conf", ".properties", ".yaml", ".yml",
                    ".toml", ".sh", ".bash", ".zsh"}

LANG_NAMES = {
    ".py": "Python", ".pyi": "Python", ".js": "JavaScript", ".jsx": "JavaScript",
    ".mjs": "JavaScript", ".cjs": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".mts": "TypeScript", ".cts": "TypeScript", ".dart": "Dart", ".kt": "Kotlin",
    ".kts": "Kotlin", ".java": "Java", ".swift": "Swift", ".m": "Objective-C", ".go": "Go",
    ".rs": "Rust", ".rb": "Ruby", ".php": "PHP", ".c": "C", ".h": "C/C++ header",
    ".cpp": "C++", ".cc": "C++", ".hpp": "C++", ".cs": "C#", ".sh": "Shell",
    ".bash": "Shell", ".zsh": "Shell", ".json": "JSON", ".yaml": "YAML", ".yml": "YAML",
    ".toml": "TOML", ".md": "Markdown", ".mdx": "Markdown", ".markdown": "Markdown",
    ".rst": "reStructuredText", ".html": "HTML", ".css": "CSS", ".scss": "SCSS",
    ".sql": "SQL", ".gradle": "Gradle", ".xml": "XML", ".proto": "Protocol Buffers",
    ".graphql": "GraphQL", ".vue": "Vue", ".svelte": "Svelte", ".txt": "Text",
}

WARNINGS: list[str] = []


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"WARNING: {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# XML helpers
# --------------------------------------------------------------------------- #

_XML_ILLEGAL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def strip_illegal_xml(text: str) -> str:
    """Remove characters that are not allowed in XML 1.0 documents."""
    return _XML_ILLEGAL_RE.sub("", text)


def xattr(value) -> str:
    """Escape a value for use inside a double-quoted XML attribute."""
    return _escape_text(strip_illegal_xml(str(value)),
                        {'"': "&quot;", "\n": "&#10;", "\r": "&#13;", "\t": "&#9;"})


def cdata(text: str) -> str:
    """Wrap text in CDATA, splitting any embedded ']]>' so the document stays well-formed."""
    text = strip_illegal_xml(text).replace("]]>", "]]]]><![CDATA[>")
    return f"<![CDATA[\n{text}\n]]>"


# --------------------------------------------------------------------------- #
# TOML (tomllib / tomli / tiny fallback parser)
# --------------------------------------------------------------------------- #

class _MiniToml:
    """Very small TOML subset parser, used only when neither tomllib nor tomli exist."""

    _BARE = re.compile(r"[A-Za-z0-9_\-]+")

    def __init__(self, text: str):
        self.s, self.i, self.n = text, 0, len(text)

    def _ws(self, newlines: bool = False) -> None:
        s = self.s
        while self.i < self.n:
            c = s[self.i]
            if c in " \t" or (newlines and c in "\r\n"):
                self.i += 1
            elif c == "#":
                while self.i < self.n and s[self.i] not in "\r\n":
                    self.i += 1
            else:
                break

    def _str_basic(self) -> str:
        s, i, out = self.s, self.i + 1, []
        esc = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "b": "\b", "f": "\f"}
        while i < self.n:
            c = s[i]
            if c == "\\" and i + 1 < self.n:
                nx = s[i + 1]
                if nx in esc:
                    out.append(esc[nx]); i += 2
                elif nx in "uU":
                    ln = 4 if nx == "u" else 8
                    out.append(chr(int(s[i + 2:i + 2 + ln], 16))); i += 2 + ln
                else:
                    out.append(nx); i += 2
            elif c == '"':
                self.i = i + 1
                return "".join(out)
            else:
                out.append(c); i += 1
        raise ValueError("unterminated TOML string")

    def _key_path(self) -> list[str]:
        keys: list[str] = []
        while True:
            self._ws()
            c = self.s[self.i] if self.i < self.n else ""
            if c == '"':
                keys.append(self._str_basic())
            elif c == "'":
                j = self.s.index("'", self.i + 1)
                keys.append(self.s[self.i + 1:j]); self.i = j + 1
            else:
                m = self._BARE.match(self.s, self.i)
                if not m:
                    raise ValueError("bad TOML key")
                keys.append(m.group(0)); self.i = m.end()
            self._ws()
            if self.i < self.n and self.s[self.i] == ".":
                self.i += 1
                continue
            return keys

    def _value(self):
        s = self.s
        if self.i >= self.n:
            raise ValueError("unexpected end of TOML")
        c = s[self.i]
        if s.startswith('"""', self.i) or s.startswith("'''", self.i):
            q = s[self.i:self.i + 3]
            j = s.index(q, self.i + 3)
            val = s[self.i + 3:j]
            self.i = j + 3
            return val[1:] if val.startswith("\n") else val
        if c == '"':
            return self._str_basic()
        if c == "'":
            j = s.index("'", self.i + 1)
            val = s[self.i + 1:j]; self.i = j + 1
            return val
        if c == "[":
            self.i += 1
            items = []
            while True:
                self._ws(True)
                if self.i >= self.n:
                    raise ValueError("unterminated array")
                if s[self.i] == "]":
                    self.i += 1
                    return items
                items.append(self._value())
                self._ws(True)
                if self.i < self.n and s[self.i] == ",":
                    self.i += 1
        if c == "{":
            self.i += 1
            table: dict = {}
            while True:
                self._ws(True)
                if self.i >= self.n:
                    raise ValueError("unterminated inline table")
                if s[self.i] == "}":
                    self.i += 1
                    return table
                keys = self._key_path()
                self.i += 1  # '='
                self._ws()
                self._set(table, keys, self._value())
                self._ws(True)
                if self.i < self.n and s[self.i] == ",":
                    self.i += 1
        j = self.i
        while j < self.n and s[j] not in ",]}#\r\n":
            j += 1
        tok = s[self.i:j].strip()
        self.i = j
        if tok in ("true", "false"):
            return tok == "true"
        try:
            return int(tok.replace("_", ""), 0)
        except ValueError:
            pass
        try:
            return float(tok.replace("_", ""))
        except ValueError:
            return tok

    @staticmethod
    def _set(target: dict, keys: list[str], value) -> None:
        for k in keys[:-1]:
            target = target.setdefault(k, {})
        target[keys[-1]] = value

    def parse(self) -> dict:
        root: dict = {}
        cur = root
        while True:
            self._ws(True)
            if self.i >= self.n:
                return root
            if self.s[self.i] == "[":
                is_arr = self.s.startswith("[[", self.i)
                self.i += 2 if is_arr else 1
                keys = self._key_path()
                self.i += 2 if is_arr else 1
                node = root
                for k in keys[:-1]:
                    node = node.setdefault(k, {})
                    if isinstance(node, list):
                        node = node[-1]
                if is_arr:
                    cur = {}
                    node.setdefault(keys[-1], []).append(cur)
                else:
                    cur = node.setdefault(keys[-1], {})
            else:
                keys = self._key_path()
                self.i += 1  # '='
                self._ws()
                self._set(cur, keys, self._value())


def parse_toml(text: str) -> dict:
    try:
        import tomllib as lib  # type: ignore
    except ImportError:
        try:
            import tomli as lib  # type: ignore
        except ImportError:
            lib = None
    if lib is not None:
        return lib.loads(text)
    return _MiniToml(text).parse()


# --------------------------------------------------------------------------- #
# Glob / ignore handling
# --------------------------------------------------------------------------- #

def _glob_to_regex(pat: str) -> str:
    i, n, out = 0, len(pat), []
    while i < n:
        c = pat[i]
        if c == "*":
            if pat.startswith("**", i):
                j = i + 2
                if j < n and pat[j] == "/":
                    out.append("(?:.*/)?"); i = j + 1
                else:
                    out.append(".*"); i = j
            else:
                out.append("[^/]*"); i += 1
        elif c == "?":
            out.append("[^/]"); i += 1
        elif c == "[":
            j = i + 1
            if j < n and pat[j] in "!^":
                j += 1
            if j < n and pat[j] == "]":
                j += 1
            while j < n and pat[j] != "]":
                j += 1
            if j >= n:
                out.append(re.escape(c)); i += 1
            else:
                body = pat[i + 1:j]
                if body[:1] in ("!", "^"):
                    body = "^" + body[1:]
                body = body.replace("\\", "\\\\").replace("[", "\\[")
                out.append("[" + body + "]"); i = j + 1
        elif c == "\\" and i + 1 < n:
            out.append(re.escape(pat[i + 1])); i += 2
        else:
            out.append(re.escape(c)); i += 1
    return "".join(out)


def compile_glob(pat: str):
    """Compile a path glob (supports **) into a case-insensitive anchored regex."""
    pat = pat.strip().lstrip("/")
    if pat.endswith("/"):
        pat += "**"
    try:
        return re.compile("^" + _glob_to_regex(pat) + "$", re.IGNORECASE)
    except re.error:
        warn(f"invalid glob pattern ignored: {pat!r}")
        return None


def _compile_ignore_rules(lines: list[str]) -> list[tuple]:
    rules = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        line = line.rstrip()
        neg = line.startswith("!")
        if neg:
            line = line[1:]
        elif line.startswith("\\#") or line.startswith("\\!"):
            line = line[1:]
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        if not line:
            continue
        anchored = "/" in line
        line = line.lstrip("/")
        prefix = "" if anchored else "(?:.*/)?"
        tail = "/.*" if dir_only else "(?:/.*)?"
        try:
            rx = re.compile("^" + prefix + _glob_to_regex(line) + tail + "$", re.DOTALL)
        except re.error:
            continue
        rules.append((rx, neg))
    return rules


class IgnoreSpec:
    """gitignore-style matcher: uses `pathspec` when installed, else an fnmatch-style fallback."""

    def __init__(self, lines: list[str]):
        self.lines = [l for l in lines if l.strip() and not l.lstrip().startswith("#")]
        self.backend = "fnmatch"
        self._ps = None
        self._rules: list[tuple] = []
        if not self.lines:
            return
        try:
            import pathspec  # type: ignore
            try:
                self._ps = pathspec.GitIgnoreSpec.from_lines(lines)
            except AttributeError:
                self._ps = pathspec.PathSpec.from_lines("gitwildmatch", lines)
            self.backend = "pathspec"
        except Exception:
            self._ps = None
        if self._ps is None:
            self._rules = _compile_ignore_rules(lines)

    def __bool__(self) -> bool:
        return bool(self.lines)

    def match(self, rel: str) -> bool:
        if self._ps is not None:
            return bool(self._ps.match_file(rel))
        ignored = False
        for rx, neg in self._rules:
            if rx.match(rel):
                ignored = not neg
        return ignored


# --------------------------------------------------------------------------- #
# Project config (.docproject.toml)
# --------------------------------------------------------------------------- #

@dataclass
class ProjectConfig:
    tiers: dict = field(default_factory=dict)          # {tier:int -> [glob, ...]}
    core_docs: list = field(default_factory=list)
    extra_excludes: list = field(default_factory=list)
    include_native_config: bool | None = None
    max_tokens: int | None = None
    max_file_kb: int | None = None
    output_dir: str | None = None
    exclude_dts: bool = False


def _str_list(value, key: str) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    warn(f"{CONFIG_FILENAME}: '{key}' must be a list of strings; ignored")
    return []


def load_project_config(project_dir: str) -> ProjectConfig:
    cfg = ProjectConfig()
    path = os.path.join(project_dir, CONFIG_FILENAME)
    if not os.path.isfile(path):
        return cfg
    try:
        with open(path, "rb") as fh:
            data = parse_toml(fh.read().decode("utf-8-sig", "replace"))
    except Exception as exc:
        warn(f"could not parse {CONFIG_FILENAME} ({exc}); using defaults")
        return cfg
    tiers = data.get("tiers", {})
    if isinstance(tiers, dict):
        for k, v in tiers.items():
            try:
                tier = int(k)
            except (TypeError, ValueError):
                warn(f"{CONFIG_FILENAME}: tier key {k!r} is not an integer; ignored")
                continue
            if not 0 <= tier <= 9:
                warn(f"{CONFIG_FILENAME}: tier {tier} out of range 0-9; ignored")
                continue
            cfg.tiers[tier] = _str_list(v, f"tiers.{k}")
    cfg.core_docs = _str_list(data.get("core_docs", []), "core_docs")
    cfg.extra_excludes = _str_list(data.get("extra_excludes", []), "extra_excludes")
    if isinstance(data.get("include_native_config"), bool):
        cfg.include_native_config = data["include_native_config"]
    if isinstance(data.get("exclude_dts"), bool):
        cfg.exclude_dts = data["exclude_dts"]
    for key in ("max_tokens", "max_file_kb"):
        val = data.get(key)
        if isinstance(val, int) and not isinstance(val, bool) and val > 0:
            setattr(cfg, key, val)
        elif val is not None:
            warn(f"{CONFIG_FILENAME}: '{key}' must be a positive integer; ignored")
    if isinstance(data.get("output_dir"), str):
        cfg.output_dir = data["output_dir"]
    return cfg


# --------------------------------------------------------------------------- #
# Git
# --------------------------------------------------------------------------- #

class GitResult(NamedTuple):
    ok: bool
    out: str
    err: str
    problem: str | None  # None | missing | timeout | not_repo | error


def _git(project_dir: str, args: list[str], timeout: int = GIT_TIMEOUT) -> GitResult:
    cmd = ["git", "-c", "core.quotepath=off", "-C", project_dir] + args
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", LC_ALL="C", LANG="C")
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env)
    except FileNotFoundError:
        return GitResult(False, "", "git executable not found", "missing")
    except subprocess.TimeoutExpired:
        return GitResult(False, "", f"timed out after {timeout}s", "timeout")
    except OSError as exc:
        return GitResult(False, "", str(exc), "error")
    out = res.stdout.decode("utf-8", "replace")
    err = res.stderr.decode("utf-8", "replace").strip()
    if res.returncode != 0:
        problem = "not_repo" if "not a git repository" in err else "error"
        return GitResult(False, out, err, problem)
    return GitResult(True, out, err, None)


def run_git_cmd(project_dir: str, args: list[str], timeout: int = GIT_TIMEOUT,
                quiet: bool = False) -> str | None:
    """Run git and return RAW stdout (never stripped: porcelain output has meaningful
    leading spaces).  Returns None and prints a warning on failure/timeout."""
    res = _git(project_dir, args, timeout)
    if res.ok:
        return res.out
    if not quiet:
        desc = [a for a in args if not a.startswith("-")][:1] or args[:1]
        warn(f"git {desc[0]} failed ({res.problem}: {res.err or 'no details'})")
    return None


def parse_porcelain_z(raw: str, prefix: str, renames: dict[str, str] | None = None) -> dict[str, str]:
    """Parse `git status --porcelain=v1 -z` into {path-relative-to-project: status code}.
    Records are NUL separated; renames/copies look like 'R  new\\0old'. When `renames`
    is given it is filled with {new_path: old_path} (both relative to the project)."""
    changed: dict[str, str] = {}
    records = raw.split("\0")
    i = 0
    while i < len(records):
        rec = records[i]
        i += 1
        if len(rec) < 4 or rec[2] != " ":
            continue
        x, y, path = rec[0], rec[1], rec[3:]
        old = None
        if x in "RC" or y in "RC":
            old = records[i] if i < len(records) else None  # the original-path record
            i += 1
        if prefix:
            if not path.startswith(prefix):
                continue
            path = path[len(prefix):]
            if old is not None:
                old = old[len(prefix):] if old.startswith(prefix) else None
        changed[path] = (x + y).strip() or "M"
        if renames is not None and old:
            renames[path] = old
    return changed


def get_git_context(project_dir: str) -> dict:
    ctx = {"is_git": False, "branch": None, "status_raw": "", "diff_stat": "",
           "recent_commits": "", "changed_files": {}, "git_file_list": None, "has_head": False,
           "renames": {}}
    probe = _git(project_dir, ["rev-parse", "--is-inside-work-tree"])
    if probe.problem == "not_repo":
        print("Git      : not a git repository (walking the filesystem)")
        return ctx
    if probe.problem:
        warn(f"git is unavailable here ({probe.problem}: {probe.err}); treating the folder as "
             "NON-git, so .gitignore is not applied and --only-changed cannot work")
        return ctx
    if probe.out.strip() != "true":
        return ctx
    ctx["is_git"] = True

    prefix = (run_git_cmd(project_dir, ["rev-parse", "--show-prefix"]) or "").rstrip("\r\n")
    branch = (run_git_cmd(project_dir, ["branch", "--show-current"], quiet=True) or "").strip()
    ctx["has_head"] = run_git_cmd(project_dir, ["rev-parse", "--verify", "--quiet", "HEAD"],
                                  quiet=True) is not None
    if not branch:
        short = (run_git_cmd(project_dir, ["rev-parse", "--short", "HEAD"], quiet=True) or "").strip()
        branch = f"(detached at {short})" if short else "(no commits yet)"
    ctx["branch"] = branch

    raw = run_git_cmd(project_dir, ["status", "--porcelain=v1", "-z", "-uall", "--", "."])
    if raw is not None:
        changed = parse_porcelain_z(raw, prefix, ctx["renames"])
        ctx["changed_files"] = changed
        ctx["status_raw"] = "\n".join(f"{code:<2} {p}" for p, code in sorted(changed.items()))
    if ctx["has_head"]:
        ctx["diff_stat"] = (run_git_cmd(project_dir, ["diff", "--stat", "--no-color", "--relative",
                                                      "HEAD", "--", "."]) or "").rstrip()
        ctx["recent_commits"] = (run_git_cmd(project_dir, ["log", "-n", "5", "--oneline",
                                                           "--no-color"]) or "").rstrip()
    ls = run_git_cmd(project_dir, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    if ls is not None:
        ctx["git_file_list"] = list(dict.fromkeys(p.replace("\\", "/") for p in ls.split("\0") if p))
    else:
        warn("could not list files through git; falling back to a plain directory walk")
    return ctx


def get_file_diff(project_dir: str, rel: str, old: str | None = None) -> str | None:
    paths = [rel] + ([old] if old else [])  # include the old path so renames diff as renames
    out = run_git_cmd(project_dir, ["--literal-pathspecs", "diff", "--no-color", "--no-ext-diff",
                                    "--relative", "-M", "-U3", "HEAD", "--"] + paths, quiet=True)
    if not out or not out.strip():
        return None
    lines = out.splitlines()
    text = "\n".join(lines[:DIFF_CAP_LINES])
    omitted = len(lines) - min(len(lines), DIFF_CAP_LINES)
    if len(text) > DIFF_CAP_CHARS:
        text = text[:DIFF_CAP_CHARS]
        text = text[:text.rfind("\n")] if "\n" in text else text
        omitted = len(lines) - len(text.splitlines())
    if omitted > 0:
        text += f"\n... [diff truncated: {omitted} more lines]"
    return text


# --------------------------------------------------------------------------- #
# Secret redaction
# --------------------------------------------------------------------------- #

_KEYWORDS = (r"(?:password|passwd|pwd|secret|token|api[_\-]?key|apikey|access[_\-]?key|"
             r"private[_\-]?key|secret[_\-]?key)")
_PRIVATE_KEY_FULL = re.compile(
    r"-----BEGIN ([A-Z ]*)PRIVATE KEY(?: BLOCK)?-----.{0,16000}?-----END \1PRIVATE KEY(?: BLOCK)?-----",
    re.DOTALL)
_PRIVATE_KEY_HEAD = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----(?:[ \t]*\r?\n[ \t]*[A-Za-z0-9+/=]{20,}){1,100}")
_TOKEN_PATTERNS = [
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWT
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),                                          # AWS
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),                                             # Google
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),                                         # GitHub
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),                                        # Slack
    re.compile(r"\b[sr]k_(?:live|test)_[0-9a-zA-Z]{16,}\b"),                               # Stripe
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}"),                                # OpenAI/Anthropic
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]{20,}=*"),
    re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),                                        # URL creds
]
_ASSIGN_QUOTED = re.compile(
    r"(?i)(\b[\w.\-]*?" + _KEYWORDS + r"[\"']?\s*(?::|=|=>)\s*)([\"'])([^\"'\r\n]{4,}?)\2")
_ASSIGN_UNQUOTED = re.compile(
    r"(?im)^(\s*(?:export\s+)?[\w.\-]*?" + _KEYWORDS + r"\s*[:=]\s*)([^\s#\"'][^\s#]{3,})([ \t]*(?:#.*)?)$")


def _is_reference(value: str) -> bool:
    """Values that merely reference another variable/template are not secrets."""
    return (value.startswith(("$", "%", "<", "!", "{{", "${")) or "${" in value or "{{" in value
            or "[REDACTED]" in value  # already replaced by a token pattern: don't double count
            or re.fullmatch(r"[*xX\u2022.]+", value) is not None)


def redact_secrets(text: str, filename: str = "") -> tuple[str, int]:
    """Replace likely secrets with [REDACTED]. Returns (new_text, replacements)."""
    count = 0

    def plain(rx, text):
        nonlocal count
        text, n = rx.subn("[REDACTED]", text)
        count += n
        return text

    text = plain(_PRIVATE_KEY_FULL, text)
    text = plain(_PRIVATE_KEY_HEAD, text)
    for rx in _TOKEN_PATTERNS:
        text = plain(rx, text)

    def quoted(m):
        nonlocal count
        if _is_reference(m.group(3)):
            return m.group(0)
        count += 1
        return f"{m.group(1)}{m.group(2)}[REDACTED]{m.group(2)}"

    text = _ASSIGN_QUOTED.sub(quoted, text)

    ext = os.path.splitext(filename)[1].lower()
    if ext in CONFIG_LIKE_EXTS or filename.lower().startswith(".env"):
        def unquoted(m):
            nonlocal count
            if _is_reference(m.group(2)) or "(" in m.group(2):
                return m.group(0)
            count += 1
            return f"{m.group(1)}[REDACTED]{m.group(3)}"
        text = _ASSIGN_UNQUOTED.sub(unquoted, text)
    return text, count


# --------------------------------------------------------------------------- #
# Text helpers / token counting
# --------------------------------------------------------------------------- #

def compress_whitespace(content: str) -> str:
    """Strip trailing spaces, collapse 3+ blank lines to 1, trim leading/trailing blanks."""
    lines = [line.rstrip() for line in content.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


class TokenCounter:
    """tiktoken when available, otherwise len(text)/4."""

    def __init__(self) -> None:
        self.name = "approx(len/4)"
        self._enc = None
        try:
            import tiktoken  # type: ignore
            for enc_name in ("o200k_base", "cl100k_base"):
                try:
                    self._enc = tiktoken.get_encoding(enc_name)
                    self.name = f"tiktoken:{enc_name}"
                    break
                except Exception:
                    continue
        except Exception:
            pass

    def __call__(self, text: str) -> int:
        if not text:
            return 0
        if self._enc is not None:
            try:
                return len(self._enc.encode(text, disallowed_special=()))
            except Exception:
                pass
        return max(1, len(text) // 4)


def truncate_to_tokens(content: str, room: int, counter) -> tuple[str, int] | None:
    """Longest line-prefix of content that fits `room` tokens. Returns (head, omitted_lines)."""
    lines = content.split("\n")
    lo, hi, best = 1, len(lines) - 1, 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if counter("\n".join(lines[:mid])) + 12 <= room:  # +12: truncation marker
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    if best == 0:
        return None
    return "\n".join(lines[:best]), len(lines) - best


def lang_name(filename: str, ext: str) -> str:
    if filename.lower().startswith(".env"):
        return "Env"
    if ext in LANG_NAMES:
        return LANG_NAMES[ext]
    if not ext:
        return filename
    return ext.lstrip(".").upper()


def lang_id(filename: str, ext: str) -> str:
    if filename.lower().startswith(".env"):
        return "env"
    return ext.lstrip(".") if ext else filename.lower()


# --------------------------------------------------------------------------- #
# Symbol outlines
# --------------------------------------------------------------------------- #

def _short(text: str, limit: int = 140) -> str:
    text = text.rstrip()
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _decl_text(line: str, prev: str) -> str:
    indent = line[:len(line) - len(line.lstrip())]
    t = re.sub(r"\s*\{\s*$", "", line.strip())
    if prev and not t.startswith("@") and re.fullmatch(r"@[\w.]+(?:\(.*\))?", prev):
        t = f"{prev} {t}"
    return _short(indent + t)


def _safe_unparse(node) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "..."


def _py_sig(node) -> str:
    args = _short(_safe_unparse(node.args), 90)
    ret = f" -> {_short(_safe_unparse(node.returns), 40)}" if node.returns is not None else ""
    kw = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{kw} {node.name}({args}){ret}"


def _py_deco(node) -> str:
    decos = getattr(node, "decorator_list", [])
    return _short("@" + _safe_unparse(decos[0]), 50) + " " if decos else ""


_PY_RE = re.compile(r"^(\s*)(async\s+def|def|class)\s+\w+.*")


def _outline_python(content: str) -> list[str]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(content)
    except Exception:
        return [f"L{n} {_short(l.rstrip(':').rstrip())}" for n, l in enumerate(content.split("\n"), 1)
                if _PY_RE.match(l) and len(l) - len(l.lstrip()) <= 4]
    out: list[str] = []
    funcs = (ast.FunctionDef, ast.AsyncFunctionDef)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = _short(", ".join(_safe_unparse(b) for b in node.bases), 60)
            out.append(f"L{node.lineno} {_py_deco(node)}class {node.name}" + (f"({bases})" if bases else ""))
            for ch in node.body:
                if isinstance(ch, funcs):
                    out.append(f"  L{ch.lineno} {_py_deco(ch)}{_py_sig(ch)}")
                elif isinstance(ch, ast.ClassDef):
                    out.append(f"  L{ch.lineno} class {ch.name}")
        elif isinstance(node, funcs):
            out.append(f"L{node.lineno} {_py_deco(node)}{_py_sig(node)}")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name) and t.id.isupper()]
            if names:
                out.append(f"L{node.lineno} {', '.join(names)}")
        elif isinstance(node, ast.If) and "__name__" in _safe_unparse(node.test):
            out.append(f"L{node.lineno} if __name__ == '__main__'")
    return out


_JS_DECL = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
    r"(?:function\b|class\b|interface\b|enum\b|namespace\b|const\s+enum\b|type\s+\w+\s*[=<])")
_JS_VAR = re.compile(r"^(?:export\s+)?(?:default\s+)?(?:const|let|var)\s+[\w$]+\s*(?::[^=]+)?=\s*(.*)$")
_JS_VALUE_FN = re.compile(
    r"^(?:async\s+)?(?:\(|function\b|[\w$]+\s*=>|React\.\w+\(|"
    r"(?:memo|forwardRef|createContext|createSlice|createApi|createStore|createAsyncThunk|styled|connect|observer)\b)")
_JS_EXPORT = re.compile(r"^(?:export\s+(?:default\b|\{|\*)|module\.exports\b|exports\.[\w$]+\s*=)")
_JS_METHOD = re.compile(
    r"^(?: {2}|\t)(?:(?:public|private|protected|static|async|override|readonly|get|set|abstract)\s+)*"
    r"([\w$#]+)\s*(?:<[^>]*>)?\(.*\)\s*(?::\s*[^={;]+)?\s*\{\s*$")
_JS_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "function", "else", "with",
                "do", "try", "await", "new", "typeof", "throw"}


def _outline_js(lines: list[str]) -> list[str]:
    out, in_class, prev = [], False, ""
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        sym = None
        if line[0] not in " \t":
            if line.startswith("}"):
                in_class = False
            elif _JS_DECL.match(line) or _JS_EXPORT.match(line):
                sym = line
                in_class = re.search(r"\bclass\b", line) is not None
            else:
                m = _JS_VAR.match(line)
                if m and (line.startswith("export") or _JS_VALUE_FN.match(m.group(1))):
                    sym = line
        elif in_class:
            m = _JS_METHOD.match(line)
            if m and m.group(1) not in _JS_KEYWORDS:
                sym = line
        if sym is not None:
            out.append(f"L{n} {_decl_text(sym, prev)}")
        prev = line.strip()
    return out


_DART_T = r"(?:Future<[^>]*>|Future|Stream<[^>]*>|FutureOr<[^>]*>|void|int|double|bool|String|num|dynamic|[A-Z]\w*(?:<[^>]*>)?\??)"
_DART_TYPE = re.compile(r"^(?:(?:abstract|base|final|sealed|interface|mixin|augment)\s+)*(?:class|mixin|enum|extension|typedef)\s+\w*")
_DART_CLASSY = re.compile(r"^(?:(?:abstract|base|final|sealed|interface|mixin)\s+)*(?:class|mixin|extension)\b")
_DART_FUNC = re.compile(r"^" + _DART_T + r"\s+_?\w+\s*(?:<[^>]*>)?\(")
_DART_MEMBER = re.compile(
    r"^ {2}(?:static\s+)?(?:" + _DART_T + r"\s+(?:get\s+)?_?\w+\s*(?:<[^>]*>)?\(|(?:get|set)\s+\w+|"
    r"(?:const\s+|factory\s+)?[A-Z]\w*(?:\.\w+)?\()")


def _outline_dart(lines: list[str]) -> list[str]:
    out, in_class = [], False
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        sym = None
        if line[0] not in " \t":
            if line.startswith("}"):
                in_class = False
            elif _DART_TYPE.match(line):
                sym = line
                in_class = _DART_CLASSY.match(line) is not None
            elif _DART_FUNC.match(line):
                sym = line
        elif in_class and _DART_MEMBER.match(line):
            sym = line
        if sym is not None:
            out.append(f"L{n} {_decl_text(sym, '')}")
    return out


_KT_DECL = re.compile(
    r"^( {0,4})(?:(?:public|private|internal|protected|open|abstract|sealed|data|enum|annotation|inline|value|"
    r"suspend|override|operator|infix|const|lateinit|companion|expect|actual|tailrec|external|fun)\s+)*"
    r"(?:class|interface|object|fun|typealias)\b")
_KT_VAL = re.compile(r"^(?:(?:public|private|internal|const|lateinit)\s+)*(?:val|var)\s+\w+")


def _outline_kotlin(lines: list[str]) -> list[str]:
    out, prev = [], ""
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        if _KT_DECL.match(line) or (line[0] not in " \t" and _KT_VAL.match(line)):
            out.append(f"L{n} {_decl_text(line, prev)}")
        prev = line.strip()
    return out


_GO_RE = re.compile(r"^(?:func\b|type\s+\w+\s+(?:struct|interface)\b)")
_RS_RE = re.compile(r"^\s{0,4}(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:fn|struct|enum|trait|impl|mod|type)\b")
_MD_RE = re.compile(r"^#{1,4}\s+\S")


def _outline_simple(lines: list[str], rx) -> list[str]:
    return [f"L{n} {_decl_text(l, '')}" for n, l in enumerate(lines, 1) if rx.match(l)]


def build_outline(ext: str, content: str) -> list[str]:
    """Symbol outline for a file (Python via ast, others via regex). Empty list if unsupported."""
    try:
        if ext in (".py", ".pyi"):
            out = _outline_python(content)
        elif ext in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"):
            out = _outline_js(content.split("\n"))
        elif ext == ".dart":
            out = _outline_dart(content.split("\n"))
        elif ext in (".kt", ".kts"):
            out = _outline_kotlin(content.split("\n"))
        elif ext == ".go":
            out = _outline_simple(content.split("\n"), _GO_RE)
        elif ext == ".rs":
            out = _outline_simple(content.split("\n"), _RS_RE)
        elif ext in (".md", ".markdown", ".mdx"):
            out = _outline_simple(content.split("\n"), _MD_RE)
        else:
            return []
    except Exception:
        return []
    if len(out) > OUTLINE_MAX_LINES:
        out = out[:OUTLINE_MAX_LINES] + [f"... (+{len(out) - OUTLINE_MAX_LINES} more symbols)"]
    return out


# --------------------------------------------------------------------------- #
# Stack profiles & tier rules
# --------------------------------------------------------------------------- #

_NODE_ENTRY = ([f"{d}{stem}.{ext}" for d in ("", "src/") for stem in ("index", "main", "App", "app", "server")
                for ext in ("js", "jsx", "ts", "tsx", "mjs", "cjs")]
               + ["app/_layout.tsx", "app/layout.tsx", "app/page.tsx", "src/app/layout.tsx",
                  "src/app/page.tsx", "pages/_app.tsx", "pages/index.tsx"])

# Tier meaning: 0 focus | 1 uncommitted git changes | 2 core docs & entry points |
# 3 domain/types/schema | 4 core logic & data | 5 UI / other source | 6 utils/theme |
# 7 config & manifests | 8 everything else (docs, tests, misc)
PROFILES: dict[str, dict] = {
    "node": {
        "label": "Node / JS / TS",
        "source_exts": {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"},
        "entry": _NODE_ENTRY,
        "tiers": {
            3: ["src/domain/**", "src/types/**", "src/models/**", "src/entities/**", "src/schema/**",
                "types/**", "models/**", "**/schema.ts", "**/schema.js", "**/*.schema.ts",
                "**/types.ts", "prisma/schema.prisma"],
            4: ["src/services/**", "src/database/**", "src/db/**", "src/hooks/**", "src/api/**",
                "src/store/**", "src/state/**", "src/lib/**", "src/core/**", "src/routes/**",
                "src/controllers/**", "src/middleware/**", "server/**", "api/**", "app/api/**",
                "src/app/api/**", "routes/**", "controllers/**", "lib/**"],
            5: ["src/screens/**", "src/components/**", "src/pages/**", "src/views/**", "src/app/**",
                "app/**", "pages/**", "components/**"],
            6: ["src/utils/**", "src/theme/**", "src/context/**", "src/helpers/**", "src/constants/**",
                "src/styles/**", "utils/**", "constants/**"],
            7: ["package.json", "tsconfig*.json", "jsconfig.json", "app.json", "app.config.*", "eas.json",
                "vite.config.*", "next.config.*", "webpack.config.*", "babel.config.*", "metro.config.*",
                "tailwind.config.*", "eslint.config.*", ".eslintrc*", "nest-cli.json"],
        },
    },
    "python": {
        "label": "Python",
        "source_exts": {".py", ".pyi"},
        "entry": ["main.py", "app.py", "manage.py", "wsgi.py", "asgi.py", "run.py", "__main__.py",
                  "src/main.py", "src/app.py", "app/main.py", "*/__main__.py", "src/*/__main__.py",
                  "src/*/main.py", "*/main.py"],
        "tiers": {
            3: ["**/models.py", "**/models/**", "**/schemas.py", "**/schemas/**", "**/schema.py",
                "**/types.py", "**/entities/**", "**/domain/**", "**/enums.py"],
            4: ["**/services/**", "**/service.py", "**/api/**", "**/routes/**", "**/routers/**",
                "**/core/**", "**/db/**", "**/repositories/**", "**/views.py", "**/tasks/**",
                "**/handlers/**"],
            5: ["**/cli.py", "**/commands/**", "**/templates/**", "**/ui/**", "**/gui/**", "**/views/**"],
            6: ["**/utils/**", "**/utils.py", "**/helpers/**", "**/config.py", "**/settings.py",
                "**/constants.py"],
            7: ["pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "tox.ini", "requirements*.txt"],
        },
    },
    "dart": {
        "label": "Dart / Flutter",
        "source_exts": {".dart"},
        "entry": ["lib/main.dart", "bin/main.dart"],
        "tiers": {
            3: ["lib/models/**", "lib/domain/**", "lib/entities/**", "lib/data/**"],
            4: ["lib/services/**", "lib/repositories/**", "lib/providers/**", "lib/bloc/**",
                "lib/blocs/**", "lib/cubit/**", "lib/controllers/**", "lib/core/**"],
            5: ["lib/screens/**", "lib/pages/**", "lib/widgets/**", "lib/views/**", "lib/ui/**"],
            6: ["lib/utils/**", "lib/theme/**", "lib/constants/**", "lib/l10n/**"],
            7: ["pubspec.yaml", "analysis_options.yaml"],
        },
    },
    "kotlin": {
        "label": "Kotlin / Java / Gradle",
        "source_exts": {".kt", ".java"},
        "entry": ["**/MainActivity.kt", "**/MainActivity.java", "**/Main.kt", "**/*Application.kt"],
        "tiers": {
            3: ["**/domain/**", "**/model/**", "**/models/**", "**/entity/**", "**/entities/**", "**/data/**"],
            4: ["**/repository/**", "**/repositories/**", "**/service/**", "**/services/**",
                "**/viewmodel/**", "**/usecase/**", "**/di/**", "**/network/**", "**/db/**", "**/database/**"],
            5: ["**/ui/**", "**/screens/**", "**/compose/**", "**/res/layout/**", "**/res/navigation/**"],
            6: ["**/util/**", "**/utils/**", "**/theme/**", "**/res/values/**"],
            7: ["build.gradle*", "settings.gradle*", "gradle.properties", "**/build.gradle*",
                "**/AndroidManifest.xml", "gradle/libs.versions.toml", "**/proguard-rules.pro"],
        },
    },
    "go": {
        "label": "Go",
        "source_exts": {".go"},
        "entry": ["main.go", "cmd/*/main.go"],
        "tiers": {
            3: ["internal/domain/**", "internal/models/**", "pkg/models/**", "**/models/**", "**/types.go"],
            4: ["internal/**", "pkg/**", "cmd/**", "**/handlers/**", "**/store/**"],
            5: ["**/web/**", "**/ui/**", "**/templates/**"],
            6: ["**/util/**", "**/utils/**", "**/config/**"],
            7: ["go.mod"],
        },
    },
    "rust": {
        "label": "Rust",
        "source_exts": {".rs"},
        "entry": ["src/main.rs", "src/lib.rs", "src/bin/*.rs"],
        "tiers": {
            3: ["src/models/**", "src/types.rs", "src/domain/**"],
            4: ["src/services/**", "src/db/**", "src/api/**", "src/core/**"],
            5: ["src/ui/**", "src/handlers/**", "src/routes/**"],
            6: ["src/utils/**", "src/config*"],
            7: ["Cargo.toml", "build.rs", "rust-toolchain*"],
        },
    },
}
GENERIC_TIERS = {
    7: ["**/Dockerfile*", "**/docker-compose*.y*ml", "**/Makefile", "**/CMakeLists.txt", "**/Procfile",
        "**/Jenkinsfile", "**/Gemfile", ".github/workflows/**", "**/requirements*.txt",
        ".env.example", ".env.sample", ".env.template"],
}
MANIFEST_STACKS = {
    "package.json": "node", "pyproject.toml": "python", "setup.py": "python", "setup.cfg": "python",
    "pipfile": "python", "pubspec.yaml": "dart", "build.gradle": "kotlin", "build.gradle.kts": "kotlin",
    "settings.gradle": "kotlin", "settings.gradle.kts": "kotlin", "go.mod": "go", "cargo.toml": "rust",
}


def stack_for_manifest(filename: str) -> str | None:
    low = filename.lower()
    if low in MANIFEST_STACKS:
        return MANIFEST_STACKS[low]
    if re.fullmatch(r"requirements[\w.\-]*\.txt", low):
        return "python"
    return None


def detect_stacks(paths: list[str], is_dir_excluded) -> list[dict]:
    """Find stack manifests at the project root and up to two directories deep."""
    found: dict[tuple[str, str], list[str]] = {}
    for rel in paths:
        if rel.count("/") > 2 or is_dir_excluded(rel):
            continue
        name = stack_for_manifest(rel.rsplit("/", 1)[-1])
        if name:
            root = rel.rsplit("/", 1)[0] if "/" in rel else ""
            found.setdefault((name, root), []).append(rel)
    stacks = [{"name": n, "root": r, "manifests": sorted(m)} for (n, r), m in found.items()]
    stacks.sort(key=lambda s: (s["root"] != "", s["root"].count("/"), s["root"], s["name"]))
    return stacks[:16]


def is_requirements_file(filename: str) -> bool:
    return re.fullmatch(r"requirements[\w.\-]*\.txt", filename, re.IGNORECASE) is not None


def is_known_extensionless(filename: str) -> bool:
    low = filename.lower()
    return low in KNOWN_EXTENSIONLESS or low.startswith(("dockerfile.", "containerfile."))


def is_secret_file(filename: str) -> bool:
    """Blocks .env.* and credential files while allowing .env.example templates."""
    if filename.lower() in ALLOWED_ENV_FILES:
        return False
    return any(p.match(filename) for p in SECRET_FILENAME_PATTERNS)


def is_test_path(parts: list[str]) -> bool:
    return bool({p.lower() for p in parts[:-1]} & TEST_DIRS) or any(p.search(parts[-1]) for p in TEST_FILE_PATTERNS)


class TierResolver:
    """Assigns the priority tier (0 = most important) to a project-relative path."""

    def __init__(self, stacks, cfg: ProjectConfig, focus_terms, git_changed, entry_points):
        def compiled(patterns):
            return [rx for rx in (compile_glob(p) for p in patterns) if rx is not None]

        self.focus_terms = focus_terms
        self.git_changed = git_changed
        self.entry_points = entry_points
        self.user_rules = [(t, rx) for t in sorted(cfg.tiers) for rx in compiled(cfg.tiers[t])]
        self.stack_rules = []
        for st in stacks:
            prof = PROFILES[st["name"]]
            rules = [(t, rx) for t, pats in prof["tiers"].items() for rx in compiled(pats)]
            self.stack_rules.append((st["root"], rules, prof["source_exts"]))
        self.generic_rules = [(t, rx) for t, pats in GENERIC_TIERS.items() for rx in compiled(pats)]
        self.core_doc_patterns = [p.lower() for p in CORE_DOC_DEFAULTS + cfg.core_docs]
        self.roots = {""} | {s["root"] for s in stacks}

    def is_core_doc(self, rel: str) -> bool:
        base, low = rel.rsplit("/", 1)[-1].lower(), rel.lower()
        return any(fnmatch.fnmatchcase(base, p) or fnmatch.fnmatchcase(low, p) for p in self.core_doc_patterns)

    def tier(self, rel: str, parts: list[str]) -> int:
        low = rel.lower()
        if self.focus_terms and any(t in low for t in self.focus_terms):
            return 0
        if rel in self.git_changed:
            return 1
        for t, rx in self.user_rules:
            if rx.match(rel):
                return t
        if is_test_path(parts):
            return 8
        if rel in self.entry_points:
            return 2
        if self.is_core_doc(rel):
            return 2 if posixpath.dirname(rel) in self.roots else 7
        best, ext = None, os.path.splitext(rel)[1].lower()
        in_source_stack = False
        for root, rules, src_exts in self.stack_rules:
            if root:
                if not rel.startswith(root + "/"):
                    continue
                sub = rel[len(root) + 1:]
            else:
                sub = rel
            if ext in src_exts:
                in_source_stack = True
            for t, rx in rules:
                if (best is None or t < best) and rx.match(sub):
                    best = t
        for t, rx in self.generic_rules:
            if (best is None or t < best) and rx.match(rel):
                best = t
        if best is None:
            return 5 if in_source_stack else 8
        return best


# --------------------------------------------------------------------------- #
# Manifest parsing / overview
# --------------------------------------------------------------------------- #

NODE_FRAMEWORKS = [("react-native", "React Native"), ("expo", "Expo"), ("next", "Next.js"), ("react", "React"),
                   ("vue", "Vue"), ("nuxt", "Nuxt"), ("svelte", "Svelte"), ("@angular/core", "Angular"),
                   ("express", "Express"), ("fastify", "Fastify"), ("@nestjs/core", "NestJS"),
                   ("electron", "Electron"), ("vite", "Vite"), ("typescript", "TypeScript"),
                   ("tailwindcss", "Tailwind CSS"), ("prisma", "Prisma"), ("drizzle-orm", "Drizzle")]
PY_FRAMEWORKS = [("django", "Django"), ("flask", "Flask"), ("fastapi", "FastAPI"), ("pytest", "pytest"),
                 ("numpy", "NumPy"), ("pandas", "pandas"), ("torch", "PyTorch"), ("tensorflow", "TensorFlow"),
                 ("sqlalchemy", "SQLAlchemy"), ("pydantic", "Pydantic"), ("celery", "Celery"),
                 ("streamlit", "Streamlit"), ("click", "Click"), ("typer", "Typer")]


def _frameworks(dep_names, table) -> list[str]:
    names = {n.lower().replace("_", "-") for n in dep_names}
    return [label for key, label in table if key in names]


def _norm_join(root: str, p: str) -> str:
    p = p.replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    return posixpath.normpath(posixpath.join(root, p)) if root else posixpath.normpath(p)


def _req_line(spec: str) -> tuple[str, str]:
    spec = spec.split("#", 1)[0].strip()
    if "://" in spec:
        return (spec.split("#egg=")[-1] if "#egg=" in spec else spec[:60]), "url"
    m = re.match(r"([A-Za-z0-9_.\-]+)\s*(?:\[[^\]]*\])?\s*([^;]*)", spec)
    if not m:
        return spec[:60], "*"
    return m.group(1), m.group(2).strip() or "*"


def parse_package_json(text: str, root: str) -> dict:
    data = json.loads(text)
    deps = [(k, str(v), False) for k, v in (data.get("dependencies") or {}).items()]
    deps += [(k, str(v), True) for k, v in (data.get("devDependencies") or {}).items()]
    entries = [_norm_join(root, data[k]) for k in ("main", "module") if isinstance(data.get(k), str)]
    b = data.get("bin")
    if isinstance(b, str):
        entries.append(_norm_join(root, b))
    elif isinstance(b, dict):
        entries += [_norm_join(root, v) for v in b.values() if isinstance(v, str)]
    return {"name": data.get("name"), "version": data.get("version"), "deps": deps,
            "scripts": [(k, str(v)) for k, v in (data.get("scripts") or {}).items()],
            "entries": entries, "frameworks": _frameworks([d[0] for d in deps], NODE_FRAMEWORKS)}


def parse_requirements(text: str, root: str) -> dict:
    deps = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        name, ver = _req_line(line)
        deps.append((name, ver, False))
    return {"deps": deps, "scripts": [], "entries": [],
            "frameworks": _frameworks([d[0] for d in deps], PY_FRAMEWORKS)}


def parse_pyproject(text: str, root: str, files: set[str]) -> dict:
    data = parse_toml(text)
    proj = data.get("project") if isinstance(data.get("project"), dict) else {}
    tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
    poetry = tool.get("poetry") if isinstance(tool.get("poetry"), dict) else {}
    deps = []
    for s in proj.get("dependencies", []) or []:
        if isinstance(s, str):
            deps.append((*_req_line(s), False))
    for grp in (proj.get("optional-dependencies") or {}).values():
        deps += [(*_req_line(s), True) for s in (grp if isinstance(grp, list) else []) if isinstance(s, str)]
    for grp in (data.get("dependency-groups") or {}).values():
        deps += [(*_req_line(s), True) for s in (grp if isinstance(grp, list) else []) if isinstance(s, str)]

    def poetry_deps(table, dev):
        for k, v in (table or {}).items():
            if k.lower() == "python":
                continue
            ver = v if isinstance(v, str) else (v.get("version", "*") if isinstance(v, dict) else "*")
            deps.append((k, ver, dev))

    poetry_deps(poetry.get("dependencies"), False)
    poetry_deps(poetry.get("dev-dependencies"), True)
    for grp in (poetry.get("group") or {}).values():
        if isinstance(grp, dict):
            poetry_deps(grp.get("dependencies"), True)
    scripts: list[tuple[str, str]] = []
    for table in (proj.get("scripts"), poetry.get("scripts")):
        if isinstance(table, dict):
            scripts += [(k, str(v)) for k, v in table.items()]
    entries = []
    for _, target in scripts:
        mod = target.split(":")[0].strip().split(" ")[0]
        base = mod.replace(".", "/")
        for cand in (f"{base}.py", f"src/{base}.py", f"{base}/__init__.py", f"src/{base}/__init__.py"):
            path = _norm_join(root, cand)
            if path in files:
                entries.append(path)
                break
    return {"name": proj.get("name") or poetry.get("name"),
            "version": proj.get("version") or poetry.get("version"), "deps": deps,
            "scripts": scripts, "entries": entries,
            "frameworks": _frameworks([d[0] for d in deps], PY_FRAMEWORKS)}


def parse_pipfile(text: str, root: str) -> dict:
    data = parse_toml(text)
    deps = []
    for key, dev in (("packages", False), ("dev-packages", True)):
        for k, v in (data.get(key) or {}).items():
            deps.append((k, v if isinstance(v, str) else (v.get("version", "*") if isinstance(v, dict) else "*"), dev))
    return {"deps": deps, "scripts": [], "entries": [],
            "frameworks": _frameworks([d[0] for d in deps], PY_FRAMEWORKS)}


def parse_pubspec(text: str, root: str) -> dict:
    name = version = None
    deps: list[tuple[str, str, bool]] = []
    section, pending = "", None
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        top = re.match(r"^([\w_]+):\s*(.*)$", line)
        if top:
            section, val = top.group(1), top.group(2).strip().strip("'\"")
            pending = None
            if section == "name":
                name = val
            elif section == "version":
                version = val
            continue
        if section in ("dependencies", "dev_dependencies"):
            d2 = re.match(r"^  ([\w.\-]+):\s*(.*)$", line)
            if d2:
                val = d2.group(2).strip().strip("'\"")
                deps.append([d2.group(1), val or "", section == "dev_dependencies"])
                pending = deps[-1] if not val else None
                continue
            d3 = re.match(r"^    ([\w]+):\s*(.+)$", line)
            if d3 and pending is not None:
                pending[1] = f"{d3.group(1)}:{d3.group(2).strip()}"
    deps = [(n, v or "*", dev) for n, v, dev in deps]
    fw = ["Flutter"] if any(n == "flutter" for n, _, _ in deps) else []
    return {"name": name, "version": version, "deps": deps, "scripts": [], "entries": [], "frameworks": fw}


_GRADLE_CFG = r"(?:implementation|api|compileOnly|runtimeOnly|testImplementation|androidTestImplementation|kapt|ksp|annotationProcessor|classpath)"


def parse_gradle(text: str, root: str) -> dict:
    deps = []
    for m in re.finditer(_GRADLE_CFG + r"""\s*\(?\s*["']([^"']+)["']""", text):
        parts = m.group(1).split(":")
        deps.append((":".join(parts[:2]), parts[2] if len(parts) > 2 else "*", "test" in m.group(0).lower()))
    for m in re.finditer(_GRADLE_CFG + r"\s*\(?\s*(libs\.[\w.]+)", text):
        deps.append((m.group(1), "(version catalog)", False))
    fw = []
    for needle, label in (("com.android.application", "Android"), ("com.android.library", "Android library"),
                          ("kotlin", "Kotlin"), ("compose", "Jetpack Compose"), ("hilt", "Hilt")):
        if needle in text:
            fw.append(label)
    return {"deps": list(dict.fromkeys(deps)), "scripts": [], "entries": [], "frameworks": fw}


def parse_gomod(text: str, root: str) -> dict:
    mod = re.search(r"^module\s+(\S+)", text, re.M)
    go = re.search(r"^go\s+(\S+)", text, re.M)
    deps = []
    for m in re.finditer(r"^\s*(?:require\s+)?([\w.\-]+\.[\w.\-]+/[^\s]+)\s+(v[^\s]+)(\s*//\s*indirect)?",
                         text, re.M):
        deps.append((m.group(1), m.group(2), bool(m.group(3))))
    return {"name": mod.group(1) if mod else None, "version": f"go {go.group(1)}" if go else None,
            "deps": deps, "scripts": [], "entries": [], "frameworks": ["Go"]}


def parse_cargo(text: str, root: str) -> dict:
    data = parse_toml(text)
    pkg = data.get("package") if isinstance(data.get("package"), dict) else {}
    deps = []
    for key, dev in (("dependencies", False), ("dev-dependencies", True), ("build-dependencies", True)):
        for k, v in (data.get(key) or {}).items():
            deps.append((k, v if isinstance(v, str) else (v.get("version", "*") if isinstance(v, dict) else "*"), dev))
    scripts, entries = [("run", "cargo run")], []
    for b in data.get("bin", []) or []:
        if isinstance(b, dict):
            if b.get("name"):
                scripts.append((b["name"], f"cargo run --bin {b['name']}"))
            if b.get("path"):
                entries.append(_norm_join(root, b["path"]))
    return {"name": pkg.get("name"), "version": pkg.get("version"), "deps": deps,
            "scripts": scripts, "entries": entries, "frameworks": ["Rust"]}


def _makefile_targets(text: str) -> list[tuple[str, str]]:
    out = []
    for m in re.finditer(r"^([A-Za-z0-9][\w.\-/]*)\s*:(?!=)", text, re.M):
        if m.group(1) not in [t for t, _ in out]:
            out.append((m.group(1), f"make {m.group(1)}"))
    return out[:25]


def _procfile_entries(text: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2).strip()) for m in re.finditer(r"^([\w\-]+):\s*(.+)$", text, re.M)]


def _read_text(path: str, limit: int = 1_000_000) -> str:
    with open(path, "rb") as fh:
        return fh.read(limit).decode("utf-8-sig", "replace")


def build_overview(project_dir: str, stacks: list[dict], paths: list[str]) -> dict:
    files = set(paths)
    manifests, frameworks, entries = [], [], []
    for st in stacks:
        for rel in st["manifests"]:
            base = rel.rsplit("/", 1)[-1]
            low = base.lower()
            try:
                text = _read_text(os.path.join(project_dir, rel))
                if low == "package.json":
                    info = parse_package_json(text, st["root"])
                elif is_requirements_file(base):
                    info = parse_requirements(text, st["root"])
                elif low == "pyproject.toml":
                    info = parse_pyproject(text, st["root"], files)
                elif low == "pipfile":
                    info = parse_pipfile(text, st["root"])
                elif low == "pubspec.yaml":
                    info = parse_pubspec(text, st["root"])
                elif low in ("build.gradle", "build.gradle.kts"):
                    info = parse_gradle(text, st["root"])
                elif low == "go.mod":
                    info = parse_gomod(text, st["root"])
                elif low == "cargo.toml":
                    info = parse_cargo(text, st["root"])
                else:
                    info = {"deps": [], "scripts": [], "entries": [], "frameworks": []}
            except Exception as exc:
                warn(f"could not parse manifest {rel}: {exc}")
                continue
            info["path"], info["stack"] = rel, st["name"]
            manifests.append(info)
            frameworks += info.get("frameworks", [])
            entries += [e for e in info.get("entries", []) if e in files]
        # conventional entry points for this stack
        rules = [rx for rx in (compile_glob(p) for p in PROFILES[st["name"]]["entry"]) if rx is not None]
        root = st["root"]
        for rel in paths:
            if root:
                if not rel.startswith(root + "/"):
                    continue
                sub = rel[len(root) + 1:]
            else:
                sub = rel
            if any(rx.match(sub) for rx in rules):
                entries.append(rel)
    for rel, parser in (("Makefile", _makefile_targets), ("Procfile", _procfile_entries)):
        if rel in files:
            try:
                scripts = parser(_read_text(os.path.join(project_dir, rel)))
                if scripts:
                    manifests.append({"path": rel, "stack": "build", "name": None, "version": None,
                                      "deps": [], "scripts": scripts, "entries": [], "frameworks": []})
            except Exception:
                pass
    primary = next((s["name"] for s in stacks if s["root"] == ""), stacks[0]["name"] if stacks else None)
    return {"stacks": stacks, "primary": primary, "frameworks": list(dict.fromkeys(frameworks)),
            "manifests": manifests, "entry_points": sorted(dict.fromkeys(entries)), "loc": {}}


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class FileEntry:
    path: str
    lang: str
    ext: str
    tier: int = 8
    git_status: str | None = None
    lines: int = 0
    tokens: int = 0
    content: str = ""
    outline: list = field(default_factory=list)
    outline_text: str = ""
    outline_tokens: int = 0
    outline_trimmed: str | None = None   # shortened outline used when the budget is tight
    outline_trimmed_tokens: int = 0
    diff: str | None = None
    diff_tokens: int = 0
    redactions: int = 0
    view: str = "full"          # full | truncated | outline | path
    shown: str | None = None    # truncated content
    omitted: int = 0

    @property
    def outline_out(self) -> str:
        """The outline text that is actually rendered."""
        return self.outline_text if self.outline_trimmed is None else self.outline_trimmed

    @property
    def outline_cost(self) -> int:
        return self.outline_tokens if self.outline_trimmed is None else self.outline_trimmed_tokens

    @property
    def base_cost(self) -> int:
        """Tokens spent on everything but the file body: outline plus diff."""
        return self.outline_cost + self.diff_tokens


@dataclass
class Pack:
    project_dir: str
    name: str
    timestamp: str
    entries: list
    excluded: list
    git: dict
    overview: dict
    tree: str
    counter: TokenCounter
    compress_ws: bool
    max_tokens: int | None
    only_changed: bool
    ignore_backend: str
    total_lines: int = 0
    redact: bool = True


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #

def collect_candidate_paths(project_dir: str, git_file_list, excluded: list, include_native: bool,
                            out_rel: str | None) -> list[str]:
    """Project-relative POSIX paths from `git ls-files` (respects .gitignore) or an os.walk."""
    if git_file_list is not None:
        return sorted(p for p in git_file_list if os.path.lexists(os.path.join(project_dir, p)))
    skip = DEPENDENCY_DIRS | GENERIC_BUILD_DIRS
    candidates: list[str] = []
    for dirpath, dirnames, filenames in os.walk(project_dir, topdown=True):
        rel_dir = os.path.relpath(dirpath, project_dir).replace("\\", "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        keep = []
        for d in sorted(dirnames):
            rel_d = f"{rel_dir}/{d}" if rel_dir else d
            if d in skip or (out_rel and rel_d == out_rel):
                excluded.append((rel_d + "/", "excluded_dir", "directory"))
            elif not rel_dir and d in NATIVE_DIRS and not include_native:
                excluded.append((rel_d + "/", "native_config_dir", "directory"))
            else:
                keep.append(d)
        dirnames[:] = keep
        for fn in sorted(filenames):
            candidates.append(f"{rel_dir}/{fn}" if rel_dir else fn)
    return candidates


def build_tree(included_files: list[str], git_changed: dict[str, str]) -> str:
    """Compact indented tree; modified/untracked files carry [M] / [??] markers."""
    lines, seen = [], set()
    for rel in sorted(included_files, key=lambda p: p.split("/")):
        parts = rel.split("/")
        for i in range(1, len(parts)):
            d = "/".join(parts[:i])
            if d not in seen:
                seen.add(d)
                lines.append("  " * (i - 1) + parts[i - 1] + "/")
        marker = f" [{git_changed[rel]}]" if rel in git_changed else ""
        lines.append("  " * (len(parts) - 1) + parts[-1] + marker)
    return "\n".join(lines)


def build_pack(project_dir: str, *, compress_ws: bool, include_tests: bool, include_docs: bool,
               include_native: bool, only_changed: bool, focus_raw: str | None, max_file_kb: int,
               max_tokens: int | None, redact: bool, verbose: bool, cfg: ProjectConfig,
               out_dir: str) -> Pack:
    name = os.path.basename(os.path.normpath(project_dir)) or "project"
    focus_terms = [t.strip().lower() for t in (focus_raw or "").split(",") if t.strip()]
    git = get_git_context(project_dir)
    if only_changed and not git["is_git"]:
        raise RuntimeError("--only-changed needs a git repository (git missing, failed, or not a repo)")
    changed: dict[str, str] = git["changed_files"]

    ignore_lines: list[str] = []
    ig_path = os.path.join(project_dir, IGNORE_FILENAME)
    if os.path.isfile(ig_path):
        try:
            ignore_lines += _read_text(ig_path).splitlines()
        except OSError as exc:
            warn(f"could not read {IGNORE_FILENAME}: {exc}")
    ignore_lines += cfg.extra_excludes
    spec = IgnoreSpec(ignore_lines)

    out_rel = None
    try:
        rel_out = os.path.relpath(os.path.abspath(out_dir), project_dir).replace("\\", "/")
        if not rel_out.startswith("..") and rel_out != ".":
            out_rel = rel_out
    except ValueError:
        pass

    excluded: list[tuple[str, str, str | None]] = []
    candidates = collect_candidate_paths(project_dir, git["git_file_list"], excluded, include_native, out_rel)
    dir_excl = DEPENDENCY_DIRS if git["git_file_list"] is not None else DEPENDENCY_DIRS | GENERIC_BUILD_DIRS

    def dir_excluded(rel: str) -> bool:
        parts = rel.split("/")[:-1]
        if any(d in dir_excl for d in parts):
            return True
        return bool(out_rel and (rel == out_rel or rel.startswith(out_rel + "/")))

    stacks = detect_stacks(candidates, dir_excluded)
    overview = build_overview(project_dir, stacks, candidates)
    resolver = TierResolver(stacks, cfg, focus_terms, changed, set(overview["entry_points"]))
    counter = TokenCounter()
    max_bytes = max_file_kb * 1024

    print(f"Scanning : {project_dir}")
    if stacks:
        print("Stack    : " + ", ".join(dict.fromkeys(PROFILES[s["name"]]["label"] for s in stacks)))
    if git["is_git"]:
        print(f"Git      : branch {git['branch']}, {len(changed)} uncommitted file(s)")
    if spec:
        print(f"Ignore   : {len(spec.lines)} rule(s) from {IGNORE_FILENAME}/extra_excludes ({spec.backend})")

    entries: list[FileEntry] = []

    def skip(rel: str, reason: str, detail: str | None = None) -> None:
        excluded.append((rel, reason, detail))

    for rel in candidates:
        full = os.path.join(project_dir, rel)
        parts = rel.split("/")
        filename, low = parts[-1], parts[-1].lower()
        ext = os.path.splitext(filename)[1].lower()

        if os.path.islink(full):
            skip(rel, "symlink"); continue
        if not os.path.isfile(full):
            continue
        if dir_excluded(rel):
            skip(rel, "excluded_dir"); continue
        if not include_native and len(parts) > 1 and parts[0] in NATIVE_DIRS:
            skip(rel, "native_config_dir"); continue
        if only_changed and rel not in changed:
            skip(rel, "not_changed"); continue
        if spec and spec.match(rel):
            skip(rel, "docignore"); continue
        if filename in EXCLUDED_FILES:
            skip(rel, "excluded_filename"); continue
        if is_secret_file(filename):
            skip(rel, "secret_file"); continue
        if low.endswith(EXCLUDED_SUFFIXES) or (cfg.exclude_dts and low.endswith(DTS_SUFFIX)):
            skip(rel, "excluded_extension", "compound suffix"); continue
        if ext in EXCLUDED_EXTENSIONS:
            skip(rel, "excluded_extension", ext); continue
        if not ext and not is_known_extensionless(filename):
            skip(rel, "no_extension"); continue
        if not include_tests and is_test_path(parts):
            skip(rel, "test_file"); continue
        if ext in DOC_EXTENSIONS:
            keep = (resolver.is_core_doc(rel) or is_requirements_file(filename)
                    or low == "cmakelists.txt" or rel in changed or include_docs)
            if not keep:
                skip(rel, "auxiliary_doc"); continue
        try:
            size = os.path.getsize(full)
        except OSError:
            skip(rel, "stat_error"); continue
        if size > max_bytes:
            skip(rel, "too_large", f"{size // 1024}KB > {max_file_kb}KB"); continue
        try:
            with open(full, "rb") as fh:
                raw = fh.read()
        except OSError:
            skip(rel, "unreadable"); continue
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = raw.decode("utf-16", "replace")
        elif b"\x00" in raw[:8192]:
            skip(rel, "binary_content"); continue
        else:
            text = raw.decode("utf-8", "replace")
        text = strip_illegal_xml(text.replace("\r\n", "\n").replace("\r", "\n")).lstrip("\ufeff")
        redactions = 0
        if redact:
            text, redactions = redact_secrets(text, filename)
        if compress_ws:
            text = compress_whitespace(text)
        if not text.strip():
            skip(rel, "empty"); continue

        e = FileEntry(path=rel, lang=lang_id(filename, ext), ext=ext, git_status=changed.get(rel),
                      content=text, lines=text.count("\n") + 1, redactions=redactions)
        e.tier = resolver.tier(rel, parts)
        e.tokens = counter(text)
        e.outline = build_outline(ext, text)
        e.outline_text = "\n".join(e.outline)
        e.outline_tokens = counter(e.outline_text)
        entries.append(e)

    if only_changed and git["has_head"]:
        n = 0
        for e in sorted(entries, key=lambda x: (x.tier, x.path)):
            if e.git_status and "?" not in e.git_status and n < MAX_DIFF_FILES:
                n += 1
                diff = get_file_diff(project_dir, e.path, git["renames"].get(e.path))
                if diff:
                    if redact:
                        diff, extra = redact_secrets(diff, e.path.rsplit("/", 1)[-1])
                        e.redactions += extra
                    e.diff, e.diff_tokens = diff, counter(diff)

    entries.sort(key=lambda x: (x.tier, x.path))
    if verbose:
        for e in entries:
            print(f"  + [Tier {e.tier}] {e.path} (~{e.tokens} tokens)")

    loc: dict[str, list[int]] = {}
    for e in entries:
        lang = lang_name(e.path.rsplit("/", 1)[-1], e.ext)
        slot = loc.setdefault(lang, [0, 0])
        slot[0] += 1
        slot[1] += sum(1 for l in e.content.split("\n") if l.strip())
    overview["loc"] = dict(sorted(loc.items(), key=lambda kv: -kv[1][1]))

    return Pack(project_dir=project_dir, name=name,
                timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                entries=entries, excluded=excluded, git=git, overview=overview,
                tree=build_tree([e.path for e in entries], changed), counter=counter,
                compress_ws=compress_ws, max_tokens=max_tokens, only_changed=only_changed,
                ignore_backend=spec.backend, total_lines=sum(e.lines for e in entries), redact=redact)


# --------------------------------------------------------------------------- #
# Token budget
# --------------------------------------------------------------------------- #

def _set_view(e: FileEntry, view: str, shown: str | None = None, omitted: int = 0) -> None:
    e.view, e.shown, e.omitted = view, shown, omitted
    e.outline_trimmed, e.outline_trimmed_tokens = None, 0


def trim_outline(e: FileEntry, room: int, counter) -> bool:
    """Shorten e's outline so outline + diff fit in `room` tokens. False if not even a stub fits."""
    room -= e.diff_tokens
    lines = e.outline
    if room < 30 or not lines:
        return False

    def build(k: int) -> str:
        return "\n".join(lines[:k] + [f"... (+{len(lines) - k} more symbols)"])

    lo, hi, best = 1, len(lines) - 1, 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if counter(build(mid)) <= room:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    if best == 0:
        return False
    e.outline_trimmed = build(best)
    e.outline_trimmed_tokens = counter(e.outline_trimmed)
    return True


def assign_levels(entries: list[FileEntry], available: int, counter) -> None:
    """Degrade by tier: full content for the top tiers, then symbol outlines (trimmed if need
    be), then path only. Entries are in priority order, so earlier files win the budget."""
    for e in entries:
        _set_view(e, "path")
    tiers = sorted({e.tier for e in entries})

    def full_cost(e):
        return e.base_cost + e.tokens

    # Highest tier that still gets full content: either everything up to it plus the outlines of
    # the rest fit, or the top tiers take at most FULL_SHARE of the budget (so lower tiers keep
    # a share of it for outlines instead of being starved by one huge high-priority file).
    full_cut = None
    for t in tiers:
        f = sum(full_cost(e) for e in entries if e.tier <= t)
        o = sum(e.base_cost for e in entries if e.tier > t)
        if f + o <= available or f <= available * FULL_SHARE:
            full_cut = t
        else:
            break
    rem = available
    for e in entries:
        if full_cut is not None and e.tier <= full_cut:
            _set_view(e, "full")
            rem -= full_cost(e)
    # Outlines in priority order, using at most OUTLINE_SHARE of what is left so that something
    # remains to upgrade/truncate files. The first outline that does not fit is trimmed to fit.
    outline_room = int(max(rem, 0) * OUTLINE_SHARE)
    for e in entries:
        if e.view == "full" or e.base_cost == 0:
            continue
        if e.base_cost <= outline_room:
            _set_view(e, "outline")
            outline_room -= e.base_cost
            rem -= e.base_cost
        elif trim_outline(e, outline_room, counter):
            e.view = "outline"
            rem -= e.base_cost
            break
    # Spend the rest upgrading files in strict priority order. A file that does not fit is
    # skipped (smaller, lower-priority files may still fit); the first one that can be cut to
    # a useful prefix is truncated, which uses up the remainder.
    for e in entries:
        if e.view == "full":
            continue
        cur = e.base_cost if e.view == "outline" else 0
        full_base = e.outline_tokens + e.diff_tokens  # an upgraded file shows its whole outline
        need = full_base + e.tokens - cur
        if need <= rem:
            _set_view(e, "full")
            rem -= need
            continue
        room = rem - (full_base - cur)
        if room >= MIN_TRUNCATE_TOKENS:
            res = truncate_to_tokens(e.content, room, counter)
            if res:
                _set_view(e, "truncated", res[0], res[1])
                break


def render_with_budget(pack: Pack, render) -> tuple[str, int]:
    """Render the pack, enforcing pack.max_tokens when set. Returns (text, total_tokens)."""
    counter = pack.counter
    if pack.max_tokens:
        budget = pack.max_tokens
        for e in pack.entries:
            _set_view(e, "path")
        overhead = counter(render(pack))
        margin = max(50, budget // 100)
        available = budget - overhead - margin
        if overhead > budget:
            warn(f"--max-tokens {budget} is smaller than the fixed overhead (~{overhead}); "
                 "all files are listed by path only")
        for attempt in range(5):
            if available > 0:
                assign_levels(pack.entries, available, counter)
            text = render(pack)
            total = counter(text)
            if total <= budget or available <= 0:
                break
            available -= (total - budget) + margin // 2
        else:
            warn("could not fit the output within --max-tokens exactly")
        if total > budget and available > 0:
            warn(f"output is ~{total} tokens, slightly over --max-tokens {budget}")
    else:
        for e in pack.entries:
            _set_view(e, "full")
        text = render(pack)
        total = counter(text)
    return text.replace(EST_PLACEHOLDER, str(total)), total


# --------------------------------------------------------------------------- #
# Rendering (shared helpers)
# --------------------------------------------------------------------------- #

def group_excluded(excluded: list, per_reason: int = 10) -> list[tuple[str, int, list[str]]]:
    groups: dict[str, list[str]] = {}
    for path, reason, detail in excluded:
        groups.setdefault(reason, []).append(f"{path} ({detail})" if detail and reason == "too_large" else path)
    return [(r, len(v), v[:per_reason]) for r, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))]


def view_counts(entries: list[FileEntry]) -> dict[str, int]:
    out = {"full": 0, "truncated": 0, "outline": 0, "path": 0}
    for e in entries:
        out[e.view] += 1
    return out


def _dep_lines(m: dict, cap: int = 80) -> list[str]:
    prod = [d for d in m["deps"] if not d[2]]
    dev = [d for d in m["deps"] if d[2]]
    lines = [f"{n} {v}" for n, v, _ in prod] + [f"{n} {v} (dev)" for n, v, _ in dev]
    if len(lines) > cap:
        lines = lines[:cap] + [f"... (+{len(m['deps']) - cap} more)"]
    return lines


def _script_lines(m: dict, cap: int = 25) -> list[str]:
    lines = [f"{k}: {_short(v, 160)}" for k, v in m["scripts"]]
    return lines[:cap] + ([f"... (+{len(lines) - cap} more)"] if len(lines) > cap else [])


# --------------------------------------------------------------------------- #
# XML rendering
# --------------------------------------------------------------------------- #

def render_xml(pack: Pack) -> str:
    o: list[str] = []
    w = o.append
    ov = pack.overview
    attrs = [f'name="{xattr(pack.name)}"', f'generated="{pack.timestamp}"',
             f'files="{len(pack.entries)}"', f'total_lines="{pack.total_lines}"',
             f'est_tokens="{EST_PLACEHOLDER}"', f'tokenizer="{xattr(pack.counter.name)}"',
             f'compress_whitespace="{str(pack.compress_ws).lower()}"']
    if pack.max_tokens:
        attrs.append(f'max_tokens="{pack.max_tokens}"')
    if pack.only_changed:
        attrs.append('mode="only_changed"')
    w('<?xml version="1.0" encoding="UTF-8"?>\n<project ' + " ".join(attrs) + ">\n\n")

    # 1. overview
    w("<overview>\n")
    detected = ", ".join(dict.fromkeys(s["name"] for s in ov["stacks"])) or "none"
    w(f'  <stack primary="{xattr(ov["primary"] or "unknown")}" detected="{xattr(detected)}" '
      f'frameworks="{xattr(", ".join(ov["frameworks"]))}">\n')
    for s in ov["stacks"]:
        root = s["root"] or "."
        mans = ", ".join(s["manifests"])
        w(f'    <root path="{xattr(root)}" stack="{xattr(s["name"])}" manifests="{xattr(mans)}" />\n')
    w("  </stack>\n")
    for m in ov["manifests"]:
        extra = ""
        if m.get("name"):
            extra += f' name="{xattr(m["name"])}"'
        if m.get("version"):
            extra += f' version="{xattr(m["version"])}"'
        if m["deps"]:
            w(f'  <dependencies manifest="{xattr(m["path"])}" count="{len(m["deps"])}"{extra}>\n'
              f'{cdata(chr(10).join(_dep_lines(m)))}\n  </dependencies>\n')
        if m["scripts"]:
            w(f'  <scripts manifest="{xattr(m["path"])}">\n{cdata(chr(10).join(_script_lines(m)))}\n  </scripts>\n')
    if ov["entry_points"]:
        w(f'  <entry_points>\n{cdata(chr(10).join(ov["entry_points"]))}\n  </entry_points>\n')
    total_loc = sum(v[1] for v in ov["loc"].values())
    w(f'  <loc total="{total_loc}">\n')
    for lang, (nf, nl) in list(ov["loc"].items())[:20]:
        w(f'    <language name="{xattr(lang)}" files="{nf}" lines="{nl}" />\n')
    w("  </loc>\n</overview>\n\n")

    # 2. git
    g = pack.git
    if g["is_git"]:
        w(f'<git_context branch="{xattr(g["branch"])}">\n')
        for tag, key in (("uncommitted_status", "status_raw"), ("diff_stat", "diff_stat"),
                         ("recent_commits", "recent_commits")):
            if g[key]:
                w(f"  <{tag}>\n{cdata(g[key])}\n  </{tag}>\n")
        w("</git_context>\n\n")

    # 3. structure + index
    w(f"<structure>\n{cdata(pack.tree)}\n</structure>\n\n")
    w('<file_index ordering="priority_tiered">\n')
    for idx, e in enumerate(pack.entries, 1):
        git_attr = f' git="{xattr(e.git_status)}"' if e.git_status else ""
        w(f'  <entry rank="{idx}" tier="{e.tier}" path="{xattr(e.path)}" lines="{e.lines}" '
          f'est_tokens="{e.tokens}" view="{e.view}"{git_attr} />\n')
    w("</file_index>\n\n")

    # 4. files
    w("<files>\n\n")
    for e in pack.entries:
        git_attr = f' git="{xattr(e.git_status)}"' if e.git_status else ""
        head = (f'path="{xattr(e.path)}" lang="{xattr(e.lang)}" tier="{e.tier}" lines="{e.lines}" '
                f'est_tokens="{e.tokens}" view="{e.view}"{git_attr}')
        if e.view == "path":
            w(f"<file {head} />\n\n")
            continue
        w(f"<file {head}>\n")
        if e.outline_out:
            w(f"<outline>\n{cdata(e.outline_out)}\n</outline>\n")
        if e.diff:
            w(f"<diff>\n{cdata(e.diff)}\n</diff>\n")
        if e.view == "full":
            w(f"<content>{cdata(e.content)}</content>\n")
        elif e.view == "truncated":
            w(f"<content>{cdata(e.shown or '')}<!-- truncated: {e.omitted} lines omitted --></content>\n")
        w("</file>\n\n")
    w("</files>\n\n")

    # 5. excluded summary + meta
    groups = group_excluded(pack.excluded)
    w(f'<excluded_summary total="{len(pack.excluded)}">\n')
    for reason, count, samples in groups:
        w(f'  <reason name="{xattr(reason)}" count="{count}" samples="{len(samples)}">\n'
          f'{cdata(chr(10).join(samples))}\n  </reason>\n')
    w("</excluded_summary>\n\n")
    vc = view_counts(pack.entries)
    w("<meta>\n")
    w(f"  <included_count>{len(pack.entries)}</included_count>\n")
    w(f"  <excluded_count>{len(pack.excluded)}</excluded_count>\n")
    w(f"  <estimated_total_tokens>{EST_PLACEHOLDER}</estimated_total_tokens>\n")
    w(f'  <budget max_tokens="{pack.max_tokens or 0}" full="{vc["full"]}" truncated="{vc["truncated"]}" '
      f'outline="{vc["outline"]}" path_only="{vc["path"]}" />\n')
    red = [(e.path, e.redactions) for e in pack.entries if e.redactions]
    w(f'  <redactions enabled="{str(pack.redact).lower()}" total="{sum(c for _, c in red)}">\n')
    for path, count in red:
        w(f'    <file path="{xattr(path)}" count="{count}" />\n')
    w("  </redactions>\n")
    w(f'  <ignore_backend>{pack.ignore_backend}</ignore_backend>\n')
    if WARNINGS:
        w(f"  <warnings>\n{cdata(chr(10).join(WARNINGS))}\n  </warnings>\n")
    w("</meta>\n\n</project>\n")
    return "".join(o)


# --------------------------------------------------------------------------- #
# Markdown rendering
# --------------------------------------------------------------------------- #

def _fence(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _fenced(text: str, lang: str = "") -> str:
    f = _fence(text)
    return f"{f}{lang}\n{text}\n{f}\n"


def render_md(pack: Pack) -> str:
    o: list[str] = []
    w = o.append
    ov = pack.overview
    w(f"# Project: {strip_illegal_xml(pack.name)}\n\n")
    w(f"- generated: {pack.timestamp}\n- files: {len(pack.entries)}  |  lines: {pack.total_lines}  |  "
      f"est. tokens: {EST_PLACEHOLDER} ({pack.counter.name})\n")
    if pack.max_tokens:
        w(f"- token budget: {pack.max_tokens}\n")
    if pack.only_changed:
        w("- mode: only uncommitted changes\n")
    w("\n## Overview\n\n")
    detected = ", ".join(dict.fromkeys(s["name"] for s in ov["stacks"])) or "none"
    w(f"- stack: {detected} (primary: {ov['primary'] or 'unknown'})\n")
    if ov["frameworks"]:
        w(f"- frameworks: {', '.join(ov['frameworks'])}\n")
    for s in ov["stacks"]:
        w(f"- root `{s['root'] or '.'}`: {s['name']} ({', '.join(s['manifests'])})\n")
    for m in ov["manifests"]:
        if m["deps"]:
            w(f"\n### Dependencies - `{m['path']}` ({len(m['deps'])})\n\n{_fenced(chr(10).join(_dep_lines(m)))}")
        if m["scripts"]:
            w(f"\n### Scripts - `{m['path']}`\n\n{_fenced(chr(10).join(_script_lines(m)))}")
    if ov["entry_points"]:
        w("\n### Entry points\n\n" + "".join(f"- `{p}`\n" for p in ov["entry_points"]))
    w("\n### Lines of code (non-blank)\n\n| Language | Files | Lines |\n|---|---:|---:|\n")
    for lang, (nf, nl) in list(ov["loc"].items())[:20]:
        w(f"| {lang} | {nf} | {nl} |\n")
    g = pack.git
    if g["is_git"]:
        w(f"\n## Git context (branch `{g['branch']}`)\n\n")
        for title, key in (("Uncommitted status", "status_raw"), ("Diff stat", "diff_stat"),
                           ("Recent commits", "recent_commits")):
            if g[key]:
                w(f"**{title}**\n\n{_fenced(g[key])}\n")
    w(f"\n## Structure\n\n{_fenced(pack.tree)}\n")
    w("## File index\n\n| # | Tier | Path | Lines | Tokens | View |\n|---:|---:|---|---:|---:|---|\n")
    for i, e in enumerate(pack.entries, 1):
        w(f"| {i} | {e.tier} | `{e.path}`{' [' + e.git_status + ']' if e.git_status else ''} | "
          f"{e.lines} | {e.tokens} | {e.view} |\n")
    w("\n## Files\n\n")
    for e in pack.entries:
        w(f"### `{e.path}`\n\ntier {e.tier} | {e.lines} lines | ~{e.tokens} tokens | view: {e.view}"
          f"{' | git: ' + e.git_status if e.git_status else ''}\n\n")
        if e.view == "path":
            continue
        if e.outline_out:
            w(f"Outline:\n\n{_fenced(e.outline_out)}\n")
        if e.diff:
            w(f"Diff vs HEAD:\n\n{_fenced(e.diff, 'diff')}\n")
        if e.view == "full":
            w(_fenced(e.content, e.lang) + "\n")
        elif e.view == "truncated":
            w(_fenced(e.shown or "", e.lang) + f"<!-- truncated: {e.omitted} lines omitted -->\n\n")
    w("## Excluded summary\n\n")
    for reason, count, samples in group_excluded(pack.excluded):
        w(f"- **{reason}**: {count}\n" + "".join(f"  - `{s}`\n" for s in samples))
    vc = view_counts(pack.entries)
    w(f"\n## Meta\n\n- included: {len(pack.entries)}\n- excluded: {len(pack.excluded)}\n"
      f"- estimated total tokens: {EST_PLACEHOLDER}\n"
      f"- views: full {vc['full']}, truncated {vc['truncated']}, outline {vc['outline']}, path-only {vc['path']}\n")
    red = [(e.path, e.redactions) for e in pack.entries if e.redactions]
    w(f"- redactions: {sum(c for _, c in red)}" + "".join(f"\n  - `{p}`: {c}" for p, c in red) + "\n")
    for msg in WARNINGS:
        w(f"- warning: {msg}\n")
    return "".join(o)


# --------------------------------------------------------------------------- #
# Output, clipboard, reports
# --------------------------------------------------------------------------- #

def resolve_output_dir(project_dir: str, override: str | None, cfg: ProjectConfig) -> str:
    """Precedence: --output-dir / GUI choice > $DOCPROJECT_OUTPUT_DIR > .docproject.toml > DEFAULT_OUTPUT_DIR."""
    if override:
        return os.path.abspath(os.path.expanduser(override))
    env = os.environ.get("DOCPROJECT_OUTPUT_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if cfg.output_dir:
        return os.path.abspath(os.path.join(project_dir, os.path.expanduser(cfg.output_dir)))
    return DEFAULT_OUTPUT_DIR


def make_output_path(out_dir: str, name: str, fmt: str) -> str:
    """<out_dir>/<project folder name>.xml (or .md). An existing file is overwritten."""
    try:
        os.makedirs(out_dir, exist_ok=True)
        if not os.access(out_dir, os.W_OK):
            raise PermissionError(out_dir)
    except OSError as exc:
        fallback = os.path.join(os.getcwd(), OUTPUT_DIR_NAME)
        warn(f"cannot write to {out_dir} ({exc}); using {fallback}")
        out_dir = fallback
        os.makedirs(out_dir, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip() or "project"  # only chars illegal in filenames
    return os.path.join(out_dir, safe + (".xml" if fmt == "xml" else ".md"))


def copy_to_clipboard(text: str) -> bool:
    try:
        import pyperclip  # type: ignore
        pyperclip.copy(text)
        return True
    except Exception:
        pass
    if sys.platform == "darwin":
        cmds = [["pbcopy"]]
    elif os.name == "nt":
        cmds = [["powershell", "-NoProfile", "-Command",
                 "[Console]::InputEncoding=[Text.Encoding]::UTF8; Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
                ["clip"]]
    else:
        cmds = [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
    for cmd in cmds:
        exe = shutil.which(cmd[0])
        if not exe:
            continue
        data = text.encode("utf-16") if cmd[0] == "clip" else text.encode("utf-8")
        try:
            if subprocess.run([exe] + cmd[1:], input=data, timeout=30,
                              capture_output=True).returncode == 0:
                return True
        except Exception:
            continue
    return False


def print_dry_run(pack: Pack) -> None:
    entries = pack.entries
    print("\nDRY RUN - nothing written\n")
    print(f"Included: {len(entries)} files | {pack.total_lines:,} lines | "
          f"~{sum(e.tokens for e in entries):,} content tokens ({pack.counter.name})")
    print("\nTop 25 files by token cost:")
    for i, e in enumerate(sorted(entries, key=lambda x: -x.tokens)[:25], 1):
        print(f"  {i:>2}. {e.tokens:>8,}  tier {e.tier}  {e.path}")
    print("\nPer-tier totals:")
    print("  tier  files   lines     tokens   cumulative")
    cum = 0
    for t in sorted({e.tier for e in entries}):
        grp = [e for e in entries if e.tier == t]
        tok = sum(e.tokens for e in grp)
        cum += tok
        print(f"  {t:>4}  {len(grp):>5}  {sum(e.lines for e in grp):>6,}  {tok:>9,}  {cum:>11,}")
    if pack.max_tokens:
        print(f"\nBudget plan for --max-tokens {pack.max_tokens:,}:")
        print("  tier  full  truncated  outline  path-only")
        for t in sorted({e.tier for e in entries}):
            c = view_counts([e for e in entries if e.tier == t])
            print(f"  {t:>4}  {c['full']:>4}  {c['truncated']:>9}  {c['outline']:>7}  {c['path']:>9}")
    print("\nExcluded by reason:")
    for reason, count, samples in group_excluded(pack.excluded, 3):
        print(f"  {count:>6}  {reason}" + (f"   e.g. {', '.join(samples)}" if samples else ""))
    redacted = sum(e.redactions for e in entries)
    if redacted:
        print(f"\nSecret redactions: {redacted} in {sum(1 for e in entries if e.redactions)} file(s)")
    print()


# --------------------------------------------------------------------------- #
# Core entry point
# --------------------------------------------------------------------------- #

def document_project_core(project_dir: str, compress_ws: bool = True, include_tests: bool = False,
                          include_docs: bool = False, only_changed: bool = False,
                          focus_raw: str | None = None, max_file_kb: int | None = None,
                          output_dir_override: str | None = None, max_tokens: int | None = None,
                          fmt: str | None = None, dry_run: bool = False, copy: bool = False,
                          redact: bool = True, include_native_config: bool = False,
                          verbose: bool = False) -> tuple[str | None, dict]:
    """Scan a project and write a priority-ordered pack. Returns (output_path|None, stats)."""
    del WARNINGS[:]
    project_dir = os.path.abspath(project_dir)
    if not os.path.isdir(project_dir):
        raise NotADirectoryError(f"not a directory: {project_dir}")
    cfg = load_project_config(project_dir)
    fmt = fmt or "xml"
    max_file_kb = max_file_kb if max_file_kb else (cfg.max_file_kb or DEFAULT_MAX_FILE_KB)
    max_tokens = max_tokens if max_tokens and max_tokens > 0 else cfg.max_tokens
    include_native_config = include_native_config or bool(cfg.include_native_config)
    out_dir = resolve_output_dir(project_dir, output_dir_override, cfg)

    pack = build_pack(project_dir, compress_ws=compress_ws, include_tests=include_tests,
                      include_docs=include_docs, include_native=include_native_config,
                      only_changed=only_changed, focus_raw=focus_raw, max_file_kb=max_file_kb,
                      max_tokens=max_tokens, redact=redact, verbose=verbose, cfg=cfg, out_dir=out_dir)
    render = render_xml if fmt == "xml" else render_md
    text, total = render_with_budget(pack, render)
    stats = {"included": len(pack.entries), "excluded": len(pack.excluded),
             "total_lines": pack.total_lines, "est_tokens": total, "output": None,
             "tokenizer": pack.counter.name, "copied": False,
             "redactions": sum(e.redactions for e in pack.entries), "views": view_counts(pack.entries),
             "warnings": list(WARNINGS)}
    if dry_run:
        print_dry_run(pack)
        return None, stats

    path = make_output_path(out_dir, pack.name, fmt)
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    except OSError as exc:
        raise RuntimeError(f"Failed to write output file: {exc}") from exc
    stats["output"] = path
    if copy:
        stats["copied"] = copy_to_clipboard(text)
        if not stats["copied"]:
            warn("--copy: no clipboard available (install pyperclip, xclip, wl-copy or xsel)")
    return path, stats


# --------------------------------------------------------------------------- #
# CLI / GUI
# --------------------------------------------------------------------------- #

def _kwargs_from_args(args) -> dict:
    return dict(compress_ws=not args.no_compress, include_tests=args.include_tests,
                include_docs=args.include_docs, only_changed=args.only_changed,
                focus_raw=args.focus, max_file_kb=args.max_file_kb,
                output_dir_override=args.output_dir, max_tokens=args.max_tokens, fmt=args.format,
                dry_run=args.dry_run, copy=args.copy, redact=not args.no_redact,
                include_native_config=args.include_native_config, verbose=args.verbose)


def build_cli_hint(args, project_dir: str) -> str:
    """Rebuild the exact command line, including every flag that was used."""
    cmd = ["python", os.path.basename(sys.argv[0]) or "doc_project.py", "--cli", "--path", project_dir]
    for attr, flag in (("no_compress", "--no-compress"), ("include_tests", "--include-tests"),
                       ("include_docs", "--include-docs"), ("include_native_config", "--include-native-config"),
                       ("only_changed", "--only-changed"), ("copy", "--copy"), ("dry_run", "--dry-run"),
                       ("no_redact", "--no-redact"), ("verbose", "--verbose")):
        if getattr(args, attr):
            cmd.append(flag)
    for attr, flag in (("focus", "--focus"), ("max_file_kb", "--max-file-kb"), ("max_tokens", "--max-tokens"),
                       ("format", "--format"), ("output_dir", "--output-dir")):
        val = getattr(args, attr)
        if val is not None:
            cmd += [flag, str(val)]
    return " ".join(shlex.quote(c) for c in cmd)


def print_summary(stats: dict) -> None:
    print("\nDone.")
    print(f"  Included    : {stats['included']} files")
    print(f"  Excluded    : {stats['excluded']} files")
    print(f"  Total Lines : {stats['total_lines']:,}")
    print(f"  Est. Tokens : ~{stats['est_tokens']:,} ({stats['tokenizer']})")
    v = stats["views"]
    print(f"  Views       : full {v['full']}, truncated {v['truncated']}, outline {v['outline']}, path-only {v['path']}")
    if stats["redactions"]:
        print(f"  Redactions  : {stats['redactions']}")
    if stats["output"]:
        print(f"  Saved to    : {stats['output']}")
    if stats["copied"]:
        print("  Clipboard   : copied")
    print()


def run_cli_mode(args) -> int:
    if not args.path:
        print("ERROR: --path is required in CLI mode.", file=sys.stderr)
        return 1
    try:
        _, stats = document_project_core(args.path, **_kwargs_from_args(args))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if not args.dry_run:
        print_summary(stats)
    return 0


_GUI: dict = {}


def _gui():
    """Lazy tkinter import - the CLI never needs a display or tkinter."""
    if not _GUI:
        import tkinter as tk
        from tkinter import filedialog, messagebox
        root = tk._default_root or tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
            root.focus_force()
        except Exception:
            pass
        _GUI.update(root=root, filedialog=filedialog, messagebox=messagebox)
    return _GUI


def run_gui_mode(args) -> int:
    try:
        gui = _gui()
    except Exception as exc:  # ImportError or TclError (no display)
        print(f"ERROR: GUI unavailable ({exc}). Use:\n  python doc_project.py --cli --path <dir>", file=sys.stderr)
        return 1
    gui["messagebox"].showinfo("Code Documenter", "Select the project directory to document.")
    project_dir = gui["filedialog"].askdirectory(title="Select Project Directory")
    if not project_dir:
        print("No directory selected. Exiting.")
        return 0
    if not args.output_dir and not args.dry_run:
        # Ask where to save. The dialog opens in the default location (or its nearest existing
        # parent); cancelling the dialog keeps the default.
        start = os.path.abspath(os.path.expanduser(os.environ.get("DOCPROJECT_OUTPUT_DIR") or DEFAULT_OUTPUT_DIR))
        while start and not os.path.isdir(start) and os.path.dirname(start) != start:
            start = os.path.dirname(start)
        gui["messagebox"].showinfo("Code Documenter", "Now choose the folder to save the output file in.")
        chosen = gui["filedialog"].askdirectory(title="Select Output Directory", initialdir=start or os.getcwd())
        args.output_dir = chosen or None  # None -> the default location
        print(f"Save to  : {chosen or '(default) ' + DEFAULT_OUTPUT_DIR}")
    try:
        _, stats = document_project_core(project_dir, **_kwargs_from_args(args))
    except Exception as exc:
        gui["messagebox"].showerror("Error", str(exc))
        return 1
    if not args.dry_run:
        print_summary(stats)
    msg = (f"Documentation complete.\n\nIncluded files : {stats['included']}\n"
           f"Excluded files : {stats['excluded']}\nTotal lines    : {stats['total_lines']:,}\n"
           f"Est. tokens    : ~{stats['est_tokens']:,}\n\nSaved to:\n{stats['output'] or '(dry run)'}")
    print("=" * 60 + "\nRUN VIA CLI NEXT TIME:\n" + build_cli_hint(args, project_dir) + "\n" + "=" * 60 + "\n")
    gui["messagebox"].showinfo("Success", msg)
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    parser = argparse.ArgumentParser(
        description="Project Code Documenter - outputs a priority-sorted pack for LLM context",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python doc_project.py                                            # GUI mode
  python doc_project.py --cli --path ./my-app                      # full prioritized pack
  python doc_project.py --cli --path ./my-app --dry-run            # see where the tokens go
  python doc_project.py --cli --path ./my-app --max-tokens 60000   # fit a 60k budget
  python doc_project.py --cli --path ./my-app --only-changed       # uncommitted work + diffs
  python doc_project.py --cli --path ./my-app --focus Order,Risk --format md --copy
""")
    parser.add_argument("--cli", action="store_true", help="Run in CLI mode (no GUI)")
    parser.add_argument("--path", help="Project directory (implies CLI mode)")
    parser.add_argument("--no-compress", action="store_true", help="Keep original whitespace")
    parser.add_argument("--include-tests", action="store_true", help="Include test/spec/mock files")
    parser.add_argument("--include-docs", action="store_true", help="Include auxiliary markdown/text docs")
    parser.add_argument("--include-native-config", action="store_true", help="Include top-level android/ and ios/")
    parser.add_argument("--only-changed", action="store_true", help="Only uncommitted git files (+ git diff HEAD)")
    parser.add_argument("--focus", help="Comma-separated substrings promoted to Tier 0")
    parser.add_argument("--max-file-kb", type=int, help=f"Skip files above this size (default {DEFAULT_MAX_FILE_KB})")
    parser.add_argument("--max-tokens", type=int, help="Token budget: full -> outline -> path-only by tier")
    parser.add_argument("--format", choices=["xml", "md"], help="Output format (default xml)")
    parser.add_argument("--output-dir", help=f"Output directory (default: {DEFAULT_OUTPUT_DIR}; "
                                             "the GUI asks and opens there)")
    parser.add_argument("--copy", action="store_true", help="Copy the output to the clipboard if possible")
    parser.add_argument("--dry-run", action="store_true", help="Print top files/tier totals, write nothing")
    parser.add_argument("--no-redact", action="store_true", help="Disable secret redaction")
    parser.add_argument("-v", "--verbose", action="store_true", help="List every included file")
    args = parser.parse_args(argv)
    if args.cli or args.path:
        return run_cli_mode(args)
    return run_gui_mode(args)


if __name__ == "__main__":
    sys.exit(main())
