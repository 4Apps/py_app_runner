############
### Base ###
############

FROM debian:trixie-slim AS base

LABEL maintainer="Gints Murans gm@gm.lv"
LABEL description="Docker image for py_app_runner development"
LABEL version="0.1"

# Avoid interactive cli blockers
ENV DEBIAN_FRONTEND=noninteractive
ENV MAKEFLAGS=-j6

# Set timezone
RUN ln -sf /usr/share/zoneinfo/Europe/Riga /etc/localtime

# Install OS dependecies and base tools
RUN apt-get update -y \
    && apt-get install -y --no-install-recommends apt-utils

# Install base
RUN apt-get install -y --no-install-recommends \
    ca-certificates less nano curl wget zip unzip tree \
    python3 python3-pip python3-venv locales \
    libssl3 libpq5 \
    # * Clean up caches
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /var/cache/apt/*

# Locale
RUN printf "en_US.UTF-8 UTF-8\n" > /etc/locale.gen && locale-gen
ENV LANG=en_US.UTF-8

# Create virtualenv and upgrade pip
RUN python3 -m venv /srv/venv
ENV PATH="/srv/venv/bin:$PATH"
RUN python3 -m pip install --no-cache-dir --upgrade pip

# Non-root user + chown everything
ARG LOCAL_USER_ID=1000
ARG LOCAL_GROUP_ID=1000
RUN groupadd -g $LOCAL_GROUP_ID appuser \
    && useradd -m -u $LOCAL_USER_ID -g appuser -s /bin/bash appuser \
    && chown -R appuser:appuser /srv/venv

# Meta (root-owned, read-only)
RUN mkdir -p /srv/meta/scripts
COPY ./docker/app/scripts /srv/meta/scripts


###############
### Builder ###
###############

FROM base AS builder

# Install build dependecies
RUN apt-get update -y \
    && apt-get install -y --no-install-recommends \
    build-essential pkg-config python3-dev \
    libssl-dev libpq-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /var/cache/apt/*

# Install python app's runtime dependecies
COPY ./pyproject.toml ./README.md /srv/app/
RUN mkdir -p /srv/app/src/py_app_runner && python3 -m pip install --no-cache-dir -e /srv/app/[dev]


###################
### Development ###
###################

FROM builder AS development

# Install additional dependecies
RUN apt-get update -y \
    && apt-get install -y --no-install-recommends \
    gettext-base rsync procps git openssh-client \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /var/cache/apt/*

# Fix ownership of app dir
RUN chown -R appuser:appuser /srv/app

# Switch to non-root user
USER appuser

# Setup git
ARG GIT_NAME
ARG GIT_EMAIL
RUN git config --global user.email "$GIT_EMAIL" \
    && git config --global user.name "$GIT_NAME" \
    && git config --global pull.rebase true \
    && git config --global safe.directory /srv/app

# Setup bashrc
RUN cat /srv/meta/scripts/bashrc.bash >> ~/.bashrc

# Set runtime stuff
WORKDIR /srv/app
CMD ["sleep", "infinity"]
