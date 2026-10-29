"""
doc_project.py — Elite Project Code Documenter & LLM Context Packer
Scans a project directory and outputs a priority-sorted, token-optimized XML file
suitable for attaching to LLM prompts (Gemini, Claude, GPT, Antigravity IDE, etc.)

Usage:
  GUI mode:  python doc_project.py
  CLI mode:  python doc_project.py --cli --path /path/to/project
  Options:
    --no-compress          Keep original whitespace
    --include-tests        Include test/spec/mock files and test directories
    --include-docs         Include auxiliary markdown/backlog files (README/AGENTS always kept)
    --only-changed         Only include files with uncommitted Git changes (git status)
    --focus SUBSTR         Prioritize or filter paths matching comma-separated substrings
    --max-file-kb INT      Skip individual files larger than N KB (default: 256 KB)
    --output-dir PATH      Override default output directory
"""

import os
import sys
import re
import subprocess
import argparse
import tkinter as tk
from tkinter import filedialog, messagebox
from datetime import datetime, timezone

STATIC_OUTPUT_BASE = "/home/kushal/Desktop/Projects/Document_Project/output_docs"
DEFAULT_MAX_FILE_KB = 256

# Directories always excluded during traversal
EXCLUDED_DIRS = {
    "node_modules", "venv", ".venv", ".git", "__pycache__",
    ".vscode", ".idea", "build", "dist", "target", "env",
    "output_docs", ".next", ".nuxt", ".svelte-kit", ".parcel-cache",
    "coverage", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "htmlcov", ".turbo", ".vercel", ".netlify", ".expo",
    "android", "ios", ".gradle", ".kotlin", "Pods", "DerivedData",
}

# Directory names excluded when --include-tests is False
TEST_DIRS = {
    "test", "tests", "__tests__", "__mocks__", "e2e", "cypress", "playwright"
}

# Fixed comma bug from original script + expanded binary/artifact extensions
EXCLUDED_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".webp", ".ico",
    ".tiff", ".tif", ".psd", ".ai", ".eps", ".log",
    ".csv", ".sqlite", ".sqlite3", ".db", ".parquet", ".pkl", ".pickle",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".mp3", ".wav", ".ogg", ".flac", ".mp4", ".webm", ".mkv", ".avi", ".mov",
    ".pdf", ".zip", ".tar", ".gz", ".rar", ".7z", ".dmg", ".exe", ".dll",
    ".pyc", ".pyo", ".class", ".o", ".so", ".dylib", ".wasm",
    ".min.js", ".min.css", ".map", ".pem", ".key", ".cert", ".p12",
    ".jks", ".keystore", ".mobileprovision", ".aab", ".apk", ".ipa",
    ".lock", ".snap",
}

# Specific noisy, sensitive, or non-code files to exclude
EXCLUDED_FILES = {
    "package-lock.json", "yarn.lock", "poetry.lock", "pnpm-lock.yaml",
    "Pipfile.lock", "composer.lock", "Gemfile.lock", "Cargo.lock", "bun.lockb",
    ".DS_Store", "Thumbs.db", ".gitignore", ".gitattributes",
    ".editorconfig", ".prettierignore", ".eslintignore", ".npmrc", ".nvmrc",
    "LICENSE", "LICENSE.md", "LICENSE.txt",
    "google-services.json", "GoogleService-Info.plist",
    "master-prompt.txt", "ideas.txt", "to_commits.txt", "cmds_to_deliver.txt",
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
    re.compile(r"^jest\.config\.[a-z0-9]+$", re.IGNORECASE),
    re.compile(r"^vitest\.config\.[a-z0-9]+$", re.IGNORECASE),
]

# Core architectural docs to always keep; noisy backlog/changelogs can be skipped unless --include-docs
CORE_DOC_FILES = {"AGENTS.md", "PAPER_TRADING_RULES.md", "SECURITY.md", "README.md"}


def is_binary_file(filepath: str) -> bool:
    """Detects if a file is binary by checking for null bytes in the first 2048 bytes."""
    try:
        with open(filepath, "rb") as f:
            chunk = f.read(2048)
            if b"\x00" in chunk:
                return True
    except Exception:
        return True
    return False


def is_secret_file(filename: str) -> bool:
    """Blocks .env.* and credential files while allowing .env.example templates."""
    if filename.lower() in ALLOWED_ENV_FILES:
        return False
    return any(p.match(filename) for p in SECRET_FILENAME_PATTERNS)


