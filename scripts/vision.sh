#!/usr/bin/env bash
# Switch camera analysis between the local model and Anthropic, or show status.
#
#   scripts/vision.sh status      what is running, recent analyses, Ollama, memory
#   scripts/vision.sh local       switch new analyses to the local Ollama model
#   scripts/vision.sh anthropic   switch new analyses back to Anthropic
#
#   Add --check to a switch to run every check and validation, then stop before
#   backing up, editing .env or recreating anything.
#
# A switch edits only the vision and timing keys in .env (every other line,
# secrets included, is copied unchanged and never printed), validates the
# result in a throwaway container before touching anything, backs up the
# database, recreates the container, and waits for readiness. If the service
# does not come back ready, the previous .env is restored and the container
# recreated again, so a failed switch leaves things as they were.
#
# Nothing here reanalyzes history or migrates the database. Existing analyses
# keep the provider, model and prompt version they were made with.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ENV_FILE="$ROOT/.env"
BACKUP_DIR="${BACKUP_DIR:-$HOME/hermes-home-backups}"
SERVICE="hermes-home"
IMAGE="hermes-home:0.1.0"            # the name docker-compose.yml always runs
CANDIDATE="hermes-home:ollama-candidate"
PREVIOUS="hermes-home:pre-ollama"
PYTHON="/usr/bin/python3"
READY_URL="http://127.0.0.1:8099/ready"
HEALTH_URL="http://127.0.0.1:8099/health"
HOST_OLLAMA="http://127.0.0.1:11434"

LOCAL_MODEL="${LOCAL_MODEL:-gemma4:31b}"
ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-claude-sonnet-5}"

# Measured and reviewed in docs/operations.md ("Local vision (Ollama)").
LOCAL_SETTINGS=(
    "VISION_PROVIDER=ollama"
    "VISION_MODEL=$LOCAL_MODEL"
    "OLLAMA_BASE_URL=http://host.docker.internal:11434"
    "OLLAMA_NUM_CTX=8192"
    "OLLAMA_NUM_PREDICT=2048"
    "OLLAMA_THINK=false"
    "OLLAMA_KEEP_ALIVE=30m"
    "VISION_TIMEOUT_SECONDS=90"
    "INGEST_WORKER_CONCURRENCY=1"
    "INGEST_LEASE_SECONDS=300"
)
# The values Anthropic ran with before local vision existed. OLLAMA_* lines are
# left in place; they are ignored while Anthropic is selected.
ANTHROPIC_SETTINGS=(
    "VISION_PROVIDER=anthropic"
    "VISION_MODEL=$ANTHROPIC_MODEL"
    "VISION_TIMEOUT_SECONDS=30"
    "INGEST_WORKER_CONCURRENCY=1"
    "INGEST_LEASE_SECONDS=120"
)

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# Print one key's value from .env. Only ever called for non-secret keys.
env_value() {
    "$PYTHON" -I - "$ENV_FILE" "$1" <<'PY'
import re, sys
pat = re.compile(r"^\s*(?:export\s+)?" + re.escape(sys.argv[2]) + r"\s*=(.*)$")
value = ""
for line in open(sys.argv[1], encoding="utf-8"):
    m = pat.match(line.rstrip("\n"))
    if m:
        value = m.group(1).strip().strip('"').strip("'")
print(value)
PY
}

# Whether a key has a non-empty value, without printing it.
env_has_value() {
    "$PYTHON" -I - "$ENV_FILE" "$1" <<'PY'
import re, sys
pat = re.compile(r"^\s*(?:export\s+)?" + re.escape(sys.argv[2]) + r"\s*=(.*)$")
found = False
for line in open(sys.argv[1], encoding="utf-8"):
    m = pat.match(line.rstrip("\n"))
    if m:
        found = bool(m.group(1).strip().strip('"').strip("'"))
sys.exit(0 if found else 1)
PY
}

docker_up() { docker info >/dev/null 2>&1; }

image_has_local_vision() {
    docker image inspect "$1" >/dev/null 2>&1 &&
        docker run --rm --entrypoint python "$1" -c "import hermes_home.vision.ollama" >/dev/null 2>&1
}

image_revision() {
    docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$1" 2>/dev/null |
        sed 's/<no value>//' | cut -c1-12
}

wait_ready() {
    local i
    for i in $(seq 1 45); do
        if curl -fsS "$READY_URL" >/dev/null 2>&1; then return 0; fi
        sleep 2
    done
    return 1
}

running_provider() {
    curl -fsS "$HEALTH_URL" 2>/dev/null |
        "$PYTHON" -I -c 'import json,sys; d=json.load(sys.stdin); print(d.get("vision_provider") or "", d.get("vision_model") or "")' 2>/dev/null ||
        true
}

