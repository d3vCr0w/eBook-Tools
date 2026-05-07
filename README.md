# ePub Cover Resizer
Python script to resize ePub covers in bulk. 

EPUBs are ZIP files, so the script opens each one, finds the cover via the OPF manifest (the same metadata Calibre reads), resizes it with Pillow's high-quality Lanczos filter, and rewrites only that one file inside the ZIP — everything else is preserved untouched.

It searches recursively, so nested subfolders are handled automatically.
Cover detection uses the standard EPUB2 (meta name="cover") and EPUB3 (properties="cover-image") metadata, with a filename-heuristic fallback for books with non-standard metadata.

## Usage 

### Dependencies

Install the one dependency (if you don't have it):

`pip install Pillow`

### Overwrite originals in place:

`python resize_epub_covers.py /path/to/your/ebooks/`

### Write to a separate output folder:

`python resize_epub_covers.py /path/to/your/ebooks/ --output /path/to/resized/`

### Preview what it will do without touching anything:

`python resize_epub_covers.py /path/to/your/ebooks/ --dry-run`

### All options:

|Flag|Default|Description|
|----|-------|-----------|
|--width / --height | 1264 / 1680 | Target resolution|
|--output DIR | (overwrite) | Write results to a different folder|
|--backup | off |Save a .epub.bak alongside each original|
|--quality N | 90 | JPEG quality (1–95)|
|--no-upscale | off | Skip covers already larger than target|
|--no-upscale | off | Skip covers already larger than target|
|--dry-run |off |Preview only, no files changed|
