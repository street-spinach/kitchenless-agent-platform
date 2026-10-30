#!/usr/bin/env bash
# GCE startup script for spinach-vm (ENGINEERING_PROCESS.md section 13.2).
# Runs as root on every boot; every step is idempotent. Secrets (infra/.env) are never set here.
set -euo pipefail
log() { echo "spinach-startup: $*"; }

# 2 GB swap as headroom for the 4 GB e2-medium.
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
swapon --show | grep -q /swapfile || swapon /swapfile

# Docker Engine with the Compose plugin, make and git.
if ! command -v docker >/dev/null; then
  apt-get update -q
  apt-get install -y -q ca-certificates curl git make
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

# deploy user (in the docker group) and /opt/app.
id deploy >/dev/null 2>&1 || useradd -m -s /bin/bash -G docker deploy
install -d -o deploy -g deploy /opt/app
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh

# Read-only GitHub deploy key. The private half never leaves the VM; the public half is logged
# to the serial console so it can be added to the repo's deploy keys.
if [ ! -f /home/deploy/.ssh/github_deploy ]; then
  sudo -u deploy ssh-keygen -q -t ed25519 -N "" -C "spinach-vm deploy key" \
    -f /home/deploy/.ssh/github_deploy
  printf 'Host github.com\n  IdentityFile ~/.ssh/github_deploy\n  IdentitiesOnly yes\n' \
    | sudo -u deploy tee /home/deploy/.ssh/config >/dev/null
  ssh-keyscan github.com 2>/dev/null | sudo -u deploy tee -a /home/deploy/.ssh/known_hosts >/dev/null
fi
log "deploy key: $(cat /home/deploy/.ssh/github_deploy.pub)"

# GitHub Actions public key, from instance metadata key gha-pubkey (authoritative when set).
gha_key=$(curl -fs -H 'Metadata-Flavor: Google' \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/gha-pubkey || true)
if [ -n "$gha_key" ]; then
  printf '%s\n' "$gha_key" > /home/deploy/.ssh/authorized_keys
  chown deploy:deploy /home/deploy/.ssh/authorized_keys
  chmod 600 /home/deploy/.ssh/authorized_keys
  log "GitHub Actions key authorized for deploy"
else
  log "no gha-pubkey metadata yet"
fi

log "done"
