#!/usr/bin/env python3
"""
Usage:
  Interactive:  python rebuild_project.py
  CLI:          python rebuild_project.py --xml path/to/Project.xml --out path/to/restore_dir
  Options:
    --force        Write into a non-empty output directory anyway
    --dry-run      Validate + show what would be created, write nothing
"""

import os
import sys
import argparse
import xml.etree.ElementTree as ET


class NotDocProjectXML(Exception):
    """Raised when the input XML doesn't match doc_project_new.py's output shape."""
    pass


REQUIRED_ROOT_ATTRS = {"name", "generated", "files", "compress_whitespace"}


def validate_and_parse(xml_path: str) -> ET.Element:
    """
    Parses the XML and verifies it matches the specific structure
    doc_project_new.py emits:

        <project name="..." generated="..." files="N" compress_whitespace="true|false">
          <structure> ... CDATA tree ... </structure>
          <files>
            <file path="..." lang="...">CDATA content</file>
            ...
          </files>
          <meta>
            <included_count>N</included_count>
            <excluded_count>N</excluded_count>
            [<excluded>...</excluded>]
          </meta>
        </project>

    Raises NotDocProjectXML with a human-readable reason on any mismatch.
    Returns the parsed root <project> element on success.
    """
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as e:
        raise NotDocProjectXML(f"File is not valid XML at all ({e}).")

    root = tree.getroot()

    if root.tag != "project":
        raise NotDocProjectXML(
            f"Root element is <{root.tag}>, expected <project>. "
            "This isn't a doc_project_new.py export."
        )

    missing_attrs = REQUIRED_ROOT_ATTRS - set(root.attrib.keys())
    if missing_attrs:
        raise NotDocProjectXML(
            f"<project> is missing required attribute(s): {sorted(missing_attrs)}. "
            "doc_project_new.py always writes name/generated/files/compress_whitespace."
        )

    if root.attrib.get("compress_whitespace") not in ("true", "false"):
        raise NotDocProjectXML(
            "compress_whitespace attribute must be 'true' or 'false' — "
            "value doesn't look like it came from doc_project_new.py."
        )

    structure_el = root.find("structure")
    if structure_el is None:
        raise NotDocProjectXML("Missing <structure> block.")

    files_el = root.find("files")
    if files_el is None:
        raise NotDocProjectXML("Missing <files> block.")

    file_els = files_el.findall("file")
    if not file_els:
        raise NotDocProjectXML("<files> block contains no <file> entries.")

    for f in file_els:
        if "path" not in f.attrib or "lang" not in f.attrib:
            raise NotDocProjectXML(
                "A <file> entry is missing 'path' or 'lang' attribute — "
                "doc_project_new.py always sets both."
            )

    meta_el = root.find("meta")
    if meta_el is None:
        raise NotDocProjectXML("Missing <meta> block.")

    if meta_el.find("included_count") is None or meta_el.find("excluded_count") is None:
        raise NotDocProjectXML(
            "<meta> is missing <included_count>/<excluded_count> — "
            "not a recognized doc_project_new.py export."
        )

    # Cross-check the declared file count against what's actually there.
    declared = root.attrib.get("files")
    try:
        declared_n = int(declared)
        if declared_n != len(file_els):
            raise NotDocProjectXML(
                f"<project files=\"{declared}\"> doesn't match the actual number "
                f"of <file> entries found ({len(file_els)}). File looks corrupted "
                "or hand-edited."
            )
    except ValueError:
        raise NotDocProjectXML(f"<project files=\"{declared}\"> is not an integer.")

    included_count_el = meta_el.find("included_count")
    try:
        meta_included = int((included_count_el.text or "").strip())
        if meta_included != len(file_els):
            raise NotDocProjectXML(
                f"<meta><included_count> ({meta_included}) doesn't match the number "
                f"of <file> entries ({len(file_els)}). File looks corrupted or "
                "hand-edited."
            )
    except ValueError:
        raise NotDocProjectXML("<meta><included_count> is not an integer.")

    return root


# ---------------------------------------------------------------------------
# Reconstruction
# ---------------------------------------------------------------------------

def extract_file_content(file_el: ET.Element) -> str:
    """
    Pulls the file's original content back out of its CDATA block.

    doc_project_new.py writes each file as:
        <file path="..." lang="...">
        <![CDATA[
        <content>
        ]]]]><![CDATA[>
        </file>

    A standards-compliant XML parser (ElementTree included) already merges
    the adjacent CDATA sections into one text node and hands back the
    literal content, so all we need to do is strip the single leading and
    single trailing newline that doc_project_new.py adds around the content
    when it writes "<![CDATA[\\n{content}\\n]]]]><![CDATA[>\\n".
    """
    text = file_el.text or ""
    if text.startswith("\n"):
        text = text[1:]
    if text.endswith("\n"):
        text = text[:-1]
    return text


