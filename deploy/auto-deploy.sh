#!/usr/bin/env bash
#
# Pull-based deploy for the hosted server.
#
# The VM checks GitHub for a new commit on the tracked branch and deploys it
# only if the test matrix passed on exactly that commit. Nothing is pushed to
# the VM, so no inbound access is opened and no private key is stored in
# GitHub. If the new container does not become healthy, the previous commit is
# restored and rebuilt.
#
# Run it from the systemd timer in deploy/systemd/, or by hand:
#
#   sudo /opt/cognicore-env/deploy/auto-deploy.sh            # deploy if ready
#   sudo /opt/cognicore-env/deploy/auto-deploy.sh --dry-run  # report only
#
# Exit codes: 0 nothing to do or deployed, 1 deploy failed and was rolled back.
set -euo pipefail

REPO_DIR=${COGNICORE_REPO_DIR:-/opt/cognicore-env}
BRANCH=${COGNICORE_DEPLOY_BRANCH:-main}
SLUG=${COGNICORE_REPO_SLUG:-cognicore-dev/cognicore-env}
# Check runs that must have passed. The test matrix is the gate; the other
# workflows (docs, dashboards, scanners) must not be able to block a deploy.
REQUIRED_CHECK_RE=${COGNICORE_REQUIRED_CHECK_RE:-^test \(3\.[0-9]+\)$}
HEALTH_TIMEOUT=${COGNICORE_HEALTH_TIMEOUT:-240}
LOCK_FILE=/run/cognicore-deploy.lock
DRY_RUN=0
[[ ${1:-} == --dry-run ]] && DRY_RUN=1

COMPOSE=(docker compose -f "$REPO_DIR/deploy/docker-compose.yml" --env-file "$REPO_DIR/deploy/.env")
# The repo is checked out by another user; root must be allowed to read it.
GIT=(git -c "safe.directory=$REPO_DIR" -C "$REPO_DIR")

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
die() { log "ERROR: $*"; exit 1; }

# Only one deploy at a time: a slow build must not race the next timer tick.
exec 9>"$LOCK_FILE"
flock -n 9 || { log "another deploy is running; skipping this tick"; exit 0; }

[[ -f "$REPO_DIR/deploy/.env" ]] || die "$REPO_DIR/deploy/.env is missing"

"${GIT[@]}" fetch --quiet origin "$BRANCH" || die "could not fetch origin/$BRANCH"
current=$("${GIT[@]}" rev-parse HEAD)
target=$("${GIT[@]}" rev-parse "origin/$BRANCH")

if [[ $current == "$target" ]]; then
    log "up to date at ${current:0:7}"
    exit 0
fi
log "new commit on $BRANCH: ${current:0:7} -> ${target:0:7}"

# ── Is the test matrix green on this exact commit? ───────────────────────────
# Unauthenticated: the repository is public, so no token is kept on the VM.
checks=$(curl -fsS --max-time 30 \
    -H 'Accept: application/vnd.github+json' \
    "https://api.github.com/repos/$SLUG/commits/$target/check-runs?per_page=100") \
    || die "could not read check runs for ${target:0:7}"

read -r -d '' CHECK_VERDICT_PY <<'PY' || true
import json, os, re, sys

pattern = re.compile(os.environ["REQUIRED_RE"])
runs = [r for r in json.load(sys.stdin)["check_runs"] if pattern.match(r["name"])]

if not runs:
    print("pending no matching check has reported yet")
elif any(r["status"] != "completed" for r in runs):
    print("pending " + ", ".join(sorted(r["name"] for r in runs if r["status"] != "completed")))
else:
    failed = sorted(r["name"] for r in runs if r["conclusion"] != "success")
    if failed:
        print("failed " + ", ".join(failed))
    else:
        print("passed " + ", ".join(sorted(r["name"] for r in runs)))
PY

verdict=$(printf '%s' "$checks" |
    REQUIRED_RE="$REQUIRED_CHECK_RE" python3 -c "$CHECK_VERDICT_PY") || die "could not evaluate check runs"

state=${verdict%% *}
detail=${verdict#* }
case "$state" in
    pending) log "tests still running on ${target:0:7} ($detail); will retry"; exit 0 ;;
    failed)  log "NOT deploying ${target:0:7}: failing checks: $detail"; exit 0 ;;
    passed)  log "tests passed on ${target:0:7}: $detail" ;;
    *)       die "unexpected verdict: $verdict" ;;
esac

if (( DRY_RUN )); then
    log "dry run: would deploy ${target:0:7}"
    exit 0
fi

# ── Deploy ───────────────────────────────────────────────────────────────────
# deploy/.env is gitignored, so a hard reset cannot discard the secrets.
log "deploying ${target:0:7}"
"${GIT[@]}" reset --hard --quiet "$target" || die "could not check out ${target:0:7}"

rollback() {
    log "ROLLING BACK to ${current:0:7}"
    "${GIT[@]}" reset --hard --quiet "$current"
    if "${COMPOSE[@]}" up -d --build >&2; then
        log "rolled back to ${current:0:7}"
    else
        log "ROLLBACK FAILED - the server may be down, intervene by hand"
    fi
}

if ! "${COMPOSE[@]}" up -d --build >&2; then
    log "build or start failed for ${target:0:7}"
    rollback
    exit 1
fi

# ── Wait for the container's own health check to pass ────────────────────────
container=$("${COMPOSE[@]}" ps -q mcp) || container=""
[[ -n $container ]] || { log "mcp container not found after start"; rollback; exit 1; }

deadline=$(( SECONDS + HEALTH_TIMEOUT ))
while (( SECONDS < deadline )); do
    status=$(docker inspect --format '{{.State.Health.Status}}' "$container" 2>/dev/null || echo unknown)
    case "$status" in
        healthy)   log "container healthy"; break ;;
        unhealthy) log "container reported unhealthy"; rollback; exit 1 ;;
    esac
    sleep 5
done

if [[ ${status:-} != healthy ]]; then
    log "container did not become healthy within ${HEALTH_TIMEOUT}s (last status: ${status:-unknown})"
    rollback
    exit 1
fi

# ── Confirm it is reachable the way users reach it ───────────────────────────
domain=$(grep -E '^COGNICORE_DOMAIN=' "$REPO_DIR/deploy/.env" | cut -d= -f2- | tr -d '"'"'"' ')
if [[ -n $domain ]]; then
    if curl -fsS --max-time 20 "https://$domain/health" >/dev/null; then
        log "https://$domain/health responding"
    else
        log "container is healthy but https://$domain/health is not responding"
        rollback
        exit 1
    fi
fi

log "deployed ${target:0:7} successfully"
