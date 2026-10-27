#!/usr/bin/env bash
#
# Print the version the current commit would publish as.
#
# The version identity lives in two places only:
#   .version   - major.minor, hand edited, the one thing a human decides
#   git tags   - the patch, counted per minor from the v<major.minor>.<patch> release tags
#
# The patch is the next release of the current minor: 0.7.0 is the first, then 0.7.1, and
# a minor bump starts again at 0. It is read off the tags rather than stored, so it never
# needs a commit of its own and cannot conflict on a merge, and rewriting history cannot
# hand back a number that was already published.
#
# Usage: scripts/version.bash [--dev]
#
#   (none)  A commit carrying a release tag of this minor prints that tag's version, so the
#           publish workflow derives what was tagged. Any other commit prints the next one.
#   --dev   The develop-channel version: "0.7.3.dev5", the next patch plus the number of
#           commits since the newest release tag, which makes each develop push unique.
#
# PEP 440 sorts a dev release below its own final release and above the previous one
# (0.7.2 < 0.7.3.dev5 < 0.7.3), and pip hides it unless asked with --pre.
#
# Prints a bare "0.7.3". scripts/release_tag.bash adds the "v" for the tag name.

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
    echo "error: ${VERSION_FILE} must be major.minor, e.g. 0.7 - found \"${MAJOR_MINOR}\"" >&2
    exit 1
fi

# A shallow clone has neither the tags nor the history to count
if [ "$(git -C "$BASE_PATH" rev-parse --is-shallow-repository 2>/dev/null || echo false)" = "true" ]; then
    echo "error: shallow clone - fetch full history (actions/checkout needs fetch-depth: 0)" >&2
    exit 1
fi

MINOR_TAGS="$(git -C "$BASE_PATH" tag -l "v${MAJOR_MINOR}.*" | grep -E "^v${MAJOR_MINOR//./\\.}\.[0-9]+$" | sort -V || true)"

if [ "$DEV" = false ]; then
    HEAD_TAG="$(git -C "$BASE_PATH" tag --points-at HEAD | grep -Fx -f <(printf '%s\n' "$MINOR_TAGS") | sort -V | tail -1 || true)"
    if [ -n "$HEAD_TAG" ]; then
        printf '%s\n' "${HEAD_TAG#v}"
        exit 0
    fi
fi

LATEST_MINOR_TAG="$(printf '%s\n' "$MINOR_TAGS" | tail -1)"
if [ -n "$LATEST_MINOR_TAG" ]; then
    NEXT_PATCH=$((${LATEST_MINOR_TAG##*.} + 1))
else
    NEXT_PATCH=0
fi

if [ "$DEV" = false ]; then
    printf '%s.%s\n' "$MAJOR_MINOR" "$NEXT_PATCH"
    exit 0
fi

# Counted from the newest release of any minor: a freshly bumped minor has no tag of its own
LATEST_TAG="$(git -C "$BASE_PATH" tag -l 'v*' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1 || true)"
if [ -n "$LATEST_TAG" ]; then
    DEV_NUMBER="$(git -C "$BASE_PATH" rev-list --count "${LATEST_TAG}..HEAD")"
else
    DEV_NUMBER="$(git -C "$BASE_PATH" rev-list --count HEAD)"
fi

printf '%s.%s.dev%s\n' "$MAJOR_MINOR" "$NEXT_PATCH" "$DEV_NUMBER"
