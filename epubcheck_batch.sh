#!/usr/bin/env bash
# check_epubs.sh — Run epubcheck against all EPUBs in a Calibre library
# Usage: ./check_epubs.sh [/path/to/calibre/library]
# If no path is given, the current directory is used.

set -uo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
LIBRARY_DIR="${1:-$PWD}"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
LOG_FILE="epubcheck_report_${TIMESTAMP}.log"
SUMMARY_FILE="epubcheck_summary_${TIMESTAMP}.txt"

# Colors (disabled automatically if stdout is not a terminal)
if [[ -t 1 ]]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
    CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; CYAN=''; BOLD=''; RESET=''
fi

# ── Preflight checks ──────────────────────────────────────────────────────────
if ! command -v epubcheck &>/dev/null; then
    echo "Error: epubcheck not found in PATH." >&2
    exit 1
fi

if [[ ! -d "$LIBRARY_DIR" ]]; then
    echo "Error: Library directory not found: $LIBRARY_DIR" >&2
    exit 1
fi

# ── Helpers ───────────────────────────────────────────────────────────────────
log() { echo "$*" | tee -a "$LOG_FILE"; }

# ── Discover EPUBs ────────────────────────────────────────────────────────────
while IFS= read -r -d '' f; do EPUB_FILES+=("$f"); done < <(find "$LIBRARY_DIR" -type f -iname "*.epub" -print0 | sort -z)
TOTAL=${#EPUB_FILES[@]}

if [[ $TOTAL -eq 0 ]]; then
    echo "No EPUB files found in: $LIBRARY_DIR"
    exit 0
fi

# ── Header ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}epubcheck batch validator${RESET}"
echo -e "Library : ${CYAN}${LIBRARY_DIR}${RESET}"
echo -e "EPUBs   : ${BOLD}${TOTAL}${RESET} files found"
echo -e "Log     : ${LOG_FILE}"
echo ""

log "epubcheck batch report — $(date)"
log "Library : $LIBRARY_DIR"
log "EPUBs   : $TOTAL"
log "$(printf '=%.0s' {1..72})"

# ── Main loop ─────────────────────────────────────────────────────────────────
PASS=0; WARN=0; FAIL=0; idx=0

for epub in "${EPUB_FILES[@]}"; do
    idx=$(( idx + 1 ))
    # Path relative to library root for cleaner output
    rel="${epub#"$LIBRARY_DIR"/}"

    printf "[%*d/%d] %s ... " "${#TOTAL}" "$idx" "$TOTAL" "$rel"
    log ""
    log "FILE ($idx/$TOTAL): $rel"
    log "$(printf -- '-%.0s' {1..72})"

    # Capture both stdout and stderr; epubcheck writes everything to stderr
    output=$(epubcheck "$epub" 2>&1)
    exit_code=$?

    # Append full output to the log
    echo "$output" >> "$LOG_FILE"

    # Parse result
    errors=$(echo   "$output" | grep -c "^ERROR"   || true)
    warnings=$(echo "$output" | grep -c "^WARNING" || true)

    if [[ $exit_code -eq 0 && $errors -eq 0 ]]; then
        echo -e "${GREEN}PASS${RESET}"
        log "RESULT: PASS"
        PASS=$(( PASS + 1 ))
    elif [[ $errors -gt 0 ]]; then
        echo -e "${RED}FAIL${RESET} (${errors} error(s), ${warnings} warning(s))"
        log "RESULT: FAIL  errors=$errors  warnings=$warnings"
        FAIL=$(( FAIL + 1 ))
    else
        echo -e "${YELLOW}WARN${RESET} (${warnings} warning(s))"
        log "RESULT: WARN  warnings=$warnings"
        WARN=$(( WARN + 1 ))
    fi
done

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}── Summary ───────────────────────────────────────────────────────────${RESET}"
echo -e "  Total  : ${BOLD}$TOTAL${RESET}"
echo -e "  ${GREEN}Pass   : $PASS${RESET}"
echo -e "  ${YELLOW}Warn   : $WARN${RESET}"
echo -e "  ${RED}Fail   : $FAIL${RESET}"
echo ""
echo -e "Full log : ${CYAN}${LOG_FILE}${RESET}"

# Write machine-readable summary
{
    echo "timestamp=$TIMESTAMP"
    echo "library=$LIBRARY_DIR"
    echo "total=$TOTAL"
    echo "pass=$PASS"
    echo "warn=$WARN"
    echo "fail=$FAIL"
} > "$SUMMARY_FILE"
echo -e "Summary  : ${CYAN}${SUMMARY_FILE}${RESET}"
echo ""

# Exit with a non-zero code if any files failed validation
[[ $FAIL -eq 0 ]]