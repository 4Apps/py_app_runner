#!/bin/bash

# To enable this hook:
#   cd .git/hooks/ && ln -s ../../scripts/git_pre_commit.bash ./pre-commit

PLATFORM=$(uname)
BASE_PATH="$(git rev-parse --show-toplevel)"
COMMIT="HEAD"
BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null || echo "detached")


# Test non-ascii filenames
echo
echo "*Testing non-ascii filenames.. "
if [ $(git diff --cached --name-only --diff-filter=A -z $COMMIT | LC_ALL=C tr -d '[ -~]\0' | wc -c) -gt 0 ]; then
    echo "Error: Attempt to add a non-ascii file name."
    echo
    echo "This can cause problems if you want to work"
    echo "with people on other platforms."
    echo
    echo "To be portable it is advisable to rename the file ..."
    echo
    exit 1
fi
echo " Done"
echo


# Run Python syntax check on staged .py files
if [ $(git diff-index --cached --name-only --diff-filter=ACMR $COMMIT | grep \\.py | wc -l) -gt 0 ]; then
    echo "*Python file(-s) changed, running syntax check.."

    for file in $(git diff-index --cached --name-only --diff-filter=ACMR $COMMIT | grep \\.py); do
        python3 -c "import py_compile; py_compile.compile('$file', doraise=True)" 2>/dev/null

        if [ "$?" != "0" ]; then
            echo "!!! SYNTAX ERROR: $file"
            exit 1
        fi
    done

    echo " Done"
    echo
fi


# Run code tests (detect if inside Docker or on host)
echo "*Running code tests... "
if [ -f /.dockerenv ]; then
    /srv/app/docker/app/scripts/code_tests.bash
else
    docker compose run --rm develop /srv/app/docker/app/scripts/code_tests.bash
fi

if [ "$?" != "0" ]; then
    echo "!!! ERROR: Code tests failed!"
    exit 1
fi
echo " Done"
echo


# Bump patch version on main/master
if [[ "$BRANCH" == "main" || "$BRANCH" == "master" ]]; then
    echo "*On $BRANCH - bumping version..."
    "$BASE_PATH/scripts/bump_version.bash" patch

    if [ "$?" != "0" ]; then
        echo "!!! ERROR: Version bump failed!"
        exit 1
    fi

    # Update commit info if script exists
    if [ -f "$BASE_PATH/scripts/update_commit_info.bash" ]; then
        "$BASE_PATH/scripts/update_commit_info.bash"
    fi

    echo " Done"
    echo
else
    echo "*Not on main/master - skipping version bump."
    echo
fi


# Test for whitespace errors
echo "*Testing for whitespace errors.. "
git diff-index --cached --check $COMMIT --
if [ "$?" != "0" ]; then
    echo "!!! ERROR !!!"
    exit 1
fi
echo " Done"
echo
