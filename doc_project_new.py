"""
doc_project.py — Project Code Documenter
Scans a project directory and outputs a compact XML file suitable for
attaching to LLM prompts (Claude, GPT, Gemini, Qwen, Llama, etc.)

Usage:
  GUI mode:  python doc_project.py
  CLI mode:  python doc_project.py --cli --path /path/to/project
  Options:
    --no-compress          Keep original whitespace
    --include-tests        Include test/spec files (excluded by default)
    --output-dir PATH      Override default output directory
"""

import os
import sys
import re
import argparse
import tkinter as tk
from tkinter import filedialog, messagebox
from datetime import datetime
STATIC_OUTPUT_BASE = "/home/kushal/Desktop/Projects/Document_Project/output_docs"

EXCLUDED_DIRS = {
    "node_modules", "venv", ".venv", ".git", "__pycache__",
    ".vscode", ".idea", "build", "dist", "target", "env",
    "output_docs", ".next", ".nuxt", ".svelte-kit", ".parcel-cache",
    "coverage", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "htmlcov", ".turbo", ".vercel", ".netlify",
}

EXCLUDED_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".webp", ".ico",
    ".tiff", ".tif", ".psd", ".ai", ".eps",
    ".csv", ".sqlite", ".db", ".parquet", ".pkl", ".pickle",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".mp3", ".wav", ".ogg", ".flac", ".mp4", ".webm", ".mkv", ".avi", ".mov",
    ".pdf", ".zip", ".tar", ".gz", ".rar", ".7z", ".dmg", ".exe", ".dll",
    ".pyc", ".pyo", ".class", ".o", ".so", ".dylib", ".wasm",
    ".min.js", ".min.css", ".map",
    ".env", ".pem", ".key", ".cert", ".p12",
}

EXCLUDED_FILES = {
    "package-lock.json", "yarn.lock", "poetry.lock", "pnpm-lock.yaml",
    "Pipfile.lock", "composer.lock", "Gemfile.lock", "cargo.lock",
    ".env", ".env.local", ".env.production", ".env.development",
    ".DS_Store", "Thumbs.db", ".gitignore", ".gitattributes",
    ".editorconfig", ".prettierignore", ".eslintignore",
}

TEST_PATTERNS = [
    re.compile(r"\.test\.[a-z]+$", re.IGNORECASE),
    re.compile(r"\.spec\.[a-z]+$", re.IGNORECASE),
    re.compile(r"_test\.[a-z]+$", re.IGNORECASE),
    re.compile(r"test_[^/\\]+\.[a-z]+$", re.IGNORECASE),
]


def compress_whitespace(content: str) -> str:
    """
    - Strip trailing spaces on every line
    - Collapse 3+ consecutive blank lines → 1 blank line
    - Remove leading/trailing blank lines from the whole block
    """
    lines = [line.rstrip() for line in content.splitlines()]
    compressed = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return compressed.strip()


def xml_escape(text: str) -> str:
    """Minimal XML escaping — only the characters that break XML parsing."""
    return text.replace("]]>", "]]]]><![CDATA[>")


def build_tree(project_dir: str, included_files: list[str]) -> str:
    """
    Build a compact indented tree of included files.
    Gives the LLM immediate structural context before reading any code.
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
        tree_lines.append("  " * (len(parts) - 1) + parts[-1])

    return "\n".join(tree_lines)


def document_project_core(
    project_dir: str,
    compress_ws: bool = True,
    include_tests: bool = False,
    output_dir_override: str | None = None,
) -> tuple[str, dict]:
    """
    Scans a project directory and writes a compact XML file.
    Returns (output_file_path, stats_dict).
    """
    project_dir = os.path.abspath(project_dir)
    folder_name = os.path.basename(os.path.normpath(project_dir))
    out_dir = os.path.expanduser(output_dir_override or STATIC_OUTPUT_BASE)
    os.makedirs(out_dir, exist_ok=True)
    output_file = os.path.join(out_dir, f"{folder_name}.xml")

    included_files: list[str] = []
    excluded_files: list[str] = []
    excluded_dirs_log: list[str] = []
    file_entries: list[tuple[str, str]] = []

    print(f"\nScanning : {project_dir}")
    print(f"Output   : {output_file}\n")

    for dirpath, dirnames, filenames in os.walk(project_dir, topdown=True):
        removed = [d for d in dirnames if d in EXCLUDED_DIRS]
        for d in removed:
            excluded_dirs_log.append(
                os.path.relpath(os.path.join(dirpath, d), project_dir)
            )
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        for filename in filenames:
            full_path = os.path.join(dirpath, filename)
            rel_path = os.path.relpath(full_path, project_dir).replace("\\", "/")
            ext = os.path.splitext(filename)[1].lower()
            if filename in EXCLUDED_FILES:
                excluded_files.append(rel_path)
                continue
            if ext in EXCLUDED_EXTENSIONS:
                excluded_files.append(rel_path)
                continue
            if not include_tests and any(p.search(filename) for p in TEST_PATTERNS):
                excluded_files.append(rel_path)
                continue
            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as fh:
                    raw = fh.read()
            except Exception as e:
                print(f"  ⚠ Could not read: {rel_path} ({e})")
                excluded_files.append(rel_path)
                continue

            content = raw
            if compress_ws:
                content = compress_whitespace(content)
            if not content.strip():
                excluded_files.append(rel_path + "  [empty after processing]")
                continue

            included_files.append(rel_path)
            file_entries.append((rel_path, content))
            print(f"  + {rel_path}")

    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    tree_str = build_tree(project_dir, included_files)

    try:
        with open(output_file, "w", encoding="utf-8") as out:
            out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            out.write(f'<project name="{folder_name}" generated="{timestamp}" '
                      f'files="{len(included_files)}" '
                      f'compress_whitespace="{str(compress_ws).lower()}">\n\n')

            out.write("<structure>\n")
            out.write(f"<![CDATA[\n{tree_str}\n]]>\n")
            out.write("</structure>\n\n")
            out.write("<files>\n\n")
            for rel_path, content in file_entries:
                ext = os.path.splitext(rel_path)[1].lstrip(".")
                out.write(f'<file path="{rel_path}" lang="{ext}">\n')
                out.write(f"<![CDATA[\n{xml_escape(content)}\n]]>\n")
                out.write("</file>\n\n")
            out.write("</files>\n\n")
            out.write("<meta>\n")
            out.write(f"  <included_count>{len(included_files)}</included_count>\n")
            out.write(f"  <excluded_count>{len(excluded_files)}</excluded_count>\n")
            if excluded_files:
                out.write("  <excluded>\n")
                for f in sorted(excluded_files):
                    out.write(f"    <f>{f}</f>\n")
                out.write("  </excluded>\n")
            out.write("</meta>\n\n")
            out.write("</project>\n")

    except Exception as e:
        raise RuntimeError(f"Failed to write output file: {e}")

    stats = {
        "included": len(included_files),
        "excluded": len(excluded_files),
        "excluded_dirs": len(excluded_dirs_log),
    }
    return output_file, stats

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
            output_dir_override=args.output_dir,
        )
    except Exception as e:
        show_error("Error", str(e))
        return
    msg = (
        f"Documentation complete.\n\n"
        f"Included files : {stats['included']}\n"
        f"Excluded files : {stats['excluded']}\n"
        f"Excluded dirs  : {stats['excluded_dirs']}\n\n"
        f"Saved to:\n{output_file}"
    )
    print("\n" + msg)
    script_name = os.path.basename(sys.argv[0])
    cli_cmd = f'python {script_name} --cli --path "{project_dir}"'
    if args.no_compress:
        cli_cmd += " --no-compress"
    if args.include_tests:
        cli_cmd += " --include-tests"
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
            output_dir_override=args.output_dir,
        )
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    print(f"\nDone.")
    print(f"  Included : {stats['included']} files")
    print(f"  Excluded : {stats['excluded']} files")
    print(f"  Saved to : {output_file}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Project Code Documenter — outputs compact XML for LLM context",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python doc_project.py                              # GUI mode
  python doc_project.py --cli --path ./my-app        # CLI mode
  python doc_project.py --cli --path ./my-app --include-tests
        """,
    )
    parser.add_argument("--cli", action="store_true",
                        help="Run in CLI mode (no GUI)")
    parser.add_argument("--path",
                        help="Project directory path (required in CLI mode)")
    parser.add_argument("--no-compress", action="store_true",
                        help="Keep original whitespace")
    parser.add_argument("--include-tests", action="store_true",
                        help="Include test/spec files (excluded by default)")
    parser.add_argument("--output-dir",
                        help="Override default output directory")
    args = parser.parse_args()
    if args.cli:
        run_cli_mode(args)
    else:
        try:
            _get_tkinter_root()
        except tk.TclError:
            print(
                "ERROR: GUI unavailable. Use:\n"
                "  python doc_project.py --cli --path <dir>"
            )
            sys.exit(1)
        run_gui_mode(args)
if __name__ == "__main__":
    main()