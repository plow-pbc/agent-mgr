#!/usr/bin/env bash
# Shared state for every agent-mgr subcommand: where the registry lives, how an
# agent name becomes a directory, and how this process fails.
#
# Sourced, never executed. AGENT_MGR_ROOT is set by the entrypoint before this
# is read, so helpers can find their siblings without re-deriving the path.

die() { printf 'agent-mgr: %s\n' "$*" >&2; exit 1; }

# Overridable so tests never touch the operator's real registry.
: "${AGENT_MGR_REGISTRY:=${XDG_CONFIG_HOME:-$HOME/.config}/agent-mgr/agents}"

registry_path() { printf '%s\n' "$AGENT_MGR_REGISTRY"; }

# The name column is matched literally. A name is validated to [a-z0-9-] on the
# way in, so it carries no regex metacharacters -- but the match is anchored and
# tab-terminated anyway, so a future name that does cannot match its own prefix.
registry_add() {
    local name="$1" dir="$2"
    case "$name" in
        ''|*[!a-z0-9-]*) die "agent name must be lowercase letters, digits and dashes: $name" ;;
    esac
    [ -d "$dir" ] || die "no such directory: $dir"
    dir="$(cd "$dir" && pwd)"
    mkdir -p "$(dirname "$AGENT_MGR_REGISTRY")"
    touch "$AGENT_MGR_REGISTRY"
    # Rewrite rather than append: registering a name twice must move it, not
    # leave two rows whose order silently decides which one wins.
    local tmp; tmp="$(mktemp)"
    grep -v "^$name	" "$AGENT_MGR_REGISTRY" > "$tmp" || true
    printf '%s\t%s\n' "$name" "$dir" >> "$tmp"
    sort -o "$tmp" "$tmp"
    mv "$tmp" "$AGENT_MGR_REGISTRY"
}

registry_lookup() {
    local name="$1"
    [ -f "$AGENT_MGR_REGISTRY" ] || return 1
    local dir
    dir="$(grep "^$name	" "$AGENT_MGR_REGISTRY" | head -1 | cut -f2-)"
    [ -n "$dir" ] || return 1
    printf '%s\n' "$dir"
}

registry_list() {
    [ -f "$AGENT_MGR_REGISTRY" ] && cat "$AGENT_MGR_REGISTRY" || true
}

usage() {
    cat <<'USAGE'
usage: agent-mgr <command> [args]

  ls                          registered agents and where they live
  register <name> <dir>       point a name at an instance repo
  new <name> [dir]            scaffold a new instance repo and register it
  resolve <name>              print the descriptor as agent-mgr resolves it

  restore <name>              install config.yaml + .env skeleton into its home
  install-plugin <name>       Plow Chat plugin, from the fleet-wide pinned SHA
  add-skill <name> <repo> [--ref SHA] [--dest PATH] [--src PATH]
  activate <name>             mint the Plow Chat credential pair
  sign-in <name>              model OAuth for this agent

  up|down|restart|logs <name> lifecycle
  agent <name> "<prompt>"     run one turn in the running container
  check-latch <name>          prove the Latch relay credential
  check-connectors <name>     report Gmail/Slack linkage
USAGE
}

# Descriptor keys this tool owns. Every one is unset from the inherited
# environment before the descriptor is read, because Compose resolves shell
# variables ahead of --env-file: a stale AGENT_HOME exported in the caller's
# shell would otherwise silently mount a different agent's home, which is the
# same failure class that once rewrote a live home to uid 501:20.
AGENT_KEYS="AGENT_NAME AGENT_DIR AGENT_HOME AGENT_CONTAINER AGENT_PROJECT AGENT_TZ AGENT_IMAGE AGENT_CONFIG"

# Compose's own environment variables, unset for the same reason and with a
# sharper edge: COMPOSE_PROJECT_NAME outranks the template's `name:` attribute,
# so a stale one in the caller's shell files this agent's stack under another
# agent's project. container_name and the /opt/data source both still resolve
# exactly as expected, so nothing downstream notices -- `up` creates a stack
# under a foreign project against this agent's live home, and `down` then
# reports success having stopped nothing.
COMPOSE_KEYS="COMPOSE_PROJECT_NAME COMPOSE_FILE COMPOSE_ENV_FILE COMPOSE_ENV_FILES COMPOSE_PROFILES"

