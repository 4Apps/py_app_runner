# Setup precommit link
if [ -d /srv/app/.git ] && [ -f /srv/app/scripts/git_pre_commit.bash ]; then
    ln -sf /srv/app/scripts/git_pre_commit.bash /srv/app/.git/hooks/pre-commit
fi

parse_git_branch() {
    git branch 2>/dev/null | sed -e '/^[^*]/d' -e 's/* \(.*\)/ (\1)/'
}

PS1='\[\033[01;32m\]\u@\h\[\033[00m\]:\[\033[01;34m\]\w\[\033[33m\]$(parse_git_branch)\[\033[00m\]\$ '
