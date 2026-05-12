#!/usr/bin/env python3
"""
remove_watermarks.py
--------------------
Removes common Spanish ebook watermark images and their HTML markup from EPUBs.

Handles:
  Lectulandia / EPL:
    - Images/EPL_logo.png  +  <img alt="Logo" class="ancho_full" ...>
    - Images/ex_libris.png +  <div class="bloque_exlibris">...</div>

  ePUBgratis / epubgratis.me:
    - Images/ePUBlogo.png  +  <p class="ePUBmarca"><span><img .../></span></p>
    - Images/epubgratis.png + <p class="ePUBgratis"><span><img .../></span></p>

For each match the script:
  1. Removes the image file from the ZIP
  2. Removes the HTML markup from all XHTML/HTML files
  3. Removes the <item> entry from the OPF manifest

Usage:
    python remove_watermarks.py /path/to/epub/folder [options]

Options:
    --output  DIR   Write cleaned EPUBs here instead of overwriting originals
    --backup        Save a .epub.bak copy of each original before modifying
    --dry-run       Report matches without changing any files
"""

import argparse
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path


# ── Watermark definitions ─────────────────────────────────────────────────────
#
# Each entry:
#   "images"   - set of lowercase image basenames to delete from the ZIP
#   "html"     - list of (compiled_regex, description) to strip from HTML files
#   "opf_re"   - regex to remove matching <item> lines from the OPF manifest

WATERMARKS = [
    {
        "name": "Lectulandia EPL logo",
        "images": {"epl_logo.png"},
        "html": [
            (
                re.compile(
                    r'<img\b[^>]*\bsrc=["\'][^"\']*EPL_logo\.png["\'][^>]*/?>',
                    re.IGNORECASE,
                ),
                "EPL_logo <img> tag",
            ),
        ],
        "opf_re": re.compile(
            r'[ \t]*<item\b[^>]*\bhref=["\'][^"\']*EPL_logo\.png["\'][^>]*/?>[ \t]*\n?',
            re.IGNORECASE,
        ),
    },
    {
        "name": "Lectulandia ex libris",
        "images": {"ex_libris.png"},
        "html": [
            (
                re.compile(
                    r'<div\b[^>]*\bclass=["\'][^"\']*bloque_exlibris[^"\']*["\'][^>]*>.*?</div\s*>',
                    re.IGNORECASE | re.DOTALL,
                ),
                "bloque_exlibris div",
            ),
        ],
        "opf_re": re.compile(
            r'[ \t]*<item\b[^>]*\bhref=["\'][^"\']*ex_libris\.png["\'][^>]*/?>[ \t]*\n?',
            re.IGNORECASE,
        ),
    },
    {
        "name": "ePUBgratis ePUBlogo",
        "images": {"epublogo.png"},
        "html": [
            (
                re.compile(
                    r'<p\b[^>]*\bclass=["\'][^"\']*ePUBmarca[^"\']*["\'][^>]*>.*?</p\s*>',
                    re.IGNORECASE | re.DOTALL,
                ),
                "ePUBmarca <p> block",
            ),
        ],
        "opf_re": re.compile(
            r'[ \t]*<item\b[^>]*\bhref=["\'][^"\']*ePUBlogo\.png["\'][^>]*/?>[ \t]*\n?',
            re.IGNORECASE,
        ),
    },
    {
        "name": "ePUBgratis epubgratis",
        "images": {"epubgratis.png"},
        "html": [
            (
                re.compile(
                    r'<p\b[^>]*\bclass=["\'][^"\']*ePUBgratis[^"\']*["\'][^>]*>.*?</p\s*>',
                    re.IGNORECASE | re.DOTALL,
                ),
                "ePUBgratis <p> block",
            ),
        ],
        "opf_re": re.compile(
            r'[ \t]*<item\b[^>]*\bhref=["\'][^"\']*epubgratis\.png["\'][^>]*/?>[ \t]*\n?',
            re.IGNORECASE,
        ),
    },
]

# All target image basenames (lowercase) for quick lookup
ALL_TARGET_IMAGES = {img for w in WATERMARKS for img in w["images"]}


# ── OPF locator ───────────────────────────────────────────────────────────────

def find_opf(epub_zip: zipfile.ZipFile) -> str | None:
    try:
        data = epub_zip.read("META-INF/container.xml").decode("utf-8", errors="replace")
        m = re.search(r'full-path=["\']([^"\']+\.opf)["\']', data, re.IGNORECASE)
        if m:
            return m.group(1)
    except KeyError:
        pass
    for name in epub_zip.namelist():
        if name.endswith(".opf"):
            return name
    return None


# ── HTML cleaner ──────────────────────────────────────────────────────────────

def clean_html(content: str) -> tuple[str, list[str]]:
    changes = []
    # Apply all HTML patterns from all watermark definitions
    for wm in WATERMARKS:
        for pattern, description in wm["html"]:
            new_content, n = pattern.subn("", content)
            if n:
                changes.append(f"removed {n} {description}")
                content = new_content
    if changes:
        content = re.sub(r'\n{3,}', '\n\n', content)
    return content, changes


# ── OPF cleaner ───────────────────────────────────────────────────────────────

