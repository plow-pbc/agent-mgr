#!/usr/bin/env bash
# Shared state for every agent-mgr subcommand: where the registry lives, how an
# agent name becomes a directory, and how this process fails.
#
# Sourced, never executed. AGENT_MGR_ROOT is set by the entrypoint before this
# is read, so helpers can find their siblings without re-deriving the path.

die() { printf 'agent-mgr: %s\n' "$*" >&2; exit 1; }

# Overridable so tests never touch the operator's real registry.
: "${AGENT_MGR_REGISTRY:=${XDG_CONFIG_HOME:-$HOME/.config}/agent-mgr/agents}"


# The name column is compared as a FIELD, by awk, never interpolated into a
# pattern. That was three rounds of the same bug: a raw name reached a grep BRE
# in every one of these, so `unregister '.*'` wiped the fleet registry and
# `restore 's.r'` resolved str's row while deriving its home from the pattern.
# Guarding each site with a name check closed those and opened a worse one -- a
# hand-edited row (the documented pre-unregister practice) then made load_agent
# die inside the collision loop, refusing the one production bare-`.hermes`
# agent, with `unregister` refusing the same name and no way out but editing the
# file again. Comparing fields removes the class instead of gating it: any row an
# operator can see in `ls` is a row they can read and drop.
#
# Through ENVIRON rather than -v, which processes backslash escapes in the value,
# and concatenated with "" on both sides because awk compares NUMERICALLY when
# both operands look numeric -- so `007` and `7` would be the same row.
registry_valid_name() {
    case "${1:-}" in
        ''|*[!a-z0-9-]*) die "agent name must be lowercase letters, digits and dashes: ${1:-}" ;;
    esac
}

registry_add() {
    local name="$1" dir="$2"
    # Input hygiene, and only here and in `new`: this is where a name is CHOSEN.
    # Reading or dropping a row that already exists must not depend on it.
    registry_valid_name "$name"
    [ -d "$dir" ] || die "no such directory: $dir"
    dir="$(cd "$dir" && pwd)"
    mkdir -p "$(dirname "$AGENT_MGR_REGISTRY")"
    touch "$AGENT_MGR_REGISTRY"
    # Rewrite rather than append: registering a name twice must move it, not
    # leave two rows whose order silently decides which one wins.
    local tmp; tmp="$(mktemp)"
    name="$name" awk -F'\t' '($1 "") != (ENVIRON["name"] "")' "$AGENT_MGR_REGISTRY" > "$tmp"
    printf '%s\t%s\n' "$name" "$dir" >> "$tmp"
    sort -o "$tmp" "$tmp"
    mv "$tmp" "$AGENT_MGR_REGISTRY"
}

# Drop a row. The remedy for an agent that is GONE: register cannot do it (it
# refuses a directory that no longer exists), so without this the only way out
# of an unresolvable row is hand-editing the registry file. No name check, on
# purpose -- a row an operator can see in `ls` must be one they can drop,
# whatever it is called.
registry_remove() {
    local name="$1"
    [ -f "$AGENT_MGR_REGISTRY" ] || die "no registry at $AGENT_MGR_REGISTRY"
    name="$name" awk -F'\t' '($1 "") == (ENVIRON["name"] "") {found=1} END{exit !found}' \
        "$AGENT_MGR_REGISTRY" || die "$name is not registered"
    local tmp; tmp="$(mktemp)"
    name="$name" awk -F'\t' '($1 "") != (ENVIRON["name"] "")' "$AGENT_MGR_REGISTRY" > "$tmp"
    mv "$tmp" "$AGENT_MGR_REGISTRY"
}

registry_lookup() {
    local name="$1"
    [ -f "$AGENT_MGR_REGISTRY" ] || return 1
    local dir
    dir="$(name="$name" awk -F'\t' '($1 "") == (ENVIRON["name"] "") {print $2; exit}' "$AGENT_MGR_REGISTRY")"
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
  register <name> <dir>       point a name at an agent repo
  unregister <name>           drop a row -- for an agent whose repo is gone
  new <name> [dir]            scaffold a new agent repo and register it
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
    [ -f "$descriptor" ] || die "$dir has no agent.env -- an agent repo needs one"

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
    # Canonicalised once, here, because two spellings of one directory defeat
    # every check downstream: `$HOME//.hermes` and `$HOME/.hermes` address the
    # same home and compare unequal, so the collision loop would clear a copycat
    # and restore would overwrite the live sibling.
    AGENT_HOME="$(printf '%s' "$AGENT_HOME" | tr -s /)"
    AGENT_HOME="${AGENT_HOME%/}"
    : "${AGENT_CONTAINER:=hermes-$name}"
    : "${AGENT_PROJECT:=hermes-$name}"
    : "${AGENT_TZ:=America/Los_Angeles}"
    : "${AGENT_IMAGE:=nousresearch/hermes-agent@$(tr -d '[:space:]' < "$AGENT_MGR_ROOT/runtime/image.ref")}"
    # Where this agent's declarative config lives. Relative resolves against the
    # agent repo, so an agent whose config sits under runtime/ (the rentals
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

# Compose subcommands that leave the LIVE container as it is. `exec` runs inside
# one, `run` starts a separate throwaway beside it, `cp`/`pull`/`build`/`push`
# touch files and images -- none of them stops or replaces the gateway, which is
# what the veto guards. `run` is here deliberately: it is what the escape hatch
# exists for, its two live callers are a maintenance shell and a host-side
# ingest, and the thing that makes it dangerous -- an unreplaced entrypoint --
# is refused upstream by run_replaces_the_entrypoint rather than by the veto.
#
# An entry must be safe under EVERY flag it accepts, which is why `wait` is not
# here: `wait --down-project` drops the whole project when the first container
# stops, so listing it would reopen the same teardown-past-the-veto route the
# inversion was written to close.
#
# Stated as what is safe rather than what is dangerous, because a list of
# stoppers has to be complete to be correct and the one this replaced was not:
# `scale hermes=0` stops a container and was in neither list.
#
# Both directions cost something, so neither is free. An entry missing HERE is a
# hard refusal of a command that was safe -- and while a guard is refusing, that
# is every invocation of it, which is exactly when a maintenance shell is
# wanted. An entry missing from the old list skipped the veto silently. Refusing
# loudly is the better failure, which is why the list is this way round, but it
# is a trade rather than a free win.
COMPOSE_LEAVES_IT_RUNNING="logs ps config version top port images events ls exec run cp pull build push"

# The subset that reaches an EXISTING container, and so needs it identified.
# `config`, `version`, `ls`, `images`, `build`, `pull` and `push` do not, and
# `run` starts a throwaway beside it -- gating those would make `config`
# require a live daemon it never needed.
COMPOSE_ADDRESSES_A_CONTAINER="logs exec cp top port"

# The container that already EXISTS under this project may not be ours.
#
# Every other check here compares the descriptor against the config agent-mgr
# WOULD apply, and a descriptor for a name that collides with a live agent is
# perfectly self-consistent: isolate HOME and the registry as thoroughly as you
# like and `-p hermes-rowan` still addresses production, because Docker's
# namespace is global and the project is derived from the agent NAME. So the
# last check has to be against the running container itself.
#
# This is not a test-only hazard, which is why it lives here rather than in the
# suite's fixtures: an interactive `agent-mgr restore rowan` from a scratch
# checkout restarted the live rentals gateway exactly this way. Deriving the
# project from the name stays deliberate -- two names may share one checkout,
# which is how one repo serves two people -- so the fix identifies the
# container rather than constraining the name.
require_running_container_is_ours() {
    local cid mounted
    # A compose that REFUSED to run is not "no container" -- conflating them is
    # exactly what reload-if-running's own comment rejects, and it would silently
    # disable this check on, say, a Compose too old for `--status`. The
    # subsequent `restart` does not use --status, so it would proceed.
    if ! cid="$(compose ps --status running --quiet hermes)"; then
        die "refusing to touch the container running as $AGENT_PROJECT -- docker could not say whether one is running"
    fi
    # Empty output is the ordinary first bring-up: nothing to misidentify.
    [ -n "$cid" ] || return 0
    mounted="$(docker inspect --format \
        '{{range .Mounts}}{{if eq .Destination "/opt/data"}}{{.Source}}{{end}}{{end}}' \
        "$cid")" \
        || die "refusing to touch the container running as $AGENT_PROJECT -- docker could not say whose home it mounts"
    mounted="$(printf '%s' "$mounted" | tr -s /)"; mounted="${mounted%/}"
    [ -n "$mounted" ] && [ "$mounted" = "$AGENT_HOME" ] && return 0
    # No removal command here, deliberately, and no branch that could produce
    # one. The obvious discriminator -- does the foreign home exist? -- is
    # evaluated as the invoking user on THIS host, while the mount is a path on
    # the docker host owned by whoever runs that agent. Another user's home is
    # unstattable under Ubuntu's default 750, and this tool explicitly supports
    # one repo serving two people, so a live gateway would routinely read as
    # "nobody's" and get a `docker rm -f` handed back for it.
    #
    # The asymmetry is total: the stray case is rare and costs one inspect to
    # sort out, while being wrong destroys a running business. So the message
    # ends at the question, not at an answer it cannot actually have.
    die "refusing to touch the container running as $AGENT_PROJECT -- it mounts ${mounted:-<nothing>} at /opt/data, not ${AGENT_NAME}'s home ($AGENT_HOME). The compose project comes from the agent NAME, so a name that collides with a live agent reaches it however isolated this descriptor is -- which usually means this descriptor wants a name of its own, or 'agent-mgr unregister $AGENT_NAME'. If you think that container is a leftover, check whose it is first: docker inspect $cid"
}

# Every route to a container transition goes through here, so the instance's
# veto cannot be bypassed by adding a call site that forgets it.
compose_transition() {
    require_running_container_is_ours
    require_transition_allowed
    compose "$@"
}

# Does this compose argv leave the container alone? The subcommand must be the
# FIRST word, so there is nothing to search and nothing to mistake: agent-mgr
# supplies the global options itself (-p, --env-file, -f), so a caller has none
# to pass, and scanning for the first recognised word let a global option's
# VALUE stand in for the subcommand -- `--project-name logs down` classified
# as `logs`.
compose_transitions_nothing() {
    case " $COMPOSE_LEAVES_IT_RUNNING " in
        *" ${1:-} "*) return 0 ;;
        *) return 1 ;;
    esac
}

