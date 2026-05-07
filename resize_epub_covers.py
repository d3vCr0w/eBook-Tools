#!/usr/bin/env python3
"""
resize_epub_covers.py
---------------------
Batch-resize cover images in EPUB files to a target resolution.

Usage:
    python resize_epub_covers.py /path/to/epub/folder [options]

Options:
    --width   W   Target width  (default: 1264)
    --height  H   Target height (default: 1680)
    --output  DIR Write modified EPUBs here instead of overwriting originals
    --backup      Keep a .bak copy of each original alongside the output
    --dry-run     Show what would be done without changing any files
    --quality Q   JPEG quality 1-95 (default: 90)
    --no-upscale  Skip covers that are already larger than the target
"""

import argparse
import io
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

# Pillow is required: pip install Pillow
try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow is required. Install it with:  pip install Pillow")


# ── Cover detection ──────────────────────────────────────────────────────────

def find_cover_path(epub_zip: zipfile.ZipFile) -> str | None:
    """Return the in-ZIP path of the cover image, or None if not found."""

    # Strategy 1: look for cover-image in the OPF manifest
    opf_path = _find_opf(epub_zip)
    if opf_path:
        cover = _cover_from_opf(epub_zip, opf_path)
        if cover:
            return cover

    # Strategy 2: heuristic – any file whose name contains "cover"
    for name in epub_zip.namelist():
        lower = name.lower()
        if "cover" in lower and lower.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
            return name

    return None


def _find_opf(epub_zip: zipfile.ZipFile) -> str | None:
    """Locate the OPF file via META-INF/container.xml."""
    try:
        data = epub_zip.read("META-INF/container.xml").decode("utf-8", errors="replace")
        import re
        m = re.search(r'full-path=["\']([^"\']+\.opf)["\']', data, re.IGNORECASE)
        if m:
            return m.group(1)
    except KeyError:
        pass

    # Fallback: search directly
    for name in epub_zip.namelist():
        if name.endswith(".opf"):
            return name
    return None


def _cover_from_opf(epub_zip: zipfile.ZipFile, opf_path: str) -> str | None:
    """Parse the OPF file and extract the cover image path."""
    import re

    try:
        opf = epub_zip.read(opf_path).decode("utf-8", errors="replace")
    except KeyError:
        return None

    opf_dir = opf_path.rsplit("/", 1)[0] if "/" in opf_path else ""

    # Find the cover item id from <meta name="cover" content="..."/>
    meta_match = re.search(
        r'<meta[^>]+name=["\']cover["\'][^>]+content=["\']([^"\']+)["\']',
        opf, re.IGNORECASE
    )
    if not meta_match:
        # EPUB3 style: properties="cover-image"
        meta_match = re.search(
            r'<item[^>]+properties=["\'][^"\']*cover-image[^"\']*["\'][^>]+href=["\']([^"\']+)["\']',
            opf, re.IGNORECASE
        )
        if meta_match:
            href = meta_match.group(1)
            full = (opf_dir + "/" + href).lstrip("/") if opf_dir else href
            return full if full in epub_zip.namelist() else None

    if not meta_match:
        return None

    cover_id = meta_match.group(1)

    # Find the href for that id in the manifest
    item_match = re.search(
        rf'<item[^>]+id=["\']{re.escape(cover_id)}["\'][^>]+href=["\']([^"\']+)["\']',
        opf, re.IGNORECASE
    )
    if not item_match:
        # try reversed attribute order
        item_match = re.search(
            rf'<item[^>]+href=["\']([^"\']+)["\'][^>]+id=["\']{re.escape(cover_id)}["\']',
            opf, re.IGNORECASE
        )
    if not item_match:
        return None

    href = item_match.group(1)
    full = (opf_dir + "/" + href).lstrip("/") if opf_dir else href
    return full if full in epub_zip.namelist() else None


# ── Image resizing ───────────────────────────────────────────────────────────

def resize_image_bytes(
    data: bytes,
    target_w: int,
    target_h: int,
    quality: int,
    no_upscale: bool,
) -> tuple[bytes, tuple[int, int], tuple[int, int]] | None:
    """
    Resize image bytes to target_w × target_h (exact, no aspect-ratio lock).
    Returns (new_bytes, old_size, new_size) or None if skipped.
    """
    img = Image.open(io.BytesIO(data))
    orig_size = img.size

    if orig_size == (target_w, target_h):
        return None  # already correct

    if no_upscale and img.width >= target_w and img.height >= target_h:
        return None  # caller asked to skip upscaling

    img = img.convert("RGB")
    img = img.resize((target_w, target_h), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue(), orig_size, (target_w, target_h)


# ── EPUB rewriting ───────────────────────────────────────────────────────────

def process_epub(
    src: Path,
    dst: Path,
    target_w: int,
    target_h: int,
    quality: int,
    no_upscale: bool,
    dry_run: bool,
) -> str:
    """
    Process a single EPUB file.
    Returns a short status string for the summary line.
    """
    try:
        with zipfile.ZipFile(src, "r") as z:
            cover_path = find_cover_path(z)

            if cover_path is None:
                return "SKIP (no cover found)"

            cover_data = z.read(cover_path)

            if dry_run:
                try:
                    img = Image.open(io.BytesIO(cover_data))
                    return f"DRY-RUN  cover={cover_path}  size={img.size}"
                except Exception:
                    return f"DRY-RUN  cover={cover_path}  (image unreadable)"

            result = resize_image_bytes(cover_data, target_w, target_h, quality, no_upscale)
            if result is None:
                return "SKIP (already correct size or no-upscale)"

            new_bytes, old_size, new_size = result

            # Rewrite the EPUB in a temp file, then replace dst
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=".epub", dir=dst.parent)
            try:
                with os.fdopen(tmp_fd, "wb") as tmp_f:
                    with zipfile.ZipFile(z.filename, "r") as zin, \
                         zipfile.ZipFile(tmp_f, "w", compression=zipfile.ZIP_DEFLATED) as zout:

                        # mimetype must be first and uncompressed per EPUB spec
                        if "mimetype" in zin.namelist():
                            zout.writestr(
                                zin.getinfo("mimetype"),
                                zin.read("mimetype"),
                                compress_type=zipfile.ZIP_STORED,
                            )

                        for item in zin.infolist():
                            if item.filename == "mimetype":
                                continue
                            if item.filename == cover_path:
                                zout.writestr(item, new_bytes)
                            else:
                                zout.writestr(item, zin.read(item.filename))

                shutil.move(tmp_path, dst)
            except Exception:
                os.unlink(tmp_path)
                raise

            return f"OK  {old_size} → {new_size}  cover={cover_path}"

    except zipfile.BadZipFile:
        return "ERROR (not a valid ZIP/EPUB)"
    except Exception as exc:
        return f"ERROR ({exc})"


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Batch-resize cover images in EPUB files."
    )
    parser.add_argument("folder", help="Folder containing EPUB files (searched recursively)")
    parser.add_argument("--width",    type=int, default=1264, help="Target width  (default: 1264)")
    parser.add_argument("--height",   type=int, default=1680, help="Target height (default: 1680)")
    parser.add_argument("--output",   default=None, help="Output folder (default: overwrite originals)")
    parser.add_argument("--backup",   action="store_true", help="Save .bak copy of each original")
    parser.add_argument("--dry-run",  action="store_true", help="Show actions without changing files")
    parser.add_argument("--quality",  type=int, default=90, help="JPEG quality 1-95 (default: 90)")
    parser.add_argument("--no-upscale", action="store_true",
                        help="Skip covers that are already larger than the target")
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
    print(f"Target resolution: {args.width}×{args.height}  JPEG quality: {args.quality}")
    if args.dry_run:
        print("DRY-RUN mode – no files will be modified.\n")
    print()

    counts = {"ok": 0, "skip": 0, "error": 0, "dryrun": 0}

    for i, epub_path in enumerate(epubs, 1):
        rel = epub_path.relative_to(src_folder)

        if out_folder:
            dst = out_folder / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not args.dry_run:
                shutil.copy2(epub_path, dst)  # work on the copy
        else:
            dst = epub_path

        if args.backup and not args.dry_run:
            shutil.copy2(epub_path, epub_path.with_suffix(".epub.bak"))

        status = process_epub(
            src=epub_path,
            dst=dst,
            target_w=args.width,
            target_h=args.height,
            quality=args.quality,
            no_upscale=args.no_upscale,
            dry_run=args.dry_run,
        )

        label = status.split()[0]
        if label == "OK":
            counts["ok"] += 1
        elif label == "DRY-RUN":
            counts["dryrun"] += 1
        elif label == "SKIP":
            counts["skip"] += 1
        else:
            counts["error"] += 1

        print(f"[{i:>4}/{total}] {rel}  →  {status}")

    print()
    print("─" * 60)
    if args.dry_run:
        print(f"Dry-run complete. Would process: {counts['dryrun']}  skip: {counts['skip']}  error: {counts['error']}")
    else:
        print(f"Done.  Updated: {counts['ok']}  Skipped: {counts['skip']}  Errors: {counts['error']}")


if __name__ == "__main__":
    main()