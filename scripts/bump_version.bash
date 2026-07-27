#!/bin/bash

# Bump the manual part of the version in .version file
# Usage: bump_version.bash [major|minor]
#
# Reads version from .version (format: X.Y)
# The patch part is not stored here - CI appends the git commit count at build time

BASE_PATH="$(git rev-parse --show-toplevel)"
VERSION_FILE="$BASE_PATH/.version"
BUMP_TYPE="${1:-minor}"

if [ ! -f "$VERSION_FILE" ]; then
    echo "Error: .version not found at $VERSION_FILE"
    exit 1
fi

CURRENT_VERSION=$(cat "$VERSION_FILE" | tr -d '[:space:]')

if [ -z "$CURRENT_VERSION" ]; then
    echo "Error: .version is empty"
    exit 1
fi

# Split version
MAJOR=$(echo "$CURRENT_VERSION" | cut -d'.' -f1)
MINOR=$(echo "$CURRENT_VERSION" | cut -d'.' -f2)

case "$BUMP_TYPE" in
    major)
        MAJOR=$((MAJOR + 1))
        MINOR=0
        ;;
    minor)
        MINOR=$((MINOR + 1))
        ;;
    *)
        echo "Error: Unknown bump type '$BUMP_TYPE'. Use: major, minor"
        exit 1
        ;;
esac

NEW_VERSION="${MAJOR}.${MINOR}"

echo "$NEW_VERSION" > "$VERSION_FILE"

echo "  Version bumped: $CURRENT_VERSION -> $NEW_VERSION"

# Stage the changed file
git add "$VERSION_FILE"