load_agent() {
    local name="${1:-}"
    [ -n "$name" ] || die "which agent? try 'agent-mgr ls'"

    local dir
    dir="$(registry_lookup "$name")" || die "$name is not registered -- run 'agent-mgr register $name <dir>'"
    [ -d "$dir" ] || die "$name points at $dir, which no longer exists"

    local descriptor="$dir/agent.env"
    [ -f "$descriptor" ] || die "$dir has no agent.env -- an instance repo needs one"

    # shellcheck disable=SC2086
    unset $AGENT_KEYS $COMPOSE_KEYS

    # Read, never execute. This used to dot-source the descriptor, which made a
    # file documented as declarative into host shell code: any repo registered
    # here could run arbitrary commands with the operator's credentials the
    # moment `agent-mgr resolve` touched it -- before a single Compose guard ran.
    #
    # Only AGENT_* is parsed. The override-only variables a descriptor also
    # carries (STR_REPO and friends) are none of this function's business:
    # Compose reads them itself through --env-file, with its own parser.
    #
    # $HOME is the one expansion, because it is the one the template documents.
    # Anything else stays literal -- a descriptor cannot reach $(...) or a
    # sibling variable, which is the whole point of not sourcing it.
    local line key value
    while IFS= read -r line; do
        case "$line" in
            \#*|'') continue ;;
            AGENT_*=*) ;;
            *) continue ;;
        esac
        key="${line%%=*}"
        value="${line#*=}"
        # One layer of surrounding quotes, the way a dotenv file is usually written.
        case "$value" in
            \"*\") value="${value#\"}"; value="${value%\"}" ;;
            "'"*"'") value="${value#\'}"; value="${value%\'}" ;;
        esac
        value="${value//\$\{HOME\}/$HOME}"
        value="${value//\$HOME/$HOME}"
        printf '%s' "$AGENT_KEYS" | grep -qw "$key" || continue
        printf -v "$key" '%s' "$value"
    done < "$descriptor"

    # Convention, applied only where the descriptor said nothing.
    AGENT_NAME="$name"
    AGENT_DIR="$dir"
    : "${AGENT_HOME:=$HOME/.hermes-$name}"
    : "${AGENT_CONTAINER:=hermes-$name}"
    : "${AGENT_PROJECT:=hermes-$name}"
    : "${AGENT_TZ:=America/Los_Angeles}"
    : "${AGENT_IMAGE:=nousresearch/hermes-agent@$(tr -d '[:space:]' < "$AGENT_MGR_ROOT/runtime/image.ref")}"
    # Where this agent's declarative config lives. Relative resolves against the
    # instance repo, so an agent whose config sits under runtime/ (the rentals
    # agent does, beside the vault seed and SOUL it ships with) says so in one
    # line instead of keeping a second installer that hardcodes the path.
    : "${AGENT_CONFIG:=config.yaml}"
    case "$AGENT_CONFIG" in
        /*) ;;
        *) AGENT_CONFIG="$dir/$AGENT_CONFIG" ;;
    esac
    AGENT_DESCRIPTOR="$descriptor"

    HERMES_UID="$(id -u)"
    HERMES_GID="$(id -g)"

    export AGENT_NAME AGENT_DIR AGENT_HOME AGENT_CONTAINER AGENT_PROJECT \
           AGENT_TZ AGENT_IMAGE AGENT_CONFIG AGENT_DESCRIPTOR HERMES_UID HERMES_GID
}

# Every Compose invocation goes through here so the file list, the override
# convention and the descriptor's env-file have exactly one definition.
compose() {
    local files=(-f "$AGENT_MGR_ROOT/templates/compose.yml")
    [ -f "$AGENT_DIR/compose.override.yml" ] && files+=(-f "$AGENT_DIR/compose.override.yml")
    docker compose -p "$AGENT_PROJECT" "${files[@]}" --env-file "$AGENT_DESCRIPTOR" "$@"
}

# Refuse to act unless the resolved config is this agent's AND a gateway is up.
#
# The guard runs here rather than at each call site: sign-in and the reload paths
# mutate a running stack, and they were reaching Compose without it -- so an
# override that retargets /opt/data could take a credential write or a restart
# against a sibling's mounted home. One seam that every mutating path already
# passes through beats three call sites that each have to remember.
#
# The running check is separated from the empty case deliberately: piping
# straight into `grep -q .` treats a compose that REFUSED TO RUN the same as one
# reporting no container, so a failure to ask reads as "not running" and the
# caller proceeds on a false negative.
require_running() {
    "$AGENT_MGR_ROOT/lib/resolve-guard" "$AGENT_NAME"
    local running
    if ! running="$(compose ps --status running --quiet hermes 2>/dev/null)"; then
        die "could not ask docker whether ${AGENT_NAME}'s gateway is running"
    fi
    [ -n "$running" ] || die "${AGENT_NAME}'s gateway is not running -- start it first: agent-mgr up $AGENT_NAME"
}
