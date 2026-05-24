#!/bin/bash

# Folder where the files will be created
TARGET_DIR="./dummy"

# Total size to generate (15 GB)
TOTAL_GB=1

# Size per file (500 MB)
FILE_MB=20

# Calculate number of files
NUM_FILES=$((TOTAL_GB * 1024 / FILE_MB))

# Create target folder if it doesn't exist
mkdir -p "$TARGET_DIR"

echo "Generating $NUM_FILES files of ${FILE_MB}MB each..."
echo "Destination: $TARGET_DIR"

for ((i=1; i<=NUM_FILES; i++)); do
    FILE_NAME=$(printf "dummy20MB_%02d.bin" "$i")

    echo "Creating $FILE_NAME..."

    # Create a 500MB file filled with random data
    mkfile "${FILE_MB}m" "$TARGET_DIR/$FILE_NAME"

    # Alternative using dd if mkfile fails:
    # dd if=/dev/zero of="$TARGET_DIR/$FILE_NAME" bs=1m count=$FILE_MB
done

echo "Done. Generated approximately ${TOTAL_GB}GB of files."