ollama_model_has_vision() {
    curl -fsS -m 5 "$HOST_OLLAMA/api/show" -d "{\"model\":\"$1\"}" 2>/dev/null |
        "$PYTHON" -I -c 'import json,sys; sys.exit(0 if "vision" in (json.load(sys.stdin).get("capabilities") or []) else 1)' 2>/dev/null
}

ollama_limit() {
    grep "server config" "$HOME/.ollama/logs/server.log" 2>/dev/null | tail -1 | tr ' ' '\n' |
        grep -E "^$1:" | cut -d: -f2 | tr -d ']'
}

# --------------------------------------------------------------------------- #

switch_to() {
    local target="$1"; shift
    local settings=("$@")

    [ -f "$ENV_FILE" ] || die "no .env at $ENV_FILE"
    docker_up || die "Docker is not running. Open Docker Desktop and try again."

    step "Checking the image"
    # The image validated below, and run after the switch.
    local use_image="$IMAGE"
    if ! image_has_local_vision "$IMAGE"; then
        # First activation: the running image predates provider switching.
        if image_has_local_vision "$CANDIDATE"; then
            use_image="$CANDIDATE"
        elif [ "$target" = "ollama" ]; then
            die "$IMAGE has no local-vision support and there is no $CANDIDATE. Build one with: make upgrade"
        fi
    fi
    say "image $use_image rev $(image_revision "$use_image" | grep . || echo "(unlabelled)")"

    step "Checking the provider"
    if [ "$target" = "ollama" ]; then
        ollama_model_has_vision "$LOCAL_MODEL" ||
            die "Ollama is not reachable on $HOST_OLLAMA, or $LOCAL_MODEL is not installed with vision. Start Ollama and check: ollama show $LOCAL_MODEL"
        say "Ollama has $LOCAL_MODEL with vision"
        local loaded
        loaded="$(ollama_limit OLLAMA_MAX_LOADED_MODELS)"
        if [ -n "$loaded" ] && [ "$loaded" -lt 2 ]; then
            say "WARNING: Ollama keeps only $loaded loaded model(s); the camera model and Hermes's model"
            say "         will evict each other (25-80 s analyses). See the LaunchAgent in docs/operations.md."
        fi
    else
        env_has_value ANTHROPIC_API_KEY ||
            die "ANTHROPIC_API_KEY is empty in .env; Anthropic cannot be selected without it"
        say "ANTHROPIC_API_KEY is present (not shown)"
    fi

    step "Preparing and validating the new settings"
    mkdir -p "$BACKUP_DIR"
    chmod 700 "$BACKUP_DIR" 2>/dev/null || true
    local stamp new saved
    stamp="$(date -u +%Y%m%d-%H%M%S)"
    new="$BACKUP_DIR/env.next-$stamp"
    saved="$BACKUP_DIR/env.before-$target-$stamp"
    "$PYTHON" -I "$ROOT/scripts/set_env_keys.py" "$ENV_FILE" "$new" "${settings[@]}"
    if ! docker run --rm --entrypoint hermes-home --env-file "$new" \
            -e CONFIG_DIR=/app/config -v "$ROOT/config:/app/config:ro" "$use_image" check-config; then
        rm -f "$new"
        die "the new settings failed validation; nothing was changed"
    fi
    if [ "$target" = "ollama" ]; then
        docker run --rm --entrypoint hermes-home --env-file "$new" "$use_image" vision check >/dev/null ||
            { rm -f "$new"; die "the container cannot use $LOCAL_MODEL through Ollama; nothing was changed"; }
        say "container can reach Ollama and $LOCAL_MODEL reports vision"
    fi
    if [ "$CHECK_ONLY" = "1" ]; then
        rm -f "$new"
        say ""
        say "Check passed. Nothing was changed: .env, the database and the container are untouched."
        say "Run without --check to switch."
        return 0
    fi

    step "Backing up the database"
    "$ROOT/scripts/backup.sh" | sed 's/^/  /'

    step "Switching to $target"
    if [ "$use_image" != "$IMAGE" ]; then
        if ! docker image inspect "$PREVIOUS" >/dev/null 2>&1; then
            docker tag "$IMAGE" "$PREVIOUS"
            say "preserved the current image as $PREVIOUS"
        fi
        docker tag "$use_image" "$IMAGE"
        say "installed $use_image as $IMAGE (the previous image stays tagged $PREVIOUS)"
    fi
    install -m 600 "$ENV_FILE" "$saved"
    install -m 600 "$new" "$ENV_FILE"
    rm -f "$new"
    say "previous .env saved privately as $saved"
    docker compose up -d --no-build --force-recreate "$SERVICE"

    step "Waiting for readiness"
    if ! wait_ready; then
        say "not ready after 90 s; restoring the previous settings"
        install -m 600 "$saved" "$ENV_FILE"
        docker compose up -d --no-build --force-recreate "$SERVICE"
        wait_ready && say "previous settings restored and ready" || say "previous settings restored but NOT ready; run: make logs"
        die "switch to $target failed; see: docker compose logs --since 10m $SERVICE"
    fi

    local now
    now="$(running_provider)"
    say "running: $now"
    case "$now" in
        "$target "*) ;;
        *) die "the service is ready but reports '$now', not $target" ;;
    esac
    say ""
    say "Switched. New analyses use $target; nothing proves it until one is stored."
    say "Trigger a camera, wait a minute, then run: scripts/vision.sh status"
}

