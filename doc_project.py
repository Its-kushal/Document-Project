import os
import tkinter as tk
from tkinter import filedialog, messagebox

def document_code_files():
    """
    Walks through a selected directory, finds code files, and writes their
    paths and contents to a single user-specified text file.
    """
    
    # --- Configuration ---
    # Add or remove extensions as needed for your project
    CODE_EXTENSIONS = {
        '.py', '.js', '.ts', '.java', '.c', '.cpp', '.h', '.hpp', '.cs',
        '.html', '.css', '.scss', '.go', '.rs', '.swift', '.kt', '.rb',
        '.php', '.pl', '.sh', '.bat', '.sql', '.xml', '.json', '.yml', '.yaml',
        '.md', '.r', '.m', '.lua'
    }

    # Add or remove directory names to exclude (case-sensitive)
    # os.walk will be pruned and will not enter these directories.
    EXCLUDED_DIRS = {
        'node_modules',
        'venv',
        '.venv'
        '.git',
        '__pycache__',
        '.vscode',
        '.idea',
        'build',
        'dist'
    }
    
    # Add or remove specific filenames to exclude
    EXCLUDED_FILES = {
        '.DS_Store',
        'package-lock.json',
        'yarn.lock'
    }

    # --- Setup Tkinter Root Window ---
    # We don't need the main window, just the dialogs
    root = tk.Tk()
    root.withdraw()

    # --- 1. Prompt user to select the project directory ---
    messagebox.showinfo(
        "Code Documenter",
        "Please select the project directory you want to document."
    )
    project_dir = filedialog.askdirectory(title="Select Project Directory")

    if not project_dir:
        print("No directory selected. Exiting.")
        return

    # --- 2. Prompt user to select the output file ---
    messagebox.showinfo(
        "Code Documenter",
        "Please select where to save the combined text file."
    )
    output_file = filedialog.asksaveasfilename(
        title="Save Documentation File As...",
        defaultextension=".txt",
        filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")]
    )

    if not output_file:
        print("No output file selected. Exiting.")
        return

    # --- 3. Walk directory and write files ---
    print(f"Starting documentation...\nProject Directory: {project_dir}\nOutput File: {output_file}")
    
    file_count = 0
    try:
        with open(output_file, 'w', encoding='utf-8') as outfile:
            # os.walk traverses the directory tree
            for dirpath, dirnames, filenames in os.walk(project_dir):
                
                # --- Prune excluded directories ---
                # We modify dirnames *in place* to stop os.walk from descending
                # into them. We iterate over a copy (dirnames[:])
                # so we can modify the original list.
                dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
                
                for filename in filenames:
                    # Skip excluded files
                    if filename in EXCLUDED_FILES:
                        continue
                        
                    # Check if the file has a code extension
                    file_ext = os.path.splitext(filename)[1].lower()
                    
                    if file_ext in CODE_EXTENSIONS:
                        file_path = os.path.join(dirpath, filename)
                        
                        # Get a relative path for cleaner output
                        relative_path = os.path.relpath(file_path, project_dir)
                        
                        print(f"Processing: {relative_path}")
                        
                        # Write a clear header for each file
                        outfile.write("=" * 80 + "\n")
                        outfile.write(f"--- FILE: {relative_path} ---\n")
                        outfile.write("=" * 80 + "\n\n")
                        
                        try:
                            # Read the content of the code file
                            with open(file_path, 'r', encoding='utf-8', errors='ignore') as infile:
                                content = infile.read()
                                outfile.write(content)
                            
                            # Add spacing between files
                            outfile.write("\n\n\n")
                            file_count += 1
                            
                        except Exception as e:
                            print(f"  Error reading {file_path}: {e}")
                            outfile.write(f"*** Error reading file: {e} ***\n\n\n")

    except Exception as e:
        print(f"An error occurred while writing the output file: {e}")
        messagebox.showerror("Error", f"An error occurred: {e}")
        return

    # --- 4. Final confirmation ---
    success_message = f"Documentation complete! \n\nProcessed {file_count} code files.\n\nSaved to: {output_file}"
    print(success_message)
    messagebox.showinfo("Success", success_message)

if __name__ == "__main__":
    document_code_files()