def safe_join(base_dir: str, rel_path: str) -> str:
    """
    Joins rel_path onto base_dir and guarantees the result can't escape
    base_dir (blocks '../../etc/passwd'-style paths from a tampered XML).
    """
    rel_path_norm = rel_path.replace("\\", "/").lstrip("/")
    full = os.path.normpath(os.path.join(base_dir, rel_path_norm))
    base_abs = os.path.abspath(base_dir)
    full_abs = os.path.abspath(full)
    if not (full_abs == base_abs or full_abs.startswith(base_abs + os.sep)):
        raise NotDocProjectXML(
            f"Refusing to write outside the output directory: '{rel_path}' "
            "resolves outside the target folder."
        )
    return full_abs


def rebuild(root: ET.Element, out_dir: str, dry_run: bool = False) -> dict:
    files_el = root.find("files")
    file_els = files_el.findall("file")

    written = []
    for file_el in file_els:
        rel_path = file_el.attrib["path"]
        content = extract_file_content(file_el)
        dest_path = safe_join(out_dir, rel_path)

        if dry_run:
            written.append(dest_path)
            continue

        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "w", encoding="utf-8") as f:
            f.write(content)
        written.append(dest_path)

    return {
        "project_name": root.attrib.get("name"),
        "generated": root.attrib.get("generated"),
        "compress_whitespace": root.attrib.get("compress_whitespace"),
        "written_count": len(written),
        "written_paths": written,
    }


# ---------------------------------------------------------------------------
# CLI / interactive entry point
# ---------------------------------------------------------------------------

def prompt_for_inputs(args):
    xml_path = args.xml
    out_dir = args.out

    print("Example:\n\tPath to the doc_project_new.py XML file: /home/kushal/Desktop/Projects/Document_Project/output_docs/TradingAgents.xml\n\tDirectory to rebuild the project into (will be created): /home/kushal/Desktop/restored/TradingAgents")
    if not xml_path:
        xml_path = input("Path to the doc_project_new.py XML file: ").strip().strip('"')

    if not out_dir:
        out_dir = input("Directory to rebuild the project into (will be created): ").strip().strip('"')

    xml_path = os.path.expanduser(xml_path)
    out_dir = os.path.expanduser(out_dir)
    return xml_path, out_dir


def main():
    parser = argparse.ArgumentParser(
        description="Rebuild a project directory from a doc_project_new.py XML export.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python rebuild_project.py
  python rebuild_project.py --xml Document_Project.xml --out ./restored
  python rebuild_project.py --xml Document_Project.xml --out ./restored --dry-run
        """,
    )
    parser.add_argument("--xml", help="Path to the XML file to rebuild from")
    parser.add_argument("--out", help="Output directory to recreate the project in")
    parser.add_argument("--force", action="store_true",
                         help="Allow writing into a non-empty output directory")
    parser.add_argument("--dry-run", action="store_true",
                         help="Validate and list what would be written, without writing")
    args = parser.parse_args()

    xml_path, out_dir = prompt_for_inputs(args)

    if not os.path.isfile(xml_path):
        print(f"ERROR: '{xml_path}' does not exist or is not a file.")
        sys.exit(1)

    print(f"\nValidating: {xml_path}")
    try:
        root = validate_and_parse(xml_path)
    except NotDocProjectXML as e:
        print(f"\nREJECTED: This XML file was not generated by doc_project_new.py.")
        print(f"Reason: {e}")
        sys.exit(1)

    print("OK — file matches the doc_project_new.py export format.")
    print(f"  Project name : {root.attrib.get('name')}")
    print(f"  Generated at : {root.attrib.get('generated')}")
    print(f"  File count   : {root.attrib.get('files')}")
    print(f"  Compressed   : {root.attrib.get('compress_whitespace')}")

    if os.path.exists(out_dir):
        existing = os.listdir(out_dir)
        if existing and not args.force and not args.dry_run:
            print(f"\nERROR: Output directory '{out_dir}' already exists and is not empty.")
            print("Re-run with --force to write into it anyway, or choose an empty/new directory.")
            sys.exit(1)
    else:
        if not args.dry_run:
            os.makedirs(out_dir, exist_ok=True)

    print(f"\n{'[DRY RUN] Would rebuild' if args.dry_run else 'Rebuilding'} into: {out_dir}\n")

    try:
        result = rebuild(root, out_dir, dry_run=args.dry_run)
    except NotDocProjectXML as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    for p in result["written_paths"]:
        print(f"  {'would write' if args.dry_run else 'wrote'}: {p}")

    print(f"\n{'Would write' if args.dry_run else 'Wrote'} {result['written_count']} file(s).")
    if not args.dry_run:
        print(f"Done. Project restored to: {os.path.abspath(out_dir)}")


if __name__ == "__main__":
    main()