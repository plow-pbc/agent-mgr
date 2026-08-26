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
AGENT_KEYS="AGENT_NAME AGENT_DIR AGENT_HOME AGENT_CONTAINER AGENT_PROJECT AGENT_TZ AGENT_IMAGE AGENT_CONFIG AGENT_RESTORE_HOOK AGENT_PRE_TRANSITION"

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
    AGENT_HOOK_ENV=()
    while IFS= read -r line; do
        case "$line" in
            \#*|'') continue ;;
            [A-Za-z_]*=*) ;;
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
        # Only AGENT_KEYS reaches THIS process. Everything else a descriptor
        # declares is collected for the hooks and never exported here.
        #
        # The previous shape exported every key, which handed a registered repo
        # the dispatcher: AGENT_MGR_ROOT decides where lib/resolve-guard is
        # loaded from, so a descriptor setting it made ordinary lifecycle
        # commands run that repo's code with the operator's credentials. A
        # denylist could not close that -- it is an allowlist problem, because
        # the dangerous names are whatever this tool happens to read.
        if printf '%s' "$AGENT_KEYS" | grep -qw "$key"; then
            printf -v "$key" '%s' "$value"
        else
            # An instance's own variables -- STR_VAULT and friends -- which its
            # compose override and its hooks are written against. Passed to the
            # hooks as an environment, never into this process.
            AGENT_HOOK_ENV+=("$key=$value")
        fi
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
    # An instance's own restore step, if it has one -- seeding a corpus,
    # composing a prompt, whatever only that agent needs. agent-mgr sequences it
    # after its own installs so there is ONE deploy entry point; without it the
    # ordering falls to whoever reads the README, which is not an owner.
    # Always defined, empty when the instance declares none: every key in
    # AGENT_KEYS is printed by `resolve`, and an unset one is fatal under `set -u`.
    : "${AGENT_RESTORE_HOOK:=}"
    : "${AGENT_PRE_TRANSITION:=}"
    for _hook in AGENT_RESTORE_HOOK AGENT_PRE_TRANSITION; do
        case "${!_hook}" in
            ''|/*) ;;
            *) printf -v "$_hook" '%s' "$dir/${!_hook}" ;;
        esac
    done
    AGENT_DESCRIPTOR="$descriptor"

    HERMES_UID="$(id -u)"
    HERMES_GID="$(id -g)"

    export AGENT_NAME AGENT_DIR AGENT_HOME AGENT_CONTAINER AGENT_PROJECT \
           AGENT_TZ AGENT_IMAGE AGENT_CONFIG AGENT_RESTORE_HOOK AGENT_PRE_TRANSITION \
           AGENT_DESCRIPTOR HERMES_UID HERMES_GID
}

# Every Compose invocation goes through here so the file list, the override
# convention and the descriptor's env-file have exactly one definition.
compose() {
    local files=(-f "$AGENT_MGR_ROOT/templates/compose.yml")
    [ -f "$AGENT_DIR/compose.override.yml" ] && files+=(-f "$AGENT_DIR/compose.override.yml")
    docker compose -p "$AGENT_PROJECT" "${files[@]}" --env-file "$AGENT_DESCRIPTOR" "$@"
}

# Compose subcommands that stop or replace the container. `start`, `pause` and
# `unpause` are here because they interrupt a running process just as surely as
# `down` does -- the earlier list omitted them, and the earlier classification
# flattened "$*", which matched the word anywhere in a prompt or a filename.
COMPOSE_TRANSITIONS="up down start stop restart kill rm create pause unpause scale"

# Every route to a container transition goes through here, so the instance's
# veto cannot be bypassed by adding a call site that forgets it.
compose_transition() {
    require_transition_allowed
    compose "$@"
}

# The subcommand of a compose argv, or empty. The first word that IS one --
# what precedes it is the project name, file flags and their values.
compose_subcommand() {
    local w
    for w in "$@"; do
        case " $COMPOSE_TRANSITIONS logs ps config exec version " in
            *" $w "*) printf '%s\n' "$w"; return ;;
        esac
    done
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

# The pinned Plow Chat plugin, into this agent's home. A function rather than
# only a subcommand because `restore` sequences it too: one deploy entry point
# means one place that knows the order, and duplicating the install inline would
# make that two.
#
# Reloads nothing -- callers do, once, after everything boot-read has landed.
install_plow_plugin() {
    local ref
    ref="${AGENT_MGR_PLUGIN_REF:-$(tr -d '[:space:]' < "$AGENT_MGR_ROOT/runtime/plow-chat-plugin.ref")}"
    # A SHA, never a branch: a branch would silently re-point a running agent on
    # the next upstream push, and this plugin holds the chat token.
    [[ "$ref" =~ ^[0-9a-f]{40}$ ]] || die "the plugin ref must be a 40-char SHA, got: $ref"
    # Through the same installer skills use. It was fetching a 270-line
    # general-purpose installer out of the old SEED and using one narrow path
    # through it; what that path needs -- a snapshot at the pinned ref, a
    # manifest name check, and a staged swap with a rollback trap -- fetch-tree
    # already does, and does correctly. This is the install whose half-written
    # state takes an agent off its phone line, so it is the last one that should
    # have had its own hand-rolled copy.
    #
    # The whole directory is replaced rather than overlaid, which is also what
    # clears the ref/hermes-plugin/plow_chat/ tree the old SEED layout left
    # inside every agent's home.
    #
    # `gh api`, so restore now needs an authenticated gh on every host -- it used
    # to need one only for an instance that shipped a skills.tsv. Said here
    # because fetch-tree's own message names a repo and a ref and nothing about
    # which step this was or what already landed.
    #
    # What landed comes from the CALLER, because the two disagree: from restore
    # the dotenv is written and config.yaml is not, while install-plugin gates on
    # an existing home and leaves config and skills untouched. One hard-coded
    # sentence told the install-plugin operator their live config was gone, and
    # the obvious response to that is to re-run restore over a healthy agent.
    "$AGENT_MGR_ROOT/lib/fetch-tree" "$AGENT_HOME" plugins plugin.yaml \
        plow-pbc/hermes-plow-chat "$ref" plow-chat-platform plow-chat-platform \
        || die "could not install the Plow Chat plugin from plow-pbc/hermes-plow-chat at ${ref:0:7} -- is 'gh' installed and authenticated (gh auth status)? ${1:?the caller must say what landed}"
}

# Refuse to write into a home that is not this agent's.
#
# The conventional home always carries the agent's own name, so it is always
# allowed. A bare `.hermes` is the legacy shape and is allowed only when the
# descriptor SAYS SO -- a copied descriptor that inherited a sibling's name
# fails the first test, and one that fell back to the convention can never
# produce a bare `.hermes` at all.
#
# Required before EVERY direct write, not just activate. resolve-guard proves
# Compose agrees with the descriptor, which a copied descriptor naming a
# sibling's home satisfies perfectly -- it is self-consistent and wrong. This is
# the check that catches that, and restore, install-plugin and add-skill all
# write into the home without going near Compose.
require_own_home() {
    # No two registered agents may resolve to the same home. This is what
    # actually closes the legacy exception: a descriptor copied from the rentals
    # agent declares its bare `.hermes` and satisfies any name-shape test, being
    # self-consistent and wrong. Asking the registry catches it whatever the
    # home is called, so the shape rule below no longer has to carry the weight.
    local other odir ohome
    while IFS=$'\t' read -r other odir; do
        [ -n "$other" ] && [ "$other" != "$AGENT_NAME" ] || continue
        # Resolved by load_agent in a subshell, not by a second parser here. A
        # sibling writing AGENT_HOME='$HOME/.hermes' stayed quoted under the
        # sed/strip version, compared unequal to the same resolved path, and the
        # collision went unseen -- one descriptor grammar, or the two disagree
        # exactly where it matters.
        ohome="$( load_agent "$other" >/dev/null 2>&1 && printf '%s' "$AGENT_HOME" )" || continue
        [ -n "$ohome" ] || continue
        [ "$ohome" = "$AGENT_HOME" ] \
            && die "refusing to write to $AGENT_HOME -- $other is already registered there"
    done < <(registry_list)

    case "$AGENT_HOME" in
        *"/.hermes-$AGENT_NAME") return 0 ;;
        */.hermes)
            # The legacy shape, allowed only when the descriptor says so -- the
            # convention can never produce a bare `.hermes`, so this is always a
            # deliberate declaration, and the collision check above is what
            # stops a second agent from making the same one.
            grep -qE '^[[:space:]]*AGENT_HOME=' "$AGENT_DESCRIPTOR" && return 0
            die "refusing to write to $AGENT_HOME -- ${AGENT_NAME} did not declare that home" ;;
    esac
    die "refusing to write to $AGENT_HOME -- that is not ${AGENT_NAME}'s own home"
}

# An instance's own veto on stopping or replacing its container.
#
# Declared once in the descriptor and invoked by agent-mgr before EVERY
# transition, rather than pasted into each documented sequence. The rentals
# agent's is a nightly-ingest check, and its doc copies drifted across three
# review rounds: one restatement got corrected while the others kept asserting
# the opposite placement, and a text scanner written to catch that could not
# see a table cell, a justfile recipe, or an imperative in prose. A hook the
# tool calls has none of those blind spots -- there is nothing left to restate.
#
# Fatal by design: a guard that says "not now" and is overridden is not a guard.
require_transition_allowed() {
    [ -n "$AGENT_PRE_TRANSITION" ] || return 0
    [ -x "$AGENT_PRE_TRANSITION" ] \
        || die "$AGENT_NAME declares a pre-transition guard at $AGENT_PRE_TRANSITION, which is missing or not executable"
    ( cd "$AGENT_DIR" && env "${AGENT_HOOK_ENV[@]}" "$AGENT_PRE_TRANSITION" ) \
        || die "${AGENT_NAME}'s pre-transition guard refused -- not transitioning the container"
}
