#!/usr/bin/env bash
#
# Print the version the current commit would publish as.
#
# The version identity lives in two places only:
#   .version   - major.minor, hand edited, the one thing a human decides
#   git        - the patch number, derived from the commit count
#
# The patch is the commit count rather than a hand maintained number, so it never needs a
# commit of its own and cannot conflict on a merge. It keeps counting across a minor bump,
# so 0.5 follows 0.4 without the patch resetting and every version sorts after the last.
#
# Usage: scripts/version.bash [--dev]
#
#   --dev   Print the develop-channel version instead: "0.4.50.dev0".
#
# PEP 440 sorts a dev release below its own final release and above the previous one
# (0.4.45 < 0.4.50.dev0 < 0.4.50), and pip hides it unless asked with --pre. The dev
# number is a constant 0 because the commit count already makes each build unique.
#
# Prints a bare "0.4.45". scripts/release_tag.bash adds the "v" for the tag name.

set -Eeuo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_PATH="$(cd "${THIS_DIR}/.." && pwd)"

VERSION_FILE="${BASE_PATH}/.version"

DEV=false
for argument in "$@"; do
    case "$argument" in
        --dev)
            DEV=true
            ;;
        *)
            echo "error: unknown option ${argument}" >&2
            exit 2
            ;;
    esac
done

if [ ! -f "$VERSION_FILE" ]; then
    echo "error: ${VERSION_FILE} not found" >&2
    exit 1
fi

MAJOR_MINOR="$(tr -d '[:space:]' < "$VERSION_FILE")"
if [ -z "$MAJOR_MINOR" ]; then
    echo "error: ${VERSION_FILE} is empty" >&2
    exit 1
fi

# PyPI parses this as PEP 440, and a typo here is a version that either fails to upload or
# uploads as something unexpected - and an upload can never be replaced
if [[ ! "$MAJOR_MINOR" =~ ^[0-9]+\.[0-9]+$ ]]; then
    echo "error: ${VERSION_FILE} must be major.minor, e.g. 0.4 - found \"${MAJOR_MINOR}\"" >&2
    exit 1
fi

# A shallow clone has no history to count, and would silently produce version .1
if [ "$(git -C "$BASE_PATH" rev-parse --is-shallow-repository 2>/dev/null || echo false)" = "true" ]; then
    echo "error: shallow clone - fetch full history (actions/checkout needs fetch-depth: 0)" >&2
    exit 1
fi

COMMIT_COUNT="$(git -C "$BASE_PATH" rev-list --count HEAD)"

if [ "$DEV" = true ]; then
    printf '%s.%s.dev0\n' "$MAJOR_MINOR" "$COMMIT_COUNT"
else
    printf '%s.%s\n' "$MAJOR_MINOR" "$COMMIT_COUNT"
fi
