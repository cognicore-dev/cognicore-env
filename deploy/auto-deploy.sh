#!/usr/bin/env bash
#
# Pull-based deploy for the hosted server.
#
# The VM checks GitHub for a new commit on the tracked branch and deploys it
# only if the CI workflow passed on exactly that commit. Nothing is pushed to
# the VM, so no inbound access is opened and no credential is stored in GitHub
# or on the VM. If the new container does not become healthy, the previous
# commit is restored and rebuilt.
#
# What it trusts, and why:
#   * The CI result is read from the workflow-runs API and must come from the
#     workflow FILE .github/workflows/ci.yml, run by a push to the tracked branch
#     of this repository. A check run's name can be chosen by any job or app;
#     the workflow path, event and repository cannot.
#   * Application code deploys automatically. It runs in the container that
#     deploy/docker-compose.yml confines (read-only, no capabilities, non-root).
#     Changes to that confinement, or to anything else under deploy/, could
#     reach the host, so they are held for a person to deploy (--allow-infra).
#   * This script is installed as a root-owned copy outside the repository, so
#     a commit cannot change what root runs without someone reinstalling it.
#
# Install and run (see docs/remote-mcp-server.md, "Automatic deploys"):
#   sudo install -o root -g root -m 0755 deploy/auto-deploy.sh /usr/local/sbin/cognicore-deploy
#   sudo cognicore-deploy --dry-run       # report what would happen
#   sudo cognicore-deploy --allow-infra   # deploy a held deploy/ change after reviewing it
#   sudo cognicore-deploy --retry         # try again a commit whose deploy failed
#
# Exit codes: 0 nothing to do, waiting, held, skipped, or deployed;
#             1 error, or deploy failed and the previous commit is serving again;
#             2 deploy and rollback both failed: the server is down.
set -euo pipefail

REPO_DIR=${COGNICORE_REPO_DIR:-/opt/cognicore-env}
BRANCH=${COGNICORE_DEPLOY_BRANCH:-main}
SLUG=${COGNICORE_REPO_SLUG:-cognicore-dev/cognicore-env}
WORKFLOW_PATH=${COGNICORE_CI_WORKFLOW:-.github/workflows/ci.yml}
HEALTH_TIMEOUT=${COGNICORE_HEALTH_TIMEOUT:-240}
STATE_DIR=/var/lib/cognicore-deploy
LOCK_FILE=/run/cognicore-deploy.lock

# Paths whose change is held for a person. Everything under deploy/ shapes the
# host or the container's confinement; the image's Python requirements are
# application code and ship like it.
INFRA_RE='^deploy/'
INFRA_EXEMPT_RE='^deploy/requirements\.(in|txt)$'

DRY_RUN=0
ALLOW_INFRA=0
RETRY=0
for arg in "$@"; do
    case "$arg" in
        --dry-run)     DRY_RUN=1 ;;
        --allow-infra) ALLOW_INFRA=1 ;;
        --retry)       RETRY=1 ;;
        *) printf 'unknown option: %s\n' "$arg" >&2; exit 2 ;;
    esac
done

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
die() { log "ERROR: $*"; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (it drives Docker)"

COMPOSE=(docker compose -f "$REPO_DIR/deploy/docker-compose.yml" --env-file "$REPO_DIR/deploy/.env")
# Git runs as the checkout's owner, never as root. Files written by root carry
# root's umask, and Docker copies their modes into the image, so a source file
# root wrote as 0600 is unreadable to the container's non-root user: a deploy
# once crash-looped on "Permission denied" for exactly that reason. Hooks and
# fsmonitor stay off so the checkout's own config cannot run anything.
repo_owner=$(stat -c %U "$REPO_DIR") || die "$REPO_DIR does not exist"
owner_home=$(getent passwd "$repo_owner" | cut -d: -f6)
GIT=(runuser -u "$repo_owner" -- env HOME="$owner_home"
     git -c core.hooksPath=/dev/null -c core.fsmonitor=false -C "$REPO_DIR")

# Only one deploy at a time: a slow build must not race the next timer tick.
exec 9>"$LOCK_FILE"
flock -n 9 || { log "another deploy is running; skipping this tick"; exit 0; }
install -d -m 0700 "$STATE_DIR"

[[ -f "$REPO_DIR/deploy/.env" ]] || die "$REPO_DIR/deploy/.env is missing"

"${GIT[@]}" fetch --quiet origin "$BRANCH" || die "could not fetch origin/$BRANCH"
current=$("${GIT[@]}" rev-parse HEAD)
target=$("${GIT[@]}" rev-parse "origin/$BRANCH")

if [[ $current == "$target" ]]; then
    log "up to date at ${current:0:7}"
    exit 0
fi

# A commit whose deploy failed is not retried on every tick: each attempt
# restarts the server. A newer commit, or --retry, tries again.
if [[ $(cat "$STATE_DIR/failed" 2>/dev/null || true) == "$target" && $RETRY -eq 0 ]]; then
    log "skipping ${target:0:7}: its deploy failed and was rolled back; merge a fix, or run: sudo cognicore-deploy --retry"
    exit 0
fi

# ── Hold changes that could reach the host ───────────────────────────────────
infra_changes=$("${GIT[@]}" diff --name-only "$current" "$target" \
    | grep -E "$INFRA_RE" | grep -Ev "$INFRA_EXEMPT_RE" || true)
if [[ -n $infra_changes && $ALLOW_INFRA -eq 0 ]]; then
    # Log a held commit once, not on every tick.
    if [[ $(cat "$STATE_DIR/held" 2>/dev/null || true) != "$target" ]]; then
        log "HELD ${target:0:7}: it changes host-level files, which need a person to deploy:"
        while IFS= read -r path; do log "  $path"; done <<<"$infra_changes"
        log "review with: git -C $REPO_DIR diff ${current:0:7} ${target:0:7} -- deploy/"
        log "then run:    sudo cognicore-deploy --allow-infra"
        printf '%s\n' "$target" > "$STATE_DIR/held"
    fi
    exit 0
fi
log "new commit on $BRANCH: ${current:0:7} -> ${target:0:7}"

# ── Did CI pass on this exact commit? ────────────────────────────────────────
# Unauthenticated: the repository is public, so no token is kept on the VM.
runs=$(curl -fsS --max-time 30 \
    -H 'Accept: application/vnd.github+json' \
    "https://api.github.com/repos/$SLUG/actions/runs?head_sha=$target&event=push&per_page=100") \
    || die "could not read workflow runs for ${target:0:7}"

read -r -d '' CI_VERDICT_PY <<'PY' || true
import json, os, sys

want = {k: os.environ[k] for k in ("WORKFLOW_PATH", "BRANCH", "SLUG", "TARGET")}
runs = [
    r for r in json.load(sys.stdin)["workflow_runs"]
    if r["path"] == want["WORKFLOW_PATH"]
    and r["event"] == "push"
    and r["head_branch"] == want["BRANCH"]
    and r["head_sha"] == want["TARGET"]
    and (r.get("head_repository") or {}).get("full_name") == want["SLUG"]
]
if not runs:
    print("pending CI has not started for this commit")
    sys.exit()
run = max(runs, key=lambda r: (r["created_at"], r["run_attempt"]))
label = f"run {run['id']} attempt {run['run_attempt']}"
if run["status"] != "completed":
    print(f"pending {label} is {run['status']}")
elif run["conclusion"] == "success":
    print(f"passed {label}")
else:
    print(f"failed {label} concluded {run['conclusion']}")
PY

verdict=$(printf '%s' "$runs" |
    WORKFLOW_PATH="$WORKFLOW_PATH" BRANCH="$BRANCH" SLUG="$SLUG" TARGET="$target" \
    python3 -c "$CI_VERDICT_PY") || die "could not evaluate workflow runs"

state=${verdict%% *}
detail=${verdict#* }
case "$state" in
    pending) log "CI not finished on ${target:0:7} ($detail); will retry"; exit 0 ;;
    failed)  log "NOT deploying ${target:0:7}: $detail"; exit 0 ;;
    passed)  log "CI passed on ${target:0:7} ($detail)" ;;
    *)       die "unexpected verdict: $verdict" ;;
