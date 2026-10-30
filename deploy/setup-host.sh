#!/usr/bin/env bash
# Prepare a fresh Ubuntu 24.04 host to run the hosted CogniCore MCP server.
# Idempotent: safe to re-run. Does not start the server and touches no secrets.
#
#   sudo bash deploy/setup-host.sh [/dev/sdc]
#
# Pass the data disk device to format and mount at $DATA_DIR. Omit it to use a
# plain directory on the OS disk (fine for a trial, not for real data).

set -euo pipefail

DATA_DIR=${DATA_DIR:-/mnt/cognicore-data}
DEVICE=${1:-}
SWAP_FILE=/swapfile
SWAP_SIZE=${SWAP_SIZE:-2G}

if [[ $EUID -ne 0 ]]; then
	echo "Run with sudo." >&2
	exit 1
fi

echo "==> Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git ufw

echo "==> Docker"
if ! command -v docker >/dev/null; then
	install -m 0755 -d /etc/apt/keyrings
	curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
		-o /etc/apt/keyrings/docker.asc
	chmod a+r /etc/apt/keyrings/docker.asc
	echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
		>/etc/apt/sources.list.d/docker.list
	apt-get update -qq
	apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
		docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker

echo "==> Swap"
# A 1 GB instance (Azure B1s, Oracle micro) will OOM during a docker build or a
# traffic spike without swap.
if ! swapon --show | grep -q "$SWAP_FILE"; then
	fallocate -l "$SWAP_SIZE" "$SWAP_FILE"
	chmod 600 "$SWAP_FILE"
	mkswap "$SWAP_FILE" >/dev/null
	swapon "$SWAP_FILE"
	grep -q "^$SWAP_FILE" /etc/fstab || echo "$SWAP_FILE none swap sw 0 0" >>/etc/fstab
fi

echo "==> Data directory"
mkdir -p "$DATA_DIR"
if [[ -n "$DEVICE" ]]; then
	if [[ ! -b "$DEVICE" ]]; then
		echo "$DEVICE is not a block device. lsblk to find the data disk." >&2
		exit 1
	fi
	# Only format a disk that has no filesystem, so a re-run never eats data.
	if [[ -z "$(blkid -o value -s TYPE "$DEVICE" 2>/dev/null || true)" ]]; then
		echo "    formatting $DEVICE as ext4"
		mkfs.ext4 -F "$DEVICE" >/dev/null
	else
		echo "    $DEVICE already has a filesystem, keeping it"
	fi
	UUID=$(blkid -o value -s UUID "$DEVICE")
	grep -q "$UUID" /etc/fstab || \
		echo "UUID=$UUID $DATA_DIR ext4 defaults,nofail 0 2" >>/etc/fstab
	mountpoint -q "$DATA_DIR" || mount "$DATA_DIR"
fi
mkdir -p "$DATA_DIR/cognicore/remote"

echo "==> Firewall"
ufw allow 22/tcp >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

echo
echo "Host ready. Data at $DATA_DIR ($(df -h --output=size "$DATA_DIR" | tail -1 | tr -d ' ') available)."
echo
echo "Next:"
echo "  1. git clone https://github.com/cognicore-dev/cognicore-env /opt/cognicore-env"
echo "  2. cp deploy/.env.example deploy/.env  and fill it in"
echo "  3. point the DNS record at this host and wait for it to resolve"
echo "  4. docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build"
echo "  5. python scripts/checks/verify_remote_mcp.py https://<your domain>"
