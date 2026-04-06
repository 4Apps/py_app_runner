#!/bin/bash

# Bump version in .current_version file
# Usage: bump_version.bash [major|minor|patch]
#
# Reads version from .current_version (format: X.Y.Z)
# Increments the specified part and stages the change

BASE_PATH="$(git rev-parse --show-toplevel)"
VERSION_FILE="$BASE_PATH/.current_version"
BUMP_TYPE="${1:-patch}"

if [ ! -f "$VERSION_FILE" ]; then
    echo "Error: .current_version not found at $VERSION_FILE"
    exit 1
fi

CURRENT_VERSION=$(cat "$VERSION_FILE" | tr -d '[:space:]')

if [ -z "$CURRENT_VERSION" ]; then
    echo "Error: .current_version is empty"
    exit 1
fi

# Split version
MAJOR=$(echo "$CURRENT_VERSION" | cut -d'.' -f1)
MINOR=$(echo "$CURRENT_VERSION" | cut -d'.' -f2)
PATCH=$(echo "$CURRENT_VERSION" | cut -d'.' -f3)

case "$BUMP_TYPE" in
    major)
        MAJOR=$((MAJOR + 1))
        MINOR=0
        PATCH=0
        ;;
    minor)
        MINOR=$((MINOR + 1))
        PATCH=0
        ;;
    patch)
        PATCH=$((PATCH + 1))
        ;;
    *)
        echo "Error: Unknown bump type '$BUMP_TYPE'. Use: major, minor, patch"
        exit 1
        ;;
esac

NEW_VERSION="${MAJOR}.${MINOR}.${PATCH}"

# Update .current_version
echo "$NEW_VERSION" > "$VERSION_FILE"

# Update version in tracked files
FILES_TO_UPDATE=(
    "pyproject.toml"
    "src/py_app_runner/__init__.py"
)
for FILE in "${FILES_TO_UPDATE[@]}"; do
    FILEPATH="$BASE_PATH/$FILE"
    if [ -f "$FILEPATH" ]; then
        sed -i "s/$CURRENT_VERSION/$NEW_VERSION/g" "$FILEPATH"
        git add "$FILEPATH"
    fi
done

echo "  Version bumped: $CURRENT_VERSION -> $NEW_VERSION"

# Stage the changed file
git add "$VERSION_FILE"
