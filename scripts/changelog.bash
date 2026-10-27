#!/usr/bin/env bash
#
# CHANGELOG.md helpers for a release.
#
# Usage:
#   scripts/changelog.bash prepare     Rename "## Unreleased" to the version scripts/version.bash
#                                      derives, dated today, and open a fresh "## Unreleased" above
#                                      it. Edits the file only; commit it like any other change.
#   scripts/changelog.bash notes TAG   Print that version's entries, for the GitHub release body.

set -Eeuo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_PATH="$(cd "${THIS_DIR}/.." && pwd)"

CHANGELOG="${BASE_PATH}/CHANGELOG.md"

# Lines between "## <heading>" and the next "## ", blank lines trimmed
section() {
    awk -v heading="## $1" '
        index($0, "## ") == 1 {
            if (found) { exit }
            if ($0 == heading || index($0, heading " ") == 1) { found = 1; next }
        }
        found { lines[++n] = $0; if ($0 ~ /[^[:space:]]/) { if (!first) { first = n } last = n } }
        END { for (i = first; first && i <= last; i++) { print lines[i] } }
    ' "$CHANGELOG"
}

case "${1:-}" in
    prepare)
        VERSION="$("${THIS_DIR}/version.bash")"
        TAG="v${VERSION}"

        if grep -q "^## ${TAG}\( \|$\)" "$CHANGELOG"; then
            echo "error: CHANGELOG.md already has a ${TAG} section." >&2
            exit 1
        fi

        if [ -z "$(section Unreleased)" ]; then
            echo "error: \"## Unreleased\" in CHANGELOG.md is missing or empty - nothing to release." >&2
            exit 1
        fi

        sed -i "0,/^## Unreleased\$/s//## Unreleased\n\n## ${TAG} - $(date +%Y-%m-%d)/" "$CHANGELOG"
        echo "CHANGELOG.md: Unreleased is now ${TAG}. Commit it, then run scripts/release_tag.bash."
        ;;
    notes)
        if [ -z "${2:-}" ]; then
            echo "error: usage: scripts/changelog.bash notes TAG" >&2
            exit 2
        fi

        NOTES="$(section "$2")"
        if [ -z "$NOTES" ]; then
            echo "error: CHANGELOG.md has no entries under \"## $2\"." >&2
            exit 1
        fi

        printf '%s\n' "$NOTES"
        ;;
    *)
        echo "error: usage: scripts/changelog.bash prepare | notes TAG" >&2
        exit 2
        ;;
esac