# `run` must replace the image entrypoint, or s6 boots a gateway beside the live
# one. --entrypoint has to come before the SERVICE, which is the first word
# after `run` that is not a flag or a flag's value: docker treats a later one as
# an argument to the service's own command, so the flag is present, s6 still
# starts, and a substring check for it passes.
run_replaces_the_entrypoint() {
    shift  # the `run` subcommand itself
    local a
    while [ $# -gt 0 ]; do
        a="$1"; shift
        case "$a" in
            --entrypoint|--entrypoint=*) return 0 ;;
            # Flags that consume the next word. Anything else starting with `-`
            # is a bare flag; anything not starting with `-` is the service, and
            # by then it is too late.
            -e|--env|-l|--label|-p|--publish|-u|--user|-v|--volume|-w|--workdir|--name)
                shift || true ;;
            -*) ;;
            *) return 1 ;;
        esac
    done
    return 1
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
    # Running is not the same as OURS, and misidentification is not
    # transition-specific: `agent-mgr agent rowan "<prompt>"` would otherwise
    # exec a turn inside PRODUCTION's gateway and answer into the live owners'
    # channel, which is worse than restarting it.
    require_running_container_is_ours
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
    local tmp; tmp="$(mktemp)"
    curl -fsSL "https://raw.githubusercontent.com/plow-pbc/seed-hermes-plow/$ref/ref/scripts/install_direct_mount.sh" -o "$tmp" \
        || { rm -f "$tmp"; die "could not fetch the plugin installer at ${ref:0:7}"; }
    PLOW_CHAT_PLUGIN_REF="$ref" bash "$tmp" --data-dir "$AGENT_HOME"
    rm -f "$tmp"
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
    # Resolved by load_agent in a subshell, not by a second parser here. A peer
    # parser has to re-derive quote stripping, ${HOME} expansion and the
    # convention default, and the copy drifted immediately: this one stripped
    # only double quotes, so a sibling declaring AGENT_HOME='"'"'$HOME/.hermes'"'"'
    # compared unequal to the same path and the collision went undetected --
    # which is the one thing this loop exists to catch.
    #
    # A sibling load_agent cannot resolve leaves the collision set INCOMPLETE,
    # and that is recorded rather than shrugged off: silently dropping the row
    # turns the one fail-closed check in this tool into a fail-open one. Moving
    # the rentals agent's repo would stop `str` resolving, and a copycat
    # declaring the same bare `.hermes` would then pass and write config and
    # credentials into a live agent's mounted home.
    local other odir ohome skipped=0
    while IFS=$'\t' read -r other odir; do
        [ -n "$other" ] && [ "$other" != "$AGENT_NAME" ] || continue
        # `|| true` is load-bearing under set -e: a bare assignment carries the
        # substitution's status, and this is a while BODY rather than a
        # condition, so a sibling load_agent refuses -- a registry row whose dir
        # was moved, a repo with no agent.env -- would abort the caller. With
        # both streams already redirected the operator would get exit 1 and no
        # output, from one unrelated stale row, on every direct-write command.
        ohome="$( load_agent "$other" >/dev/null 2>&1 && printf '%s' "$AGENT_HOME" )" || true
        if [ -z "$ohome" ]; then
            skipped=1
            echo "agent-mgr: could not resolve $other -- the collision check skipped that row" >&2
            continue
        fi
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
            #
            # Which is why an incomplete check refuses this arm. Only the bare
            # home rests on the collision loop having seen every sibling; the
            # conventional one carries the agent's own name and cannot collide.
            # So a skipped row costs a refusal here and nothing anywhere else.
            [ "$skipped" -eq 0 ] \
                || die "refusing to write to $AGENT_HOME -- a registered agent could not be resolved, so this tool cannot prove no one else claims that home. Restore that repo, or drop the row with 'agent-mgr unregister <name>' if the agent is gone."
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
