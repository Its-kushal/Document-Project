import os
import tkinter as tk
from tkinter import filedialog, messagebox
import subprocess
import shutil
import sys

def _is_zenity_available():
    """Checks if 'zenity' command is available on the system."""
    return shutil.which('zenity') is not None

_USE_ZENITY = _is_zenity_available()

def show_info(title, message):
    """Shows an info message box, using zenity if available."""
    if _USE_ZENITY:
        try:
            subprocess.run(
                ['zenity', '--info', f'--title={title}', f'--text={message}'],
                check=True
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            _show_info_tkinter(title, message)
    else:
        _show_info_tkinter(title, message)

def show_error(title, message):
    """Shows an error message box, using zenity if available."""
    if _USE_ZENITY:
        try:
            subprocess.run(
                ['zenity', '--error', f'--title={title}', f'--text={message}'],
                check=True
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            _show_error_tkinter(title, message)
    else:
        _show_error_tkinter(title, message)

def ask_directory(title):
    """Asks for a directory, using zenity if available."""
    if _USE_ZENITY:
        try:
            result = subprocess.run(
                ['zenity', '--file-selection', '--directory', f'--title={title}'],
                capture_output=True, text=True, check=True
            )
            # .strip() is crucial to remove trailing newlines
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            # User likely clicked "Cancel" (non-zero exit code)
            return None
        except FileNotFoundError:
            return _ask_directory_tkinter(title)
    else:
        return _ask_directory_tkinter(title)

def ask_save_file(title):
    """Asks for a save-file path, using zenity if available."""
    if _USE_ZENITY:
        try:
            result = subprocess.run(
                [
                    'zenity', '--file-selection', '--save', '--confirm-overwrite',
                    f'--title={title}',
                    '--file-filter=Text Files (*.txt) | *.txt',
                    '--file-filter=All Files (*.*) | *'
                ],
                capture_output=True, text=True, check=True
            )
            path = result.stdout.strip()
            # Zenity doesn't automatically add the extension
            if not path.lower().endswith('.txt'):
                path += '.txt'
            return path
        except subprocess.CalledProcessError:
            # User likely clicked "Cancel"
            return None
        except FileNotFoundError:
            return _ask_save_file_tkinter(title)
    else:
        return _ask_save_file_tkinter(title)

# --- Original Tkinter Fallbacks ---

def _setup_tkinter_root():
    """Creates a hidden root window for Tkinter dialogs."""
    if not tk._default_root:
        root = tk.Tk()
        root.withdraw()
        return root
    return tk._default_root

def _show_info_tkinter(title, message):
    _setup_tkinter_root()
    messagebox.showinfo(title, message)

def _show_error_tkinter(title, message):
    _setup_tkinter_root()
    messagebox.showerror(title, message)

def _ask_directory_tkinter(title):
    _setup_tkinter_root()
    return filedialog.askdirectory(title=title)

def _ask_save_file_tkinter(title):
    _setup_tkinter_root()
    return filedialog.asksaveasfilename(
        title=title,
        defaultextension=".txt",
        filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")]
    )

# --- Main Application Logic ---

def document_code_files():
    """
    Walks through a selected directory, finds code and media files,
    and writes their paths and contents to a single text file.
    """
    # --- Configuration ---
    CODE_EXTENSIONS = {
        '.py', '.js', '.ts', '.java', '.c', '.cpp', '.h', '.hpp', '.cs',
        '.html', '.css', '.scss', '.go', '.rs', '.swift', '.kt', '.rb',
        '.php', '.pl', '.sh', '.bat', '.sql', '.xml', '.json', '.yml', '.yaml',
        '.md', '.r', '.m', '.lua', '.toml', '.ini', 'dockerfile'
    }
    
    # NEW: Extensions for media/asset files to list
    MEDIA_EXTENSIONS = {
        # Images
        '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.svg', '.webp', '.ico',
        # Fonts
        '.ttf', '.otf', '.woff', '.woff2',
        # Audio
        '.mp3', '.wav', '.ogg', '.flac',
        # Video
        '.mp4', '.webm', '.mkv', '.avi',
        # Docs
        '.pdf'
    }

    EXCLUDED_DIRS = {
        'node_modules', 'venv', '.venv', '.git', '__pycache__',
        '.vscode', '.idea', 'build', 'dist', 'target', 'env', '.DS_Store'
    }
    
    EXCLUDED_FILES = {
        'package-lock.json', 'yarn.lock', 'poetry.lock'
    }

    # --- 1. Prompt user to select the project directory ---
    show_info(
        "Code Documenter",
        "Please select the project directory you want to document."
    )
    project_dir = ask_directory(title="Select Project Directory")

    if not project_dir:
        print("No directory selected. Exiting.")
        return

    # --- 2. Prompt user to select the output file ---
    show_info(
        "Code Documenter",
        "Please select where to save the combined text file."
    )
    output_file = ask_save_file(title="Save Documentation File As...")

    if not output_file:
        print("No output file selected. Exiting.")
        return

    # --- 3. Walk directory and write files ---
    print(f"Starting documentation...\nProject Directory: {project_dir}\nOutput File: {output_file}")
    
    code_file_count = 0
    media_files_found = []

    try:
        with open(output_file, 'w', encoding='utf-8') as outfile:
            for dirpath, dirnames, filenames in os.walk(project_dir, topdown=True):
                
                # --- Prune excluded directories ---
                dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
                
                for filename in filenames:
                    if filename in EXCLUDED_FILES:
                        continue
                        
                    file_ext = os.path.splitext(filename)[1].lower()
                    if not file_ext and filename.lower() == 'dockerfile':
                        file_ext = 'dockerfile' # Handle extensionless files
                    
                    file_path = os.path.join(dirpath, filename)
                    relative_path = os.path.relpath(file_path, project_dir)
                    
                    if file_ext in CODE_EXTENSIONS:
                        print(f"Processing Code: {relative_path}")
                        
                        outfile.write("=" * 80 + "\n")
                        outfile.write(f"--- FILE: {relative_path} ---\n")
                        outfile.write("=" * 80 + "\n\n")
                        
                        try:
                            with open(file_path, 'r', encoding='utf-8', errors='ignore') as infile:
                                content = infile.read()
                                outfile.write(content)
                            
                            outfile.write("\n\n\n")
                            code_file_count += 1
                            
                        except Exception as e:
                            print(f"  Error reading {file_path}: {e}")
                            outfile.write(f"*** Error reading file: {e} ***\n\n\n")

                    elif file_ext in MEDIA_EXTENSIONS:
                        print(f"Found Asset: {relative_path}")
                        media_files_found.append(relative_path)

    except Exception as e:
        print(f"An error occurred while writing the output file: {e}")
        show_error("Error", f"An error occurred: {e}")
        return

    if media_files_found:
        print(f"Appending {len(media_files_found)} asset file paths...")
        try:
            with open(output_file, 'a', encoding='utf-8') as outfile:
                outfile.write("\n\n" + "#" * 80 + "\n")
                outfile.write(f"--- ASSET / MEDIA FILE LIST ({len(media_files_found)} files) ---\n")
                outfile.write("#" * 80 + "\n\n")
                
                media_files_found.sort()
                for relative_path in media_files_found:
                    outfile.write(f"{relative_path}\n")
                    
        except Exception as e:
            print(f"An error occurred while appending media file list: {e}")
            show_error("Error", f"An error occurred while appending media list: {e}")

    success_message = (
        f"Documentation complete!\n\n"
        f"Processed {code_file_count} code files.\n"
        f"Found {len(media_files_found)} asset/media files.\n\n"
        f"Saved to: {output_file}"
    )
    print(success_message)
    show_info("Success", success_message)

if __name__ == "__main__":
    if sys.platform != "darwin": # Don't hide root on macOS, it can cause issues
        try:
            _setup_tkinter_root()
        except tk.TclError:
            print("Could not initialize GUI. Are you in a headless environment?")
            sys.exit(1)
            
    document_code_files()