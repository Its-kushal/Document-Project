import os
import sys
import argparse
import tkinter as tk
from tkinter import filedialog, messagebox


STATIC_OUTPUT_BASE = "~/Desktop/Projects/Document_Project/output_docs"

def _get_tkinter_root():
    """Create and return a hidden Tk root."""
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


def document_project_core(project_dir):
    """
    Scans a project directory, documents all files not excluded, and logs scanning results.
    Returns a tuple (output_file, stats_dict).
    """

    EXCLUDED_DIRS = {
        "node_modules",
        "venv",
        ".venv",
        ".git",
        "__pycache__",
        ".vscode",
        ".idea",
        "build",
        "dist",
        "target",
        "env",
        "output_docs",
        ".next",
    }

    EXCLUDED_EXTENSIONS = {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".svg",
        ".webp",
        ".ico",
        ".csv",
        ".ttf",
        ".otf",
        ".woff",
        ".woff2",
        ".mp3",
        ".wav",
        ".ogg",
        ".flac",
        ".mp4",
        ".webm",
        ".mkv",
        ".avi",
        ".pdf",
        ".env",
    }

    EXCLUDED_FILES = {
        "package-lock.json",
        "yarn.lock",
        "poetry.lock",
        ".env",
    }

    project_dir = os.path.abspath(project_dir)
    folder_name = os.path.basename(os.path.normpath(project_dir))

    output_dir = os.path.expanduser(STATIC_OUTPUT_BASE)
    os.makedirs(output_dir, exist_ok=True)

    output_file = os.path.join(output_dir, f"{folder_name}.txt")

    included_files = []
    excluded_files = []
    excluded_dirs_log = []
    scanned_dirs = []
    scanned_files = []

    print(f"Scanning project: {project_dir}")
    print(f"Output file:      {output_file}\n")

    try:
        with open(output_file, "w", encoding="utf-8") as outfile:

            for dirpath, dirnames, filenames in os.walk(project_dir, topdown=True):
                rel_dir = os.path.relpath(dirpath, project_dir)
                scanned_dirs.append(rel_dir)
                removed = [d for d in dirnames if d in EXCLUDED_DIRS]
                for d in removed:
                    excluded_dirs_log.append(
                        os.path.relpath(os.path.join(dirpath, d), project_dir)
                    )
                dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
                for filename in filenames:
                    rel_path = os.path.relpath(
                        os.path.join(dirpath, filename), project_dir
                    )
                    scanned_files.append(rel_path)
                    ext = os.path.splitext(filename)[1].lower()
                    if filename in EXCLUDED_FILES or ext in EXCLUDED_EXTENSIONS:
                        excluded_files.append(rel_path)
                        continue
                    included_files.append(rel_path)
                    print(f"Including: {rel_path}")
                    outfile.write(f"--- FILE: {rel_path} ---\n\n")
                    full_path = os.path.join(dirpath, filename)
                    try:
                        with open(
                            full_path, "r", encoding="utf-8", errors="ignore"
                        ) as infile:
                            outfile.write(infile.read())
                        outfile.write("\n\n\n")
                    except Exception as e:
                        outfile.write(f"*** Error reading file: {e} ***\n\n\n")
            outfile.write("--- EXCLUDED FILES ---\n")
            for f in sorted(excluded_files):
                outfile.write(f + "\n")
            outfile.write("--- EXCLUDED DIRECTORIES ---\n")
            for d in sorted(excluded_dirs_log):
                outfile.write(d + "\n")
            outfile.write("--- SCANNED DIRECTORIES ---\n")
            for d in sorted(scanned_dirs):
                outfile.write(d + "\n")
            outfile.write("--- SCANNED FILES ---\n")
            for f in sorted(scanned_files):
                outfile.write(f + "\n")

    except Exception as e:
        raise RuntimeError(f"Error writing output file: {e}")
    stats = {
        "included": len(included_files),
        "excluded": len(excluded_files),
        "scanned_dirs": len(scanned_dirs),
        "scanned_files": len(scanned_files),
    }
    return output_file, stats
def run_gui_mode():
    show_info("Code Documenter", "Please select the project directory to document.")
    project_dir = ask_directory("Select Project Directory")
    if not project_dir:
        print("No directory selected. Exiting.")
        return
    try:
        output_file, stats = document_project_core(project_dir)
    except Exception as e:
        show_error("Error", str(e))
        return
    msg = (
        f"Documentation complete.\n\n"
        f"Included files:  {stats['included']}\n"
        f"Excluded files:  {stats['excluded']}\n"
        f"Scanned dirs:    {stats['scanned_dirs']}\n"
        f"Scanned files:   {stats['scanned_files']}\n\n"
        f"Saved to: {output_file}"
    )
    print(msg)
    script_name = os.path.basename(sys.argv[0])
    cli_command = f'python {script_name} --cli --path "{project_dir}"'
    print("\n" + "="*60)
    print("RUN VIA CLI NEXT TIME USING THIS EXACT COMMAND:")
    print(cli_command)
    print("="*60 + "\n")
    show_info("Success", msg)

def run_cli_mode(args):
    if not args.path:
        print("ERROR: --path is required when using --cli")
        sys.exit(1)
    project_dir = args.path
    try:
        output_file, stats = document_project_core(project_dir)
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    print("\nDocumentation complete.")
    print(f"Included files:  {stats['included']}")
    print(f"Excluded files:  {stats['excluded']}")
    print(f"Scanned dirs:    {stats['scanned_dirs']}")
    print(f"Scanned files:   {stats['scanned_files']}")
    print(f"Saved to:        {output_file}\n")

def main():
    parser = argparse.ArgumentParser(description="Project Code Documentor")
    parser.add_argument("--cli", action="store_true", help="Run in CLI mode (no GUI)")
    parser.add_argument("--path", help="Project directory path (required in CLI mode)")
    args = parser.parse_args()
    if args.cli:
        run_cli_mode(args)
    else:
        try:
            _get_tkinter_root()
        except tk.TclError:
            print(
                "ERROR: GUI not available. Use CLI mode: python documentor.py --cli --path <dir>"
            )
            sys.exit(1)
        run_gui_mode()

if __name__ == "__main__":
    main()
