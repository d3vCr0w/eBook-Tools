#!/usr/bin/env python3
"""
remove_cover_watermark.py
--------------------------
Finds the Lectulandia blue banner watermark on EPUB cover images and removes
the white "Lectulandia" text from it, leaving the blue shape intact.

The script works by:
  1. Locating the cover image inside each EPUB
  2. Detecting the blue banner in the bottom-right area by color
  3. Fitting a 2D polynomial to the known clean blue pixels to model the gradient
  4. Replacing text pixels (non-blue + antialiasing shadows) with the predicted blue

Requires: pip install Pillow numpy scipy

Usage:
    python remove_cover_watermark.py /path/to/epub/folder [options]

Options:
    --output  DIR   Write modified EPUBs here instead of overwriting originals
    --backup        Save a .epub.bak copy of each original before modifying
    --dry-run       Detect banners and report without changing any files
    --quality N     JPEG quality for saved cover (default: 92)
"""

import argparse
import io
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

try:
    import numpy as np
    from PIL import Image
    from scipy.ndimage import binary_dilation
except ImportError:
    sys.exit(
        "Missing dependencies. Install with:\n"
        "  pip install Pillow numpy scipy"
    )


# ── Cover locator (reused from resize script) ─────────────────────────────────

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


def find_cover_path(epub_zip: zipfile.ZipFile) -> str | None:
    opf_path = find_opf(epub_zip)
    if opf_path:
        cover = _cover_from_opf(epub_zip, opf_path)
        if cover:
            return cover
    for name in epub_zip.namelist():
        lower = name.lower()
        if "cover" in lower and lower.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
            return name
    return None


def _cover_from_opf(epub_zip: zipfile.ZipFile, opf_path: str) -> str | None:
    try:
        opf = epub_zip.read(opf_path).decode("utf-8", errors="replace")
    except KeyError:
        return None

    opf_dir = opf_path.rsplit("/", 1)[0] if "/" in opf_path else ""

    meta_match = re.search(
        r'<meta[^>]+name=["\']cover["\'][^>]+content=["\']([^"\']+)["\']',
        opf, re.IGNORECASE
    )
    if not meta_match:
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
    item_match = re.search(
        rf'<item[^>]+id=["\']{re.escape(cover_id)}["\'][^>]+href=["\']([^"\']+)["\']',
        opf, re.IGNORECASE
    )
    if not item_match:
        item_match = re.search(
            rf'<item[^>]+href=["\']([^"\']+)["\'][^>]+id=["\']{re.escape(cover_id)}["\']',
            opf, re.IGNORECASE
        )
    if not item_match:
        return None

    href = item_match.group(1)
    full = (opf_dir + "/" + href).lstrip("/") if opf_dir else href
    return full if full in epub_zip.namelist() else None


# ── Watermark removal ─────────────────────────────────────────────────────────

def detect_banner(arr: np.ndarray) -> tuple[np.ndarray, tuple] | None:
    """
    Detect the Lectulandia blue banner in the image array.
    Returns (banner_blue_mask, (y1, y2, x1, x2)) or None if not found.
    """
    h, w = arr.shape[:2]
    r, g, b = arr[:,:,0], arr[:,:,1], arr[:,:,2]

    # The banner is a specific teal-blue: B > G > R, B > 140, G > 90, R < 130
    blue_mask = (b > g) & (g > r) & (b > 140) & (g > 90) & (r < 130)

    # Restrict to bottom-right region (banner is always there)
    region = np.zeros((h, w), dtype=bool)
    region[int(h * 0.75):, int(w * 0.50):] = True
    banner_blue = blue_mask & region

    if banner_blue.sum() < 500:  # too few pixels — no banner
        return None

    rows = np.where(banner_blue.any(axis=1))[0]
    cols = np.where(banner_blue.any(axis=0))[0]
    y1 = max(0, rows.min())
    y2 = min(h, rows.max() + 1)
    x1 = max(0, cols.min())
    x2 = min(w, cols.max() + 1)

    return banner_blue, (y1, y2, x1, x2)


def remove_watermark_text(image_data: bytes, quality: int) -> bytes | None:
    """
    Remove Lectulandia text from a cover image.
    Returns new image bytes, or None if no banner detected.
    """
    img = Image.open(io.BytesIO(image_data)).convert("RGB")
    arr = np.array(img, dtype=np.float32)
    h, w = arr.shape[:2]

    result = detect_banner(arr)
    if result is None:
        return None

    banner_blue, (y1, y2, x1, x2) = result

    sub = arr[y1:y2, x1:x2]
    sub_blue = banner_blue[y1:y2, x1:x2]
    sub_r, sub_g, sub_b = sub[:,:,0], sub[:,:,1], sub[:,:,2]

    sh, sw = sub.shape[:2]

    # Not-red guard: don't touch pixels that are clearly the cover background
    not_red = ~((sub_r > 130) & (sub_r > sub_b * 1.8) & (sub_r > sub_g * 1.5))

    # Text mask: non-blue pixels inside banner box, dilated to catch antialiasing
    text_mask_base = ~sub_blue
    text_mask = binary_dilation(text_mask_base, iterations=3) & not_red

    # Polynomial gradient model fitted to clean blue pixels
    # (erode text mask to ensure we only use pixels well away from text)
    clean_blue = sub_blue & ~binary_dilation(text_mask_base, iterations=6)
    if clean_blue.sum() < 50:
        clean_blue = sub_blue  # fallback if banner is very small

    predicted_channels = []
    for ch in range(3):
        channel = sub[:,:,ch]
        ky, kx = np.where(clean_blue)
        kvals = channel[clean_blue]
        yn, xn = ky / sh, kx / sw

        # Degree-3 polynomial basis
        A = np.column_stack([
            np.ones(len(kx)),
            xn, yn, xn**2, xn*yn, yn**2,
            xn**3, yn**3,
        ])
        coeffs, _, _, _ = np.linalg.lstsq(A, kvals, rcond=None)

        all_y, all_x = np.mgrid[0:sh, 0:sw]
        ayn, axn = all_y / sh, all_x / sw
        A_full = np.column_stack([
            np.ones(sh * sw),
            axn.ravel(), ayn.ravel(),
            axn.ravel()**2, (axn * ayn).ravel(), ayn.ravel()**2,
            axn.ravel()**3, ayn.ravel()**3,
        ])
        predicted = (A_full @ coeffs).reshape(sh, sw)
        predicted_channels.append(predicted)

    # Shadow pass: pixels darker than predicted by >10 brightness units
    # within the blue area are the text's dark stroke/antialiasing
    pred_brightness = sum(predicted_channels) / 3
    actual_brightness = (sub_r + sub_g + sub_b) / 3
    shadow_mask = (pred_brightness - actual_brightness > 10) & sub_blue & not_red
    shadow_mask = binary_dilation(shadow_mask, iterations=2) & not_red

    text_mask_final = text_mask | shadow_mask

    # Apply: replace text pixels with predicted gradient color
    out_arr = arr.copy()
    for ch in range(3):
        channel = sub[:,:,ch].copy()
        channel[text_mask_final] = np.clip(predicted_channels[ch][text_mask_final], 0, 255)
        out_arr[y1:y2, x1:x2, ch] = channel

    out_img = Image.fromarray(out_arr.astype(np.uint8))
    buf = io.BytesIO()
    out_img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


# ── EPUB processor ────────────────────────────────────────────────────────────

def process_epub(
    src: Path,
    dst: Path,
    quality: int,
    dry_run: bool,
) -> str:
    try:
        with zipfile.ZipFile(src, "r") as zin:
            cover_path = find_cover_path(zin)
            if cover_path is None:
                return "SKIP (no cover found)"

            cover_data = zin.read(cover_path)

            # Quick banner detection for dry-run
            try:
                img = Image.open(io.BytesIO(cover_data)).convert("RGB")
                arr = np.array(img, dtype=np.float32)
            except Exception:
                return "SKIP (cover unreadable)"

            banner_result = detect_banner(arr)
            if banner_result is None:
                return "SKIP (no Lectulandia banner detected)"

            if dry_run:
                _, (y1, y2, x1, x2) = banner_result
                return f"DRY-RUN  banner detected at y={y1}-{y2} x={x1}-{x2}  cover={cover_path}"

            # Process the image
            new_cover = remove_watermark_text(cover_data, quality)
            if new_cover is None:
                return "SKIP (banner vanished on reprocess?)"

            # Rewrite EPUB with updated cover
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=".epub", dir=dst.parent)
            try:
                with os.fdopen(tmp_fd, "wb") as tmp_f:
                    with zipfile.ZipFile(zin.filename, "r") as z2, \
                         zipfile.ZipFile(tmp_f, "w", compression=zipfile.ZIP_DEFLATED) as zout:
                        if "mimetype" in z2.namelist():
                            zout.writestr(
                                z2.getinfo("mimetype"),
                                z2.read("mimetype"),
                                compress_type=zipfile.ZIP_STORED,
                            )
                        for item in z2.infolist():
                            if item.filename == "mimetype":
                                continue
                            if item.filename == cover_path:
                                zout.writestr(item, new_cover)
                            else:
                                zout.writestr(item, z2.read(item.filename))
                shutil.move(tmp_path, dst)
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise

            return f"OK  cover={cover_path}"

    except zipfile.BadZipFile:
        return "ERROR (not a valid ZIP/EPUB)"
    except Exception as exc:
        return f"ERROR ({exc})"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Remove Lectulandia text from cover image watermarks in EPUB files."
    )
    parser.add_argument("folder", help="Folder containing EPUB files (searched recursively)")
    parser.add_argument("--output",  default=None, help="Output folder (default: overwrite originals)")
    parser.add_argument("--backup",  action="store_true", help="Save .epub.bak copy of each original")
    parser.add_argument("--dry-run", action="store_true", help="Detect banners without modifying files")
    parser.add_argument("--quality", type=int, default=95, help="JPEG quality 1-95 (default: 95)")
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

    counts = {"ok": 0, "skip": 0, "error": 0, "dryrun": 0}

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

        status = process_epub(
            src=epub_path,
            dst=dst,
            quality=args.quality,
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
        print(f"Dry-run complete.  Would process: {counts['dryrun']}  Skip: {counts['skip']}  Errors: {counts['error']}")
    else:
        print(f"Done.  Cleaned: {counts['ok']}  Skipped: {counts['skip']}  Errors: {counts['error']}")


if __name__ == "__main__":
    main()
