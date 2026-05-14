#!/usr/bin/env python3
"""
fix_html_orphans.py
-------------------
Scans EPUB files for HTML <img> tags that reference image files which no
longer exist inside the EPUB ZIP. Also checks OPF manifest entries for the
same missing files.

Optionally removes the orphaned tags/entries with --fix.

Usage:
    python fix_html_orphans.py /path/to/epub/folder [options]

Options:
    --fix       Actually remove orphaned <img> tags and OPF entries (default: report only)
    --backup    Save a .epub.bak copy before modifying (use with --fix)
    --output    Write fixed EPUBs to a separate folder instead of overwriting
"""

import argparse
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_title(epub_zip: zipfile.ZipFile, opf_path: str | None) -> str | None:
    """Extract the book title from the OPF metadata."""
    if not opf_path:
        return None
    try:
        opf = epub_zip.read(opf_path).decode("utf-8", errors="replace")
        m = re.search(r'<dc:title[^>]*>([^<]+)</dc:title>', opf, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    except KeyError:
        pass
    return None


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


def resolve_path(href: str, current_file: str, namelist_lower: dict) -> str | None:
    """
    Resolve a relative href from an HTML file to its full ZIP path.
    Returns the ZIP path if it exists, None otherwise.
    """
    if href.startswith("http://") or href.startswith("https://") or href.startswith("data:"):
        return "external"  # external/data URIs are fine

    # Directory of the current HTML file within the ZIP
    base_dir = current_file.rsplit("/", 1)[0] if "/" in current_file else ""

    # Resolve relative path segments
    parts = (base_dir + "/" + href).split("/") if base_dir else href.split("/")
    resolved = []
    for part in parts:
        if part == "..":
            if resolved:
                resolved.pop()
        elif part and part != ".":
            resolved.append(part)
    full_path = "/".join(resolved)

    # Check against the actual ZIP contents (case-insensitive)
    return namelist_lower.get(full_path.lower())


def find_img_srcs(html: str) -> list[tuple[str, str]]:
    """
    Extract all (full_tag, src_value) pairs from <img> tags.
    """
    return re.findall(
        r'(<img\b[^>]*\bsrc=["\']([^"\']+)["\'][^>]*/?>)',
        html, re.IGNORECASE
    )


def make_remove_tag_pattern(src: str) -> re.Pattern:
    """
    Build a regex that removes the smallest enclosing block containing this img src.
    Tries (in order):
      1. <p ...><img src="..."/></p>  — paragraph wrapping just the image
      2. <div ...><img src="..."/></div>
      3. <span ...><img src="..."/></span>
      4. Just the bare <img> tag itself
    """
    escaped = re.escape(src)
    # Wrapper patterns — paragraph, div, span containing only whitespace + the img
    for tag in ("p", "div", "span"):
        pattern = re.compile(
            rf'[ \t]*<{tag}\b[^>]*>\s*<img\b[^>]*\bsrc=["\']{{escaped}}["\'][^>]*/?>[ \t]*</{tag}\s*>[ \t]*\n?'
            .replace("{escaped}", escaped),
            re.IGNORECASE | re.DOTALL,
        )
        # Quick check: does it match at all (we return the first pattern that could work)
        # We'll actually test during removal
        pass  # build all and return list

    patterns = []
    for tag in ("p", "div", "span"):
        patterns.append(re.compile(
            rf'[ \t]*<{tag}\b[^>]*>\s*(<img\b[^>]*\bsrc=["\']{{escaped}}["\'][^>]*/?>)\s*</{tag}\s*>[ \t]*\n?'
            .replace("{escaped}", escaped),
            re.IGNORECASE | re.DOTALL,
        ))
    # Bare img tag fallback
    patterns.append(re.compile(
        rf'[ \t]*<img\b[^>]*\bsrc=["\']{{escaped}}["\'][^>]*/?>[ \t]*\n?'
        .replace("{escaped}", escaped),
        re.IGNORECASE,
    ))
    return patterns


def remove_img_tag(html: str, src: str) -> tuple[str, str]:
    """
    Remove the HTML element containing an <img src="..."> from html.
    Returns (new_html, description_of_what_was_removed).
    """
    escaped = re.escape(src)

    # Try removing wrapper elements first (p, div, span with only the img inside)
    for tag in ("p", "div", "span"):
        pattern = re.compile(
            rf'[ \t]*<{tag}\b[^>]*>\s*<img\b[^>]*\bsrc=["\']{{escaped}}["\'][^>]*/?>[ \t]*</{tag}\s*>[ \t]*\n?'
            .replace("{escaped}", escaped),
            re.IGNORECASE | re.DOTALL,
        )
        new_html, n = pattern.subn("", html)
        if n:
            return new_html, f"removed <{tag}><img src=\"{src}\"/></{tag}>"

    # Fallback: bare img tag
    pattern = re.compile(
        rf'[ \t]*<img\b[^>]*\bsrc=["\']{{escaped}}["\'][^>]*/?>[ \t]*\n?'
        .replace("{escaped}", escaped),
        re.IGNORECASE,
    )
    new_html, n = pattern.subn("", html)
    if n:
        return new_html, f"removed <img src=\"{src}\"/>"

    return html, f"WARNING: could not remove <img src=\"{src}\"> (tag not found)"


def remove_opf_item(opf: str, href_basename: str) -> tuple[str, int]:
    """Remove <item> entries from OPF manifest matching the image basename."""
    pattern = re.compile(
        rf'[ \t]*<item\b[^>]*\bhref=["\'][^"\']*{re.escape(href_basename)}["\'][^>]*/?>[ \t]*\n?',
        re.IGNORECASE,
    )
    new_opf, n = pattern.subn("", opf)
    return new_opf, n


# ── Core EPUB checker ─────────────────────────────────────────────────────────

def check_epub(
    src: Path,
    fix: bool,
    dst: Path,
    backup: bool,
) -> tuple[str, list[str]]:
    """
    Returns (status, details).
    status: "orphan" | "clean" | "error"
    """
    try:
        with zipfile.ZipFile(src, "r") as zin:
            names = zin.namelist()

            # Build a lowercase → original-case lookup for path resolution
            namelist_lower = {n.lower(): n for n in names}

            html_files = [n for n in names if n.lower().endswith((".xhtml", ".html", ".htm"))]
            opf_path   = find_opf(zin)

            # Map: missing image ZIP path → list of (html_file, src_as_written)
            orphans: dict[str, list[tuple[str, str]]] = {}

            for html_file in html_files:
                raw = zin.read(html_file)
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text = raw.decode("latin-1")

                for full_tag, src in find_img_srcs(text):
                    resolved = resolve_path(src, html_file, namelist_lower)
                    if resolved == "external":
                        continue
                    if resolved is None:
                        # Image referenced but not in ZIP
                        key = src  # group by the src string as written
                        orphans.setdefault(key, []).append((html_file, src))

            title = get_title(zin, opf_path)

            if not orphans:
                return "clean", [], None

            details = []
            for src, locations in orphans.items():
                for html_file, _ in locations:
                    details.append(f"missing image '{src}' referenced in {html_file}")

            if not fix:
                return "orphan", details, title

            # Fix mode: rewrite the EPUB
            if backup:
                shutil.copy2(src, src.with_suffix(".epub.bak"))

            # Build modified file contents
            planned: dict[str, bytes] = {}

            for html_file in html_files:
                raw = zin.read(html_file)
                try:
                    text = raw.decode("utf-8")
                    encoding = "utf-8"
                except UnicodeDecodeError:
                    text = raw.decode("latin-1")
                    encoding = "latin-1"

                modified = text
                for src, locations in orphans.items():
                    for loc_file, loc_src in locations:
                        if loc_file == html_file:
                            modified, desc = remove_img_tag(modified, loc_src)
                            details.append(f"{html_file}: {desc}")

                if modified != text:
                    # Clean up leftover blank lines
                    modified = re.sub(r'\n{3,}', '\n\n', modified)
                    planned[html_file] = modified.encode(encoding)

            # Also clean OPF manifest
            if opf_path:
                raw = zin.read(opf_path)
                try:
                    opf_text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    opf_text = raw.decode("latin-1")

                opf_modified = opf_text
                for src in orphans:
                    basename = src.rsplit("/", 1)[-1]
                    opf_modified, n = remove_opf_item(opf_modified, basename)
                    if n:
                        details.append(f"OPF: removed {n} manifest item(s) for '{basename}'")

                if opf_modified != opf_text:
                    planned[opf_path] = opf_modified.encode("utf-8")

            # Write new EPUB
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=".epub", dir=dst.parent)
            try:
                with os.fdopen(tmp_fd, "wb") as tmp_f:
                    with zipfile.ZipFile(zin.filename, "r") as z2, \
                         zipfile.ZipFile(tmp_f, "w", compression=zipfile.ZIP_DEFLATED) as zout:
                        if "mimetype" in names:
                            zout.writestr(
                                z2.getinfo("mimetype"),
                                z2.read("mimetype"),
                                compress_type=zipfile.ZIP_STORED,
                            )
                        for item in z2.infolist():
                            if item.filename == "mimetype":
                                continue
                            if item.filename in planned:
                                zout.writestr(item, planned[item.filename])
                            else:
                                zout.writestr(item, z2.read(item.filename))
                shutil.move(tmp_path, dst)
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise

            return "orphan", details, title

    except zipfile.BadZipFile:
        return "error", ["not a valid ZIP/EPUB"], None
    except Exception as exc:
        return "error", [str(exc)], None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Find (and optionally fix) orphaned <img> references in EPUB HTML files."
    )
    parser.add_argument("folder",   help="Folder to scan (searched recursively)")
    parser.add_argument("--fix",    action="store_true", help="Remove orphaned tags and OPF entries")
    parser.add_argument("--backup", action="store_true", help="Save .epub.bak before modifying (with --fix)")
    parser.add_argument("--output", default=None,        help="Write fixed EPUBs here instead of overwriting")
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
    if not args.fix:
        print("Report-only mode — run with --fix to repair orphaned references.\n")
    print()

    counts = {"orphan": 0, "clean": 0, "error": 0}
    orphan_titles = []

    for i, epub_path in enumerate(epubs, 1):
        rel = epub_path.relative_to(src_folder)
        dst = (out_folder / rel) if out_folder else epub_path
        if out_folder and args.fix:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(epub_path, dst)

        status, details, title = check_epub(
            src=epub_path,
            fix=args.fix,
            dst=dst,
            backup=args.backup,
        )
        counts[status] += 1

        if status == "clean":
            print(f"[{i:>4}/{total}] {rel}  →  clean")
        elif status == "orphan":
            tag = "FIXED" if args.fix else "ORPHAN FOUND"
            print(f"[{i:>4}/{total}] {rel}  →  {tag}")
            for d in details:
                print(f"           • {d}")
            if title:
                orphan_titles.append(title)
            else:
                orphan_titles.append(epub_path.stem)  # fallback to filename
        else:
            print(f"[{i:>4}/{total}] {rel}  →  ERROR")
            for d in details:
                print(f"           • {d}")

    print()
    print("─" * 60)
    action = "Fixed" if args.fix else "Orphans found"
    print(f"Done.  {action}: {counts['orphan']}  Clean: {counts['clean']}  Errors: {counts['error']}")

    if orphan_titles:
        print()
        print("Calibre search query (paste into the search bar):")
        print()
        query = " or ".join(f'title:"{t}"' for t in orphan_titles)
        print(query)


if __name__ == "__main__":
    main()