def compress_whitespace(content: str) -> str:
    """
    - Strip trailing spaces on every line
    - Collapse 3+ consecutive blank lines -> 1 blank line
    - Remove leading/trailing blank lines
    """
    lines = [line.rstrip() for line in content.splitlines()]
    compressed = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return compressed.strip()


def xml_escape(text: str) -> str:
    """Safely escapes CDATA closing sequences so embedded code never breaks XML."""
    return text.replace("]]>", "]]]]><![CDATA[>")


def run_git_cmd(project_dir: str, args: list[str]) -> str | None:
    """Runs a git command safely and returns stdout or None if not a git repo."""
    try:
        res = subprocess.run(
            ["git", "-C", project_dir] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return None


def get_git_context(project_dir: str) -> dict:
    """
    Collects Git metadata: current branch, uncommitted status map, diff stat,
    recent commits, and git-tracked/untracked non-ignored file list.
    """
    branch = run_git_cmd(project_dir, ["rev-parse", "--abbrev-ref", "HEAD"])
    if not branch:
        return {
            "is_git": False,
            "branch": None,
            "status_raw": "",
            "diff_stat": "",
            "recent_commits": "",
            "changed_files": {},
            "git_file_list": None,
        }

    status_raw = run_git_cmd(project_dir, ["status", "--short"]) or ""
    diff_stat = run_git_cmd(project_dir, ["diff", "--stat", "HEAD"]) or ""
    recent_commits = run_git_cmd(project_dir, ["log", "-n", "5", "--oneline"]) or ""

    changed_files: dict[str, str] = {}
    for line in status_raw.splitlines():
        if len(line) > 3:
            code = line[:2].strip() or "M"
            path_part = line[3:].strip().strip('"')
            if " -> " in path_part:
                path_part = path_part.split(" -> ")[-1].strip()
            changed_files[path_part.replace("\\", "/")] = code

    # Use git ls-files to automatically respect .gitignore rules
    ls_out = run_git_cmd(
        project_dir, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    )
    git_file_list = None
    if ls_out is not None:
        git_file_list = [p.replace("\\", "/") for p in ls_out.split("\0") if p]

    return {
        "is_git": True,
        "branch": branch,
        "status_raw": status_raw,
        "diff_stat": diff_stat,
        "recent_commits": recent_commits,
        "changed_files": changed_files,
        "git_file_list": git_file_list,
    }


def compute_file_priority(
    rel_path: str,
    git_changed: dict[str, str],
    focus_terms: list[str],
) -> tuple[int, str]:
    """
    Assigns a deterministic architectural priority tier so critical files appear
    FIRST in the XML (preventing LLM truncation of screens/hooks/services):
      Tier 0: Explicitly focused paths (--focus)
      Tier 1: Uncommitted Git modified/added files (active work)
      Tier 2: Architecture rules & entrypoints (AGENTS.md, PAPER_TRADING_RULES.md, App.tsx)
      Tier 3: Domain models, types, & database schema (src/domain, src/types, schema.ts)
      Tier 4: Core services, repositories, & hooks (src/services, src/database, src/hooks)
      Tier 5: UI screens & components (src/screens, src/components)
      Tier 6: Utilities & theme (src/utils, src/theme, src/context)
      Tier 7: Project configuration (package.json, tsconfig.json, app.json)
      Tier 8: Docs, tests, & miscellaneous
    """
    norm = rel_path.lower()

    if focus_terms and any(term in norm for term in focus_terms):
        return (0, rel_path)
    if rel_path in git_changed:
        return (1, rel_path)
    if rel_path in {"AGENTS.md", "PAPER_TRADING_RULES.md", "SECURITY.md", "App.tsx"}:
        return (2, rel_path)
    if norm.startswith(("src/domain/", "src/types/")) or norm.endswith("schema.ts"):
        return (3, rel_path)
    if norm.startswith(("src/services/", "src/database/", "src/hooks/")):
        return (4, rel_path)
    if norm.startswith(("src/screens/", "src/components/")):
        return (5, rel_path)
    if norm.startswith(("src/utils/", "src/theme/", "src/context/")):
        return (6, rel_path)
    if norm in {"package.json", "tsconfig.json", "app.json", "eas.json", "index.ts"}:
        return (7, rel_path)
    return (8, rel_path)


def build_tree(included_files: list[str], git_changed: dict[str, str]) -> str:
    """
    Builds a compact indented directory tree of included files.
    Marks modified/untracked files with [M] / [??] right in the tree!
    """
    tree_lines = []
    seen_dirs = set()
    for rel_path in sorted(included_files):
        parts = rel_path.replace("\\", "/").split("/")
        for i in range(1, len(parts)):
            dir_path = "/".join(parts[:i])
            if dir_path not in seen_dirs:
                seen_dirs.add(dir_path)
                tree_lines.append("  " * (i - 1) + parts[i - 1] + "/")
        marker = f" [{git_changed[rel_path]}]" if rel_path in git_changed else ""
        tree_lines.append("  " * (len(parts) - 1) + parts[-1] + marker)
    return "\n".join(tree_lines)


def collect_candidate_paths(project_dir: str, git_file_list: list[str] | None) -> list[str]:
    """Returns relative POSIX paths using git ls-files if available, else os.walk."""
    if git_file_list is not None:
        # Filter out files that were deleted in working tree
        return [
            p for p in git_file_list
            if os.path.isfile(os.path.join(project_dir, p))
        ]

    candidates = []
    for dirpath, dirnames, filenames in os.walk(project_dir, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        for filename in filenames:
            full_path = os.path.join(dirpath, filename)
            rel_path = os.path.relpath(full_path, project_dir).replace("\\", "/")
            candidates.append(rel_path)
    return candidates


def document_project_core(
    project_dir: str,
    compress_ws: bool = True,
    include_tests: bool = False,
    include_docs: bool = False,
    only_changed: bool = False,
    focus_raw: str | None = None,
    max_file_kb: int = DEFAULT_MAX_FILE_KB,
    output_dir_override: str | None = None,
) -> tuple[str, dict]:
    """
    Scans a project directory and writes a priority-ordered, compact XML file.
    Returns (output_file_path, stats_dict).
    """
    project_dir = os.path.abspath(project_dir)
    folder_name = os.path.basename(os.path.normpath(project_dir))
    out_dir = os.path.expanduser(output_dir_override or STATIC_OUTPUT_BASE)
    os.makedirs(out_dir, exist_ok=True)
    output_file = os.path.join(out_dir, f"{folder_name}.xml")

    focus_terms = [
        t.strip().lower() for t in (focus_raw or "").split(",") if t.strip()
    ]

    git_ctx = get_git_context(project_dir)
    git_changed: dict[str, str] = git_ctx["changed_files"]

    candidates = collect_candidate_paths(project_dir, git_ctx["git_file_list"])

    included_files: list[str] = []
    excluded_files: list[tuple[str, str]] = []
    file_entries: list[dict] = []

    print(f"\nScanning : {project_dir}")
    print(f"Output   : {output_file}\n")

    max_bytes = max_file_kb * 1024

    for rel_path in candidates:
        full_path = os.path.join(project_dir, rel_path)
        parts = rel_path.split("/")
        filename = parts[-1]
        dir_parts = set(parts[:-1])
        ext = os.path.splitext(filename)[1].lower()

        # 1. Directory exclusions
        if dir_parts & EXCLUDED_DIRS:
            excluded_files.append((rel_path, "excluded_dir"))
            continue

        # 2. --only-changed filter
        if only_changed and rel_path not in git_changed:
            continue

        # 3. Filename & secret exclusions
        if filename in EXCLUDED_FILES or is_secret_file(filename):
            excluded_files.append((rel_path, "excluded_filename_or_secret"))
            continue

        # 4. Extension exclusions
        if not ext or ext in EXCLUDED_EXTENSIONS:
            excluded_files.append((rel_path, f"excluded_extension:{ext or 'none'}"))
            continue

        # 5. Test directory & test file exclusions
        if not include_tests:
            if (dir_parts & TEST_DIRS) or any(p.search(filename) for p in TEST_FILE_PATTERNS):
                excluded_files.append((rel_path, "test_or_mock_file"))
                continue

        # 6. Non-core markdown/backlog exclusions (unless --include-docs or git modified)
        if not include_docs and rel_path not in git_changed:
            if (ext in {".md", ".txt"} and filename not in CORE_DOC_FILES) or "etc" in dir_parts:
                excluded_files.append((rel_path, "auxiliary_doc"))
                continue

        # 7. File size guard
        try:
            file_size = os.path.getsize(full_path)
            if file_size > max_bytes:
                excluded_files.append((rel_path, f"exceeds_{max_file_kb}KB"))
                print(f"  - {rel_path}  [excluded: {file_size // 1024}KB > {max_file_kb}KB]")
                continue
        except OSError:
            excluded_files.append((rel_path, "stat_error"))
            continue

        # 8. Heuristic binary check
        if is_binary_file(full_path):
            excluded_files.append((rel_path, "binary"))
            continue

        # 9. Read & process content
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
        except Exception:
            excluded_files.append((rel_path, "unreadable"))
            continue

        if compress_ws:
            content = compress_whitespace(content)

        if not content.strip():
            excluded_files.append((rel_path, "empty"))
            continue

        priority_tier, _ = compute_file_priority(rel_path, git_changed, focus_terms)
        lines_count = content.count("\n") + 1
        est_tokens = max(1, len(content) // 4)

        included_files.append(rel_path)
        file_entries.append({
            "path": rel_path,
            "lang": ext.lstrip("."),
            "priority": priority_tier,
            "git_status": git_changed.get(rel_path),
            "lines": lines_count,
            "tokens": est_tokens,
            "content": content,
        })
        print(f"  + [Tier {priority_tier}] {rel_path} (~{est_tokens} tokens)")

    # Sort files by architectural priority tier first, then alphabetically
    file_entries.sort(key=lambda item: (item["priority"], item["path"]))

    total_tokens = sum(item["tokens"] for item in file_entries)
    total_lines = sum(item["lines"] for item in file_entries)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tree_str = build_tree(included_files, git_changed)

    try:
        with open(output_file, "w", encoding="utf-8") as out:
            out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            out.write(
                f'<project name="{folder_name}" generated="{timestamp}" '
                f'files="{len(file_entries)}" total_lines="{total_lines}" '
                f'est_tokens="{total_tokens}" '
                f'compress_whitespace="{str(compress_ws).lower()}">\n\n'
            )

            # 1. Git Working Tree Context (so the LLM immediately sees active work)
            if git_ctx["is_git"]:
                out.write(f'<git_context branch="{git_ctx["branch"]}">\n')
                if git_ctx["status_raw"]:
                    out.write("  <uncommitted_status>\n")
                    out.write(f"<![CDATA[\n{git_ctx['status_raw']}\n]]>\n")
                    out.write("  </uncommitted_status>\n")
                if git_ctx["diff_stat"]:
                    out.write("  <diff_stat>\n")
                    out.write(f"<![CDATA[\n{git_ctx['diff_stat']}\n]]>\n")
                    out.write("  </diff_stat>\n")
                if git_ctx["recent_commits"]:
                    out.write("  <recent_commits>\n")
                    out.write(f"<![CDATA[\n{git_ctx['recent_commits']}\n]]>\n")
                    out.write("  </recent_commits>\n")
                out.write("</git_context>\n\n")

            # 2. Directory Structure Tree (Alphabetical with [M]/[??] badges)
            out.write("<structure>\n")
            out.write(f"<![CDATA[\n{tree_str}\n]]>\n")
            out.write("</structure>\n\n")

            # 3. Priority-Ordered File Index (Table of Contents with Token Weights)
            out.write("<file_index ordering=\"priority_tiered\">\n")
            for idx, entry in enumerate(file_entries, start=1):
                git_attr = f' git="{entry["git_status"]}"' if entry["git_status"] else ""
                out.write(
                    f'  <entry rank="{idx}" tier="{entry["priority"]}" '
                    f'path="{entry["path"]}" lines="{entry["lines"]}" '
                    f'est_tokens="{entry["tokens"]}"{git_attr} />\n'
                )
            out.write("</file_index>\n\n")

            # 4. File Contents (Sorted by Priority Tier so critical code is never truncated)
            out.write("<files>\n\n")
            for entry in file_entries:
                git_attr = f' git="{entry["git_status"]}"' if entry["git_status"] else ""
                out.write(
                    f'<file path="{entry["path"]}" lang="{entry["lang"]}" '
                    f'tier="{entry["priority"]}" lines="{entry["lines"]}"{git_attr}>\n'
                )
                out.write(f"<![CDATA[\n{xml_escape(entry['content'])}\n]]>\n</file>\n\n")
            out.write("</files>\n\n")

            # 5. Compact Meta Summary
            out.write("<meta>\n")
            out.write(f"  <included_count>{len(file_entries)}</included_count>\n")
            out.write(f"  <excluded_count>{len(excluded_files)}</excluded_count>\n")
            out.write(f"  <estimated_total_tokens>{total_tokens}</estimated_total_tokens>\n")
            out.write("</meta>\n\n</project>\n")

    except Exception as e:
        raise RuntimeError(f"Failed to write output file: {e}")

    return output_file, {
        "included": len(file_entries),
        "excluded": len(excluded_files),
        "total_lines": total_lines,
        "est_tokens": total_tokens,
    }


def _get_tkinter_root():
    if tk._default_root:
        return tk._default_root
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.focus_force()
    return root


def show_info(title, message):
    _get_tkinter_root()
    messagebox.showinfo(title, message)


def show_error(title, message):
    _get_tkinter_root()
    messagebox.showerror(title, message)


def ask_directory(title):
    _get_tkinter_root()
    return filedialog.askdirectory(title=title)


def run_gui_mode(args):
    show_info("Code Documenter", "Select the project directory to document.")
    project_dir = ask_directory("Select Project Directory")
    if not project_dir:
        print("No directory selected. Exiting.")
        return
    try:
        output_file, stats = document_project_core(
            project_dir,
            compress_ws=not args.no_compress,
            include_tests=args.include_tests,
            include_docs=args.include_docs,
            only_changed=args.only_changed,
            focus_raw=args.focus,
            max_file_kb=args.max_file_kb,
            output_dir_override=args.output_dir,
        )
    except Exception as e:
        show_error("Error", str(e))
        return
    msg = (
        f"Documentation complete.\n\n"
        f"Included files : {stats['included']}\n"
        f"Excluded files : {stats['excluded']}\n"
        f"Total lines    : {stats['total_lines']:,}\n"
        f"Est. tokens    : ~{stats['est_tokens']:,}\n\n"
        f"Saved to:\n{output_file}"
    )
    print("\n" + msg)
    script_name = os.path.basename(sys.argv[0])
    cli_cmd = f'python {script_name} --cli --path "{project_dir}"'
    if args.no_compress:
        cli_cmd += " --no-compress"
    if args.include_tests:
        cli_cmd += " --include-tests"
    if args.include_docs:
        cli_cmd += " --include-docs"
    print("\n" + "=" * 60)
    print("RUN VIA CLI NEXT TIME:")
    print(cli_cmd)
    print("=" * 60 + "\n")
    show_info("Success", msg)


def run_cli_mode(args):
    if not args.path:
        print("ERROR: --path is required in CLI mode.")
        sys.exit(1)
    try:
        output_file, stats = document_project_core(
            args.path,
            compress_ws=not args.no_compress,
            include_tests=args.include_tests,
            include_docs=args.include_docs,
            only_changed=args.only_changed,
            focus_raw=args.focus,
            max_file_kb=args.max_file_kb,
            output_dir_override=args.output_dir,
        )
        print(
            f"\nDone.\n"
            f"  Included    : {stats['included']} files\n"
            f"  Excluded    : {stats['excluded']} files\n"
            f"  Total Lines : {stats['total_lines']:,}\n"
            f"  Est. Tokens : ~{stats['est_tokens']:,}\n"
            f"  Saved to    : {output_file}\n"
        )
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Project Code Documenter — outputs priority-sorted XML for LLM context",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python doc_project.py                                          # GUI mode
  python doc_project.py --cli --path ./my-app                    # CLI mode (full prioritized pack)
  python doc_project.py --cli --path ./my-app --only-changed     # Pack only uncommitted Git changes
  python doc_project.py --cli --path ./my-app --focus Order,Risk # Put Order/Risk files at Tier 0
        """,
    )
    parser.add_argument("--cli", action="store_true", help="Run in CLI mode (no GUI)")
    parser.add_argument("--path", help="Project directory path (required in CLI mode)")
    parser.add_argument("--no-compress", action="store_true", help="Keep original whitespace")
    parser.add_argument("--include-tests", action="store_true", help="Include test/spec/mock files")
    parser.add_argument("--include-docs", action="store_true", help="Include auxiliary markdown/etc files")
    parser.add_argument("--only-changed", action="store_true", help="Only include uncommitted Git files")
    parser.add_argument("--focus", help="Comma-separated substrings to promote to Tier 0")
    parser.add_argument("--max-file-kb", type=int, default=DEFAULT_MAX_FILE_KB, help="Max file size in KB")
    parser.add_argument("--output-dir", help="Override default output directory")

    args = parser.parse_args()
    if args.cli:
        run_cli_mode(args)
    else:
        try:
            _get_tkinter_root()
        except tk.TclError:
            print("ERROR: GUI unavailable. Use:\n  python doc_project.py --cli --path <dir>")
            sys.exit(1)
        run_gui_mode(args)


if __name__ == "__main__":
    main()