# --------------------------------------------------------------------------- #

status() {
    step "Service"
    if ! docker_up; then
        say "Docker is NOT running. Open Docker Desktop; hermes-home restarts on its own once it is up."
        return 1
    fi
    local state image
    state="$(docker inspect --format '{{.State.Status}} since {{.State.StartedAt}}' "$SERVICE" 2>/dev/null || echo "no container")"
    image="$(docker inspect --format '{{.Image}}' "$SERVICE" 2>/dev/null || true)"
    say "container: $state"
    [ -n "$image" ] && say "image:     ${image:7:12} rev $(image_revision "$image" | grep . || echo "(unlabelled)")"
    say "running:   $(running_provider)"
    if curl -fsS "$READY_URL" >/dev/null 2>&1; then say "ready:     yes"; else say "ready:     NO (run: make ready)"; fi
    local configured
    configured="$(env_value VISION_PROVIDER) $(env_value VISION_MODEL)"
    say ".env:      $configured"
    case "$(running_provider)" in
        "$(env_value VISION_PROVIDER) "*) ;;
        *) say "NOTE: .env and the running service differ; the container was not recreated after an edit." ;;
    esac

    step "Last 5 analyses"
    docker compose exec -T "$SERVICE" sqlite3 -readonly -header -column /data/hermes-home.db \
        "select substr(e.occurred_at,1,19) as occurred, json_extract(e.payload,'\$.camera') as camera,
                a.status, a.provider, a.model, a.prompt_version, a.latency_ms as ms, a.error_code
         from event_analyses a join events e on e.id = a.event_id
         order by a.id desc limit 5" 2>/dev/null || say "(could not query the database)"

    step "Ollama"
    if curl -fsS -m 3 "$HOST_OLLAMA/api/version" >/dev/null 2>&1; then
        say "server:    $(curl -fsS -m 3 "$HOST_OLLAMA/api/version" | "$PYTHON" -I -c 'import json,sys; print(json.load(sys.stdin)["version"])')"
        say "limits:    max loaded models $(ollama_limit OLLAMA_MAX_LOADED_MODELS), parallel $(ollama_limit OLLAMA_NUM_PARALLEL)"
        "$PYTHON" -I - "$(curl -fsS -m 3 "$HOST_OLLAMA/api/ps")" <<'PY'
import json, sys
models = json.loads(sys.argv[1]).get("models", [])
if not models:
    print("loaded:    none")
for m in models:
    size = m.get("size_vram", 0) / 2**30
    until = m.get("expires_at", "")[11:16]
    print(f"loaded:    {m['name']:<20} {size:5.1f} GB  ctx {m.get('context_length')}  until {until}")
PY
    else
        say "server:    NOT reachable on $HOST_OLLAMA (local analyses will fail until Ollama runs)"
    fi
    local agent
    agent="$(launchctl print "gui/$(id -u)/local.ollama.limits" 2>/dev/null | awk -F'= ' '/last exit code/ {print $2; exit}')"
    if [ -n "$agent" ]; then say "limits agent: loaded, last exit $agent"; else say "limits agent: NOT loaded"; fi

    step "Memory"
    local level
    level="$(sysctl -n kern.memorystatus_vm_pressure_level 2>/dev/null || echo '?')"
    case "$level" in 1) level="normal" ;; 2) level="warning" ;; 4) level="CRITICAL" ;; esac
    say "pressure:  $level, $(memory_pressure 2>/dev/null | awk -F': ' '/free percentage/ {print $2}') free"
    say "swap:      $(sysctl -n vm.swapusage | sed 's/.*used = \([^ ]*\).*/\1/') used"

    local settings_file="$HOME/Library/Group Containers/group.com.docker/settings-store.json"
    if [ -f "$settings_file" ] && "$PYTHON" -I -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("AutoStart") else 1)' "$settings_file" 2>/dev/null; then
        :
    else
        step "Reboot readiness"
        say "Docker Desktop does not start at sign-in, so hermes-home will not run after a reboot"
        say "until Docker Desktop is opened. Turn on: Docker Desktop > Settings > General >"
        say "\"Start Docker Desktop when you sign in to your computer\"."
    fi
}

CHECK_ONLY=0
if [ "${2:-}" = "--check" ]; then CHECK_ONLY=1; fi

case "${1:-}" in
    status) status ;;
    local) switch_to ollama "${LOCAL_SETTINGS[@]}" ;;
    anthropic) switch_to anthropic "${ANTHROPIC_SETTINGS[@]}" ;;
    *)
        sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
        exit 2
        ;;
esac