def clean_opf(content: str) -> tuple[str, list[str]]:
    changes = []
    for wm in WATERMARKS:
        new_content, n = wm["opf_re"].subn("", content)
        if n:
            changes.append(f"removed {n} OPF manifest item(s) for {wm['name']}")
            content = new_content
    return content, changes


# ── EPUB processor ────────────────────────────────────────────────────────────

def process_epub(
    src: Path,
    dst: Path,
    dry_run: bool,
) -> tuple[bool, list[str]]:
    all_changes = []

    try:
        with zipfile.ZipFile(src, "r") as zin:
            names = zin.namelist()

            html_files = [n for n in names if n.lower().endswith((".xhtml", ".html", ".htm"))]
            opf_files  = [n for n in names if n.lower().endswith(".opf")]
            images_to_drop = {
                n for n in names
                if Path(n).name.lower() in ALL_TARGET_IMAGES
            }

            planned = {}  # zip_path -> new bytes, or None = delete

            for path in images_to_drop:
                planned[path] = None
                all_changes.append(f"delete image: {path}")

            for path in html_files:
                raw = zin.read(path)
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text = raw.decode("latin-1")
                cleaned, changes = clean_html(text)
                if changes:
                    planned[path] = cleaned.encode("utf-8")
                    for c in changes:
                        all_changes.append(f"{path}: {c}")

            for path in opf_files:
                raw = zin.read(path)
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text = raw.decode("latin-1")
                cleaned, changes = clean_opf(text)
                if changes:
                    planned[path] = cleaned.encode("utf-8")
                    for c in changes:
                        all_changes.append(f"{path}: {c}")

            if not all_changes or dry_run:
                return bool(all_changes), all_changes

            tmp_fd, tmp_path = tempfile.mkstemp(suffix=".epub", dir=dst.parent)
            try:
                with os.fdopen(tmp_fd, "wb") as tmp_f:
                    with zipfile.ZipFile(tmp_f, "w", compression=zipfile.ZIP_DEFLATED) as zout:
                        if "mimetype" in names:
                            zout.writestr(
                                zin.getinfo("mimetype"),
                                zin.read("mimetype"),
                                compress_type=zipfile.ZIP_STORED,
                            )
                        for item in zin.infolist():
                            if item.filename == "mimetype":
                                continue
                            if item.filename in planned and planned[item.filename] is None:
                                continue  # deleted
                            elif item.filename in planned:
                                zout.writestr(item, planned[item.filename])
                            else:
                                zout.writestr(item, zin.read(item.filename))
                shutil.move(tmp_path, dst)
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise

    except zipfile.BadZipFile:
        return False, ["ERROR: not a valid ZIP/EPUB"]
    except Exception as exc:
        return False, [f"ERROR: {exc}"]

    return True, all_changes


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Remove Spanish ebook watermarks (Lectulandia, ePUBgratis) from EPUBs."
    )
    parser.add_argument("folder", help="Folder containing EPUB files (searched recursively)")
    parser.add_argument("--output",  default=None, help="Output folder (default: overwrite originals)")
    parser.add_argument("--backup",  action="store_true", help="Save .epub.bak copy of each original")
    parser.add_argument("--dry-run", action="store_true", help="Report matches without changing files")
    args = parser.parse_args()

    src_folder = Path(args.folder).resolve()
    if not src_folder.is_dir():
        sys.exit(f"Error: '{src_folder}' is not a directory.")

    epubs = sorted(src_folder.rglob("*.epub"))
    if not epubs:
        sys.exit(f"No .epub files found in '{src_folder}'.")

    out_folder = Path(args.output).resolve() if args.output else None
    if out_folder:
        out_folder.mkdir(parents=True, exist_ok=True)

    total = len(epubs)
    print(f"Found {total} EPUB file(s) in '{src_folder}'")
    if args.dry_run:
        print("DRY-RUN mode — no files will be modified.\n")
    print()

    counts = {"modified": 0, "clean": 0, "error": 0}

    for i, epub_path in enumerate(epubs, 1):
        rel = epub_path.relative_to(src_folder)

        if out_folder:
            dst = out_folder / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not args.dry_run:
                shutil.copy2(epub_path, dst)
        else:
            dst = epub_path

        if args.backup and not args.dry_run:
            shutil.copy2(epub_path, epub_path.with_suffix(".epub.bak"))

        modified, changes = process_epub(src=epub_path, dst=dst, dry_run=args.dry_run)

        if changes and changes[0].startswith("ERROR"):
            counts["error"] += 1
            print(f"[{i:>4}/{total}] {rel}")
            for c in changes:
                print(f"           {c}")
        elif modified:
            counts["modified"] += 1
            tag = "DRY-RUN" if args.dry_run else "CLEANED"
            print(f"[{i:>4}/{total}] {rel}  →  {tag}")
            for c in changes:
                print(f"           • {c}")
        else:
            counts["clean"] += 1
            print(f"[{i:>4}/{total}] {rel}  →  clean")

    print()
    print("─" * 60)
    if args.dry_run:
        print(f"Dry-run complete.  Would clean: {counts['modified']}  Already clean: {counts['clean']}  Errors: {counts['error']}")
    else:
        print(f"Done.  Cleaned: {counts['modified']}  Already clean: {counts['clean']}  Errors: {counts['error']}")


if __name__ == "__main__":
    main()