esac

if (( DRY_RUN )); then
    log "dry run: would deploy ${target:0:7}"
    if [[ -n $infra_changes ]]; then
        log "dry run: this includes host-level changes:"
        while IFS= read -r path; do log "  $path"; done <<<"$infra_changes"
    fi
    exit 0
fi

# ── Deploy ───────────────────────────────────────────────────────────────────
domain=$(sed -n 's/^COGNICORE_DOMAIN=//p' "$REPO_DIR/deploy/.env" | tr -d "\"' ")

# Is the mcp container healthy by its own check, and (when a domain is set)
# reachable the way users reach it?
server_healthy() {
    local container status=unknown deadline=$(( SECONDS + HEALTH_TIMEOUT ))
    container=$("${COMPOSE[@]}" ps -q mcp) || container=""
    [[ -n $container ]] || { log "mcp container not found"; return 1; }
    while (( SECONDS < deadline )); do
        status=$(docker inspect --format '{{.State.Health.Status}}' "$container" 2>/dev/null || echo unknown)
        case "$status" in
            healthy)   break ;;
            unhealthy) log "container reported unhealthy"; return 1 ;;
        esac
        sleep 5
    done
    if [[ $status != healthy ]]; then
        log "container did not become healthy within ${HEALTH_TIMEOUT}s (last status: $status)"
        return 1
    fi
    log "container healthy"
    if [[ -n $domain ]] && ! curl -fsS --max-time 20 "https://$domain/health" >/dev/null; then
        log "container is healthy but https://$domain/health is not responding"
        return 1
    fi
    [[ -z $domain ]] || log "https://$domain/health responding"
}

# Restore the previous commit and prove it is serving again. Exits.
rollback() {
    printf '%s\n' "$target" > "$STATE_DIR/failed"
    log "ROLLING BACK to ${current:0:7}"
    if "${GIT[@]}" reset --hard --quiet "$current" \
        && "${COMPOSE[@]}" up -d --build >&2 \
        && server_healthy; then
        log "rolled back to ${current:0:7}; the server is running it. ${target:0:7} will not be retried automatically."
        exit 1
    fi
    log "ROLLBACK FAILED - THE SERVER IS DOWN. Check: sudo docker compose -f $REPO_DIR/deploy/docker-compose.yml logs mcp"
    exit 2
}

# deploy/.env is gitignored, so a hard reset cannot discard the secrets.
log "deploying ${target:0:7}"
"${GIT[@]}" reset --hard --quiet "$target" || die "could not check out ${target:0:7}"

# Docker copies file modes into the image and the app runs as a non-root user,
# so a source file other users cannot read would crash it on start. Check
# before touching the running container: nothing has restarted yet.
unreadable=$(find "$REPO_DIR/cognicore" -type f ! -perm -o=r -print -quit)
if [[ -n $unreadable ]]; then
    "${GIT[@]}" reset --hard --quiet "$current" || true
    die "not deploying ${target:0:7}: $unreadable is not readable by the container's user; the running server is untouched"
fi

if ! "${COMPOSE[@]}" up -d --build >&2; then
    log "build or start failed for ${target:0:7}"
    rollback
fi
server_healthy || rollback

rm -f "$STATE_DIR/held" "$STATE_DIR/failed"
log "deployed ${target:0:7} successfully"
