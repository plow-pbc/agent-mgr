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
# `realpath -m` and `realpath -m -s` are GNU-only: BSD/macOS realpath has
# neither flag, so on a Mac every command that loads an agent died at the first
# call with "illegal option -- m" -- 178 of 220 tests with it. python3 is
# already a host dependency here (lib/resolve-guard parses compose config with
# it) and answers for a path that does not exist yet, which `-m` is for and
# which a first restore needs. Both forms take the path as an argument, so a
# leading dash is data rather than an option and no `--` is needed.
#
# Two functions because the two GNU forms mean different things, and the callers
# below depend on the difference: normalized_path leaves symlinks intact
# (`-m -s`), canonical_path follows them (`-m`).
#
# `-I` on both, and on lib/resolve-guard's parser -- every python3 this tool
# runs on the host. It drops PYTHONPATH and the user site directory, so none of
# them can be handed a `sitecustomize` to import. No test pins the flag, so this
# line is what keeps it. `os.path.abspath(` and `os.path.realpath(` each appear
# exactly once in anything this tool runs, which is what lets a test break one
# helper and not the other; a second caller of either moves which call that is.
#
# And the rule every caller of these two follows, stated once here because
# stating it at each site is what let one site be fixed and another missed:
# NEVER assign either one's output to the variable its refusal names. A failed
# substitution stores its empty output before the `||` arm runs, so such a
# refusal names the value it just erased -- "cannot resolve rowan's home ()".
# Resolve into a second variable and name the original.
normalized_path() {
    python3 -I -c 'import os, sys; print(os.path.abspath(sys.argv[1]))' "$1"
}

canonical_path() {
    python3 -I -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1"
}

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
  install-skill <name>        fleet google-workspace skill, from its pinned SHA
  add-skill <name> <repo> [--ref SHA] [--dest PATH] [--src PATH]
  cron-sync <name>            converge the repo's cron spec onto the scheduler
  activate <name>             mint the Plow Chat credential pair
  sign-in <name>              model OAuth for this agent
  set-latch <name>            read the Latch pair on stdin into its dotenv

  backup-homes <dest>         snapshot every agent home on this host
  prune-backups <dest> [days] drop backup runs older than [days] (14)

  up|down|restart|logs <name> lifecycle
  agent <name> "<prompt>"     run one turn in the running container
  chats <name>                the account's chats and lines; home chat marked
  set-home <name> <cht_...>   re-point the home chat -- line recovery after activate
  check-latch <name>          prove the Latch relay credential
  check-connectors <name>     report Gmail/Slack linkage
USAGE
}

# The agent-supplied repo-relative paths: the keys whose value is a path
# resolved against the agent's repo, and whose default is empty. Those two properties travel
# together, and three consumers key off them -- load_agent defines them from this
# list, the path loop below resolves them, and the parser exempts them from the
# empty-value refusal. Empty and unset mean the same thing here, so nothing fills
# in behind the operator and `AGENT_RESTORE_HOOK=` is the natural way to write
# "this agent has no restore step"; every other consumed key defaults to a real
# value, which is what makes an empty one a silent substitution. A key with an
# empty default that is NOT a repo-relative path does not belong here. Not all
# of them are executables -- AGENT_CRON_SPEC names a data file, and each use
# site owns its own existence/executability check.
AGENT_REPO_PATHS="AGENT_RESTORE_HOOK AGENT_PRE_TRANSITION AGENT_CRON_SPEC"

# Descriptor keys this tool owns. Every one is unset from the inherited
# environment before the descriptor is read, because Compose resolves shell
# variables ahead of --env-file: a stale AGENT_HOME exported in the caller's
# shell would otherwise silently mount a different agent's home, which is the
# same failure class that once rewrote a live home to uid 501:20.
AGENT_KEYS="AGENT_NAME AGENT_DIR AGENT_HOME AGENT_CONTAINER AGENT_PROJECT AGENT_TZ AGENT_IMAGE AGENT_CONFIG $AGENT_REPO_PATHS"


# Compose's own environment variables, unset for the same reason and with a
# sharper edge: COMPOSE_PROJECT_NAME outranks the template's `name:` attribute,
# so a stale one in the caller's shell files this agent's stack under another
# agent's project. container_name and the /opt/data source both still resolve
# exactly as expected, so nothing downstream notices -- `up` creates a stack
# under a foreign project against this agent's live home, and `down` then
# reports success having stopped nothing.
COMPOSE_KEYS="COMPOSE_PROJECT_NAME COMPOSE_FILE COMPOSE_ENV_FILE COMPOSE_ENV_FILES COMPOSE_PROFILES"

# Parse one declarative KEY=VALUE file. Read, NEVER execute.
#
# One copy, called for the agent's descriptor and again for the instance's
# own dotenv. A second parser is what this file already paid for once: the
# peer parser in require_own_home stripped only double quotes, so a sibling
# declaring its home with single quotes compared unequal and the collision
# it existed to catch went undetected. The grammar above is measured against
# compose-go; a second copy of it would drift from that measurement.
#
# $2 is the allowlist of keys that reach THIS process. $3 is the file's ROLE,
# which decides two things that differ between the two callers:
#
#   descriptor  Compose reads this same file through --env-file, so the grammar
#               must agree with compose-go's and a line Compose rejects is
#               refused here too. Its non-allowlisted keys go to the hooks.
#   dotenv      Compose never reads it. It is hand-maintained and written by the
#               gateway, holds credentials, and agent-mgr wants exactly one
#               non-secret value out of it -- so every other key is skipped
#               before validation, and nothing is said about it: a line this
#               tool does not consume is not its business to comment on, least
#               of all in a file full of secrets.
#
# The role does NOT buy leniency. A malformed value on a key that IS consumed is
# fatal in both files: this feature exists so an agent does not silently run on
# somebody else's clock, and warn-then-use-the-default is that failure wearing a
# diagnostic. Through require_own_home's fail-closed arm a broken dotenv stops
# the other agents' `activate`/`sign-in` too -- the deliberate cost of a resolver
# that will not guess.
parse_env_file() {
    local file="$1" allow="$2" role="$3" collect="" _lineno=0
    # Named, because the redirection that opens this file below reports an
    # unreadable one as a bare `line N: <path>: Permission denied` from the
    # sourcing shell -- which names this file rather than the agent, and reads
    # as a bug in agent-mgr rather than a permission problem on a dotenv the
    # operator can fix. A `.env` written 600 under another account is the
    # realistic way to get here. Same posture as the rest of this function:
    # fatal, not guessing.
    [ -r "$file" ] || die "cannot read $file"
    [ "$role" = descriptor ] && collect=hooks
    local line key value _rest
    # Expanded at its two call sites as ${AGENT_HOOK_ENV[@]+"..."} rather than
    # bare "${AGENT_HOOK_ENV[@]}": an agent with no extra descriptor keys leaves
    # this empty, and bash treats an empty array as unset under `set -u` until
    # 4.4 -- so on the 3.2 that macOS still ships, `restore` and every guarded
    # transition died with "AGENT_HOOK_ENV[@]: unbound variable".
    # `|| [ -n "$line" ]` so an unterminated final line is still seen. read
    # returns non-zero at EOF even when it filled $line, and that was tolerable
    # while this parsed only agent.env, which agent-mgr writes from its own
    # template. It is not for an instance's own dotenv: that file is maintained
    # by hand and by the gateway, so a last line with no newline is ordinary.
    while IFS= read -r line || [ -n "$line" ]; do
        _lineno=$((_lineno + 1))
        # Normalized the way compose-go's dotenv parser normalizes, because
        # Compose reads this SAME file through --env-file (see compose()) and
        # the two disagreeing about one file is the failure to avoid. Measured
        # against a real `docker compose --env-file`: leading whitespace, an
        # `export ` prefix, and space around the `=` are all accepted there and
        # read as the bare key. Refusing them here would make agent-mgr fail on
        # a descriptor Compose reads without complaint -- and via
        # require_own_home's fail-closed arm, fail every OTHER agent's
        # direct-write commands too.
        line="${line#"${line%%[![:space:]]*}"}"
        case "$line" in \#*|'') continue ;; esac
        case "$line" in
            export[[:space:]]*)
                line="${line#export}"
                line="${line#"${line%%[![:space:]]*}"}" ;;
        esac

        case "$line" in
            *=*) ;;
            # Not a declaration at all. Skipped, and parsing continues -- this
            # parser's existing contract, whose concern is that such a line is
            # never EXECUTED rather than that it is fatal.
            *) continue ;;
        esac
        key="${line%%=*}"
        value="${line#*=}"
        key="${key%"${key##*[![:space:]]}"}"
        value="${value#"${value%%[![:space:]]*}"}"

        # A dotenv is not this parser's to police. agent-mgr wants exactly one
        # key out of it; every other line is a credential or a gateway setting
        # it neither consumes nor owns, so validating those made it a partial
        # linter for fields belonging to someone else -- and that is precisely
        # where the leak risk lived, since a diagnostic about a malformed line
        # in a credential file has to describe the line without quoting it.
        #
        # Skipping first deletes that whole role. It costs nothing an operator
        # would notice: the near-misses that actually get typed -- spaces around
        # the `=`, `export`, indentation -- are normalized ABOVE this point, so
        # they still resolve. Only a genuine misspelling goes quiet, and its
        # symptom is immediate and local: the zone did not change.
        if [ "$role" = dotenv ] && [ "$key" != AGENT_TZ ]; then
            continue
        fi

        # What remains after normalization must be a real identifier. This is
        # where the execution hole was: a malformed key matched the allowlist as
        # a PATTERN and reached `printf -v`, where an array subscript is
        # evaluated arithmetically and arithmetic performs command substitution.
        # Compose errors on these too, so refusing is the agreeing behaviour.
        case "$key" in
            ''|*[!A-Za-z0-9_]*)
                # Plain die, not role-aware: the dotenv filter above admits only
                # the literal AGENT_TZ, which is a valid identifier, so this arm
                # is unreachable for that role. A role branch here would be dead
                # dispatch pretending to be a policy.
                # Locator, never content: for a line like `sk-abc=x` the parsed
                # "key" IS the secret. Unreachable for the dotenv today -- the
                # filter above admits only AGENT_TZ -- but the line number
                # already locates it, so the coupling need not be load bearing.
                die "$file: line $_lineno: malformed key" ;;
        esac

        # The value grammar, ported from compose-go in one pass rather than a
        # rule per review round -- measured against a real `docker compose
        # --env-file`, because Compose reads this same file and the two
        # disagreeing about it is the whole failure mode:
        #
        #   VAL # c        -> VAL          (inline comment starts at SPACE-hash)
        #   VAL# c         -> VAL# c       (no space: not a comment)
        #   a#b            -> a#b          (ditto)
        #   "VAL # c"      -> VAL # c      (quotes protect it)
        #   'VAL # c'      -> VAL # c
        #   "VAL" trailing -> VAL          (anything after the closing quote is dropped)
        #   VAL␠␠          -> VAL          (unquoted trailing space trimmed)
        #   "VAL␠␠"        -> VAL␠␠        (quoted trailing space kept)
        #
        # An unterminated quote is refused, because Compose refuses it -- and
        # accepting what Compose rejects is the dangerous direction: the
        # descriptor would resolve here and break the deploy that reads it.
        #
        # Single-quote literalness ('a\nb' stays literal) already agrees; that
        # was measured, not assumed.
        #
        # ONE rule is deliberately not ported: escape processing inside DOUBLE
        # quotes ("a\nb" -> a<newline>b). It is the only divergence left and the
        # only inert one -- every AGENT_* value is a zone, a path or an image
        # digest, so a value needing an escape is not one this parser has a use
        # for. Recorded rather than discovered, so a future need starts from
        # fact.
        case "$value" in
            \"*)
                _rest="${value#\"}"
                case "$_rest" in
                    *\"*) value="${_rest%%\"*}" ;;
                    *) die "$file: line $_lineno: unterminated quote in value for $key" ;;
                esac ;;
            "'"*)
                _rest="${value#\'}"
                case "$_rest" in
                    *"'"*) value="${_rest%%\'*}" ;;
                    *) die "$file: line $_lineno: unterminated quote in value for $key" ;;
                esac ;;
            *)
                case "$value" in *" #"*) value="${value%%" #"*}" ;; esac
                value="${value%"${value##*[![:space:]]}"}" ;;
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
        # -F, not a regex, and it is load-bearing rather than tidy. `grep -qw`
        # read $key as a PATTERN, so a descriptor could declare a key whose
        # bracket expression matched an allowlisted name as a character class --
        # `AGENT_T[$(...)Z]` matches AGENT_TZ -- and the name then reached
        # `printf -v`, where bash evaluates an array subscript arithmetically
        # and arithmetic performs command substitution. A registered repo's
        # agent.env could run host commands with the operator's credentials on
        # any `agent-mgr resolve`, defeating the read-never-execute property
        # this parser exists for.
        #
        # Not "defence in depth" -- a regex match against a fixed list was
        # always the wrong way to test set membership, exploit or no. It is not
        # independently observable from outside now that the shape check above
        # rejects every regex metacharacter, so no test can distinguish -F from
        # its absence; it is here because membership is a literal question, and
        # because loosening that check must not silently re-open the sink.
        # `--` for uniformity with the other guarded greps.
        if printf '%s' "$allow" | grep -Fqw -- "$key"; then
            # Recorded by the parser that accepted it, so require_own_home needs
            # no second opinion about the same file. Its raw
            # `grep '^[[:space:]]*AGENT_HOME='` could not see `export AGENT_HOME=`
            # or `AGENT_HOME = `, both of which this parser accepts -- so a
            # descriptor resolved fine and every direct-write command then
            # refused it as undeclared.
            [ "$key" = AGENT_HOME ] && AGENT_HOME_DECLARED=1
            # A key this tool CONSUMES must carry a value, unless empty IS its
            # value (AGENT_REPO_PATHS). Assigning empty to the rest is
            # indistinguishable from never declaring it, because they all reach
            # `${X:=default}` downstream: `AGENT_TZ=` in an instance's dotenv
            # overwrote the repo's zone with nothing, the convention default
            # filled in, and the container ran on a third clock neither file
            # named. Unowned keys are not this tool's business and go to the
            # hooks empty or not. The key is a validated identifier by here, so
            # the padded glob is an exact membership test.
            case " $AGENT_REPO_PATHS " in
                *" $key "*) ;;
                *) [ -n "$value" ] || die "$file: line $_lineno: empty value for $key" ;;
            esac
            printf -v "$key" '%s' "$value"
        elif [ "$collect" = "hooks" ]; then
            # An instance's own variables -- STR_VAULT and friends -- which its
            # compose override and its hooks are written against. Passed to the
            # hooks as an environment, never into this process.
            AGENT_HOOK_ENV+=("$key=$value")
        fi
    done < "$file"
}

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
    AGENT_HOME_DECLARED=0

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
    AGENT_HOOK_ENV=()
    parse_env_file "$descriptor" "$AGENT_KEYS" descriptor

    # Convention, applied only where the descriptor said nothing.
    AGENT_NAME="$name"
    AGENT_DIR="$dir"
    : "${AGENT_HOME:=$HOME/.hermes-$name}"
    # Canonicalised once, here, because two spellings of one directory defeat
    # every check downstream: they address the same home and compare unequal, so
    # the collision loop clears a copycat and restore overwrites the live
    # sibling. Resolved rather than collapsing slashes by hand, which left
    # `$HOME/foo/../.hermes` intact and evading the check. normalized_path, not
    # canonical_path: a home symlinked onto a bigger disk is ordinary, and the
    # shape rule below reads this value and must still see the declared name.
    #
    # Resolved into `home`, not into AGENT_HOME: the never-assign rule beside
    # normalized_path.
    local home
    home="$(normalized_path "$AGENT_HOME")" \
        || die "cannot resolve ${name}'s home ($AGENT_HOME)"
    AGENT_HOME="$home"
    : "${AGENT_CONTAINER:=hermes-$name}"
    : "${AGENT_PROJECT:=hermes-$name}"

    # The instance's own dotenv -- the file the operator already keeps per
    # person, mounted at /opt/data, holding its Plow token and Latch credential.
    # Read AFTER the home is known and BEFORE the default below, so precedence
    # is  this file > the shared descriptor > convention.
    #
    # This is what lets ONE repo serve several people, and it is deliberately
    # almost nothing. config.yaml interpolates ${VAR} from this same dotenv at
    # runtime, so a per-person model, locale or endpoint is already a line in
    # here and no business of agent-mgr's. AGENT_TZ is the sole exception:
    # Compose sets `TZ` into the container at render time, so the gateway never
    # sees it and cannot resolve it from the file the way it resolves the rest.
    #
    # AGENT_TZ alone, and that matters -- this file holds credentials. One
    # non-secret value enters agent-mgr's process; TZ still reaches the container
    # through `environment:`, so nothing here goes to Compose and the
    # no-credential-through-compose contract is untouched. Identity is derived
    # above, so a dotenv cannot move its own home.
    if [ -f "$AGENT_HOME/.env" ]; then
        parse_env_file "$AGENT_HOME/.env" AGENT_TZ dotenv
    fi

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
    for _path in $AGENT_REPO_PATHS; do
        printf -v "$_path" '%s' "${!_path-}"
        case "${!_path}" in
            ''|/*) ;;
            *) printf -v "$_path" '%s' "$dir/${!_path}" ;;
        esac
    done
    AGENT_DESCRIPTOR="$descriptor"

    HERMES_UID="$(id -u)"
    HERMES_GID="$(id -g)"

    # Word-split deliberately, like the scrub above: a hardcoded copy of these
    # ten names is a fourth place a new key has to be added, and the one place
    # where forgetting is silent -- the value resolves and prints, and only the
    # container comes up without it.
    # shellcheck disable=SC2086
    export $AGENT_KEYS AGENT_DESCRIPTOR HERMES_UID HERMES_GID
}

# No fetch through this tool may replace what the host built. This is one of the
# two doors: resolve-guard closes the other, where Compose fetches on its own
# under a pull_policy that is not `never` or `build`. Neither is sufficient
# alone, and two attempts to derive the guarantee from the image NAME were both
# wrong -- fetchability is not a property of the string.
#
# The SUBCOMMAND is $1, per this file's own rule: scanning the whole argv for it
# made `compose rowan exec hermes git pull` die about --ignore-buildable.
compose_fetch_is_safe() {
    local sub="${1:-}"
    # Every guard here reads the subcommand as $1, so a leading global would
    # shift it out from under all of them at once. Refused with the guards
    # rather than only at the passthrough, so the invariant travels with the
    # code that depends on it.
    case "$sub" in -*) return 1 ;; esac
    # `build --pull` is a BOOLEAN flag -- "always attempt to pull a newer
    # version of the base image" -- not the value-taking `--pull` that `up` and
    # `run` take. It re-pulls the FROM image and rebuilds, so the output is
    # still what this host built, and reading its next word as a policy refused
    # a safe command.
    case "$sub" in build) return 0 ;; esac
    # `pull` is refused outright, with no admitted form. The exemption that
    # kept `--ignore-buildable` open was wrong THREE ways in one review round,
    # each measured: presence is not the value, since a later occurrence
    # overrides an earlier one; past `--` the word is a service name; and
    # `pull --policy --ignore-buildable` has `--policy` swallow the word as its
    # value, so Compose never sees the flag at all and fetches.
    #
    # Each fix recognised one more spelling, which is re-implementing Compose's
    # flag parser -- and every miss fails OPEN, replacing a built image that
    # then runs with the agent's credentials. The property being asked about
    # is not in the argv anyway: whether a fetch can substitute anything
    # depends on whether the RESOLVED service is buildable, which resolve-guard
    # already reads. Nothing in the fleet calls this passthrough, and `up`
    # fetches under the file policy resolve-guard has checked, so refusing
    # deletes the parser rather than sharpening it.
    case "$sub" in pull) return 1 ;; esac
    # Scanned to the end, with no service boundary and no list of value-taking
    # flags. Locating the boundary needed that list to be COMPLETE, and an
    # entry missing from it truncated the scan and let a real fetch through --
    # a silent failure, on the one subcommand (`run`) that both stops and
    # fetches. Scanning everything cannot miss one.
    #
    # The cost is that a container command carrying a `--pull` whose next word
    # is not `never` or `build` gets refused -- including a bare boolean one,
    # as `docker build --pull -t x .` has. That is loud rather than silent,
    # and far rarer than a flag
    # this list had not heard of: the `git pull` false positive that prompted
    # the boundary came from matching the SUBCOMMAND anywhere in the argv,
    # which is keyed on $1 above and stays fixed.
    while [ $# -gt 0 ]; do
        case "$1" in
            # The SAME allowlist the file-level policy gets, because the CLI
            # flag overrides the file: `--pull missing` beats a safe
            # `pull_policy: never` and fetches when the local tag is absent,
            # which the marker test showed is a real substitution. Rejecting
            # only `always` left that door open.
            --pull=never|--pull=build) ;;
            --pull=*) return 1 ;;
            --pull)
                case "${2:-}" in never|build) ;; *) return 1 ;; esac ;;
        esac
        shift
    done
    return 0
}

# Called at the dispatch as well as here, the way the `run` entrypoint rule is
# pre-checked: `pull` has no accepted form, so an operator who types one must be
# told THAT, not sent through an identification whose remedies -- rename the
# descriptor, unregister the agent -- are doors that do not open for it.
require_fetch_is_safe() {
    compose_fetch_is_safe "$@" || die "refusing a fetch that could replace a built image. Here it is the COMMAND LINE: 'pull' has no accepted form -- use 'up', which fetches under the file's pull_policy that resolve-guard has already checked -- and '--pull' takes only 'never' or 'build' (except on 'build', where it is a boolean that re-pulls the base image and rebuilds), because the flag overrides whatever the file says -- editing pull_policy will not clear this one. (resolve-guard enforces the same pair on the file, which is the other door.) If that --pull belongs to a command you are running INSIDE the container, this scan cannot tell -- wrap it so the flag is not a word on this argv, e.g. exec ... sh -c 'docker build --pull ...'."
}

# Every Compose invocation goes through here so the file list, the override
# convention and the descriptor's env-file have exactly one definition.
compose() {
    # The fetch refusal is checked at the dispatch too, for the message. It is
    # ALSO here because every Compose invocation in this tool goes through this
    # function -- so the invariant travels with the code that depends on it,
    # rather than with the one entry point that happens to state it first.
    require_fetch_is_safe "$@"

    local files=(-f "$AGENT_MGR_ROOT/templates/compose.yml")
    [ -f "$AGENT_DIR/compose.override.yml" ] && files+=(-f "$AGENT_DIR/compose.override.yml")
    docker compose -p "$AGENT_PROJECT" "${files[@]}" --env-file "$AGENT_DESCRIPTOR" "$@"
}

# Compose subcommands that leave the LIVE container as it is. `exec` runs inside
# one, `run` starts a separate throwaway beside it, `cp`/`build`/`push`
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
COMPOSE_LEAVES_IT_RUNNING="logs ps config version top port images events ls exec run cp build push"

# The subset that needs NO identification, stated that way round for the same
# reason COMPOSE_LEAVES_IT_RUNNING is: a list of things that must be gated has
# to be complete to be correct, and the first attempt at this one was not --
# `events` fell in neither list and streamed the live project ungated, by
# omission rather than by decision.
#
# These touch no running container: `config`, `version`, `ls`, `images`,
# `build` and `push` are file and image operations, and `run` starts a
# throwaway beside the live one. `ps` is exempt because it reads names and
# status only, and the identification is ITSELF a `compose ps` -- gating it
# would issue a second identical call for no added information. Not because
# it would recurse: the check calls the `compose` shell function directly and
# never re-enters this dispatch, so gating `ps` later is possible if a reason
# appears.
#
# `pull` is named in NEITHER list. `compose` refuses it and the dispatch refuses
# it before classifying anything, so an entry in either is a contract for a
# command that cannot run -- and an exemption entry in particular would take
# effect SILENTLY on a future re-admission, skipping the ownership check by
# inheritance rather than by decision, which is the by-omission failure this
# list exists to prevent. Whoever re-admits it classifies it.
#
# Anything not named here is gated, heard of or not. Missing an entry costs a
# needless `compose ps`; missing one from a gate-these list skipped the check.
COMPOSE_NEEDS_NO_IDENTIFICATION="config version ls images build push run ps"

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
    local cid cids mounted self m
    # A compose that REFUSED to run is not "no container" -- conflating them is
    # exactly what reload-if-running's own comment rejects, and it would silently
    # disable this check on, say, a Compose too old for `--status`. The
    # subsequent `restart` does not use --status, so it would proceed.
    # `-a`, not --status running: a STOPPED sibling container still owns the
    # project name, and `up` is exactly the command that would adopt it. Reading
    # only running ones let a stopped sibling pass as absent, which is the
    # second-gateway case through a door the running check never saw.
    if ! cids="$(compose ps -a --quiet hermes)"; then
        die "refusing to touch the container under $AGENT_PROJECT -- docker could not say whether one exists"
    fi
    # Empty output is the ordinary first bring-up: nothing to misidentify.
    [ -n "$cids" ] || return 0
    # EVERY container under the project, not the first. `-a` includes the
    # one-off containers `compose run` leaves behind -- which this tool
    # deliberately supports -- so picking one and trusting it identified an
    # arbitrary member of the set and ignored the rest. Any foreign mount
    # refuses; this agent's own one-offs mount its own home and pass.
    # Once, above the loop, and refused with `|| die` rather than left to
    # `set -e` -- which is suspended for everything on the left of a `||`,
    # function bodies included, and lib/reload-if-running calls this whole path
    # that way. Resolved inline in the `if` below instead, a failed call
    # compared "" to "" and `continue`d past the refusal for a container
    # mounting a FOREIGN home.
    # Shadowed, deliberately: require_own_home resolves this same path with this
    # same helper on every route in, so this fires only when the SECOND call
    # fails where the first succeeded. That is also why it has no test -- a stub
    # matching this path trips require_own_home's refusal, one process earlier.
    self="$(canonical_path "$AGENT_HOME")" \
        || die "refusing to touch the container under $AGENT_PROJECT -- could not resolve $AGENT_HOME. Anything already written is written; re-run once that is fixed."
    for cid in $cids; do
    mounted="$(docker inspect --format \
        '{{range .Mounts}}{{if eq .Destination "/opt/data"}}{{.Source}}{{end}}{{end}}' \
        "$cid")" \
        || die "refusing to touch the container running as $AGENT_PROJECT -- docker could not say whose home it mounts"
    # Resolved before comparing, or this is a comparison between two spellings
    # again -- one of them from a source we do not control.
    if [ -n "$mounted" ]; then
        # Both sides end at realpath, so a match IS the same directory. Only
        # AGENT_HOME also arrives abspath'd, from load_agent, so a source
        # spelled with a `..` after a symlink can cost a false refusal and
        # nothing worse.
        #
        # `mounted` itself is never assigned from the substitution -- the
        # never-assign rule beside normalized_path -- which is what keeps
        # docker's raw .Source for this refusal and for the mismatch die below,
        # where an operator matches it against `docker inspect`.
        m="$(canonical_path "$mounted")" \
            || die "refusing to touch the container under $AGENT_PROJECT -- could not resolve the home it mounts ($mounted). Anything already written is written; re-run once that is fixed."
        [ "$m" = "$self" ] && continue
    fi
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
    done
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
# one. The flag must be the FIRST argument after `run`.
#
# Position, and exactly one position, because every looser rule needs to know
# something the argv cannot tell it. "Before the service" needs a complete list
# of value-taking flags, and a missing entry puts the boundary in the wrong
# place. "Before the first non-flag word" needs no list but cannot tell
# `--entrypoint` used as a flag from `-e --entrypoint`, where it is another
# flag's VALUE and overrides nothing. Both fail by ADMITTING an invocation that
# boots a second gateway, which is the one outcome this tool exists to prevent.
#
# First position is unambiguous, and it costs a caller only an argument order:
# `run --entrypoint bash --rm --no-deps -T hermes`. Nothing becomes impossible.
run_replaces_the_entrypoint() {
    shift  # the `run` subcommand itself
    # A non-empty value too: `--entrypoint=` and a bare `--entrypoint` with
    # nothing after it override with nothing, so s6 is still the entrypoint.
    case "${1:-}" in
        --entrypoint) [ -n "${2:-}" ] && return 0; return 2 ;;
        --entrypoint=?*) return 0 ;;
        --entrypoint=) return 2 ;;
    esac
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

# The value the gateway would load for KEY.
#
# ONE spelling, deliberately: `KEY=value` at column 0. This tool writes every
# DOMO_* and PLOW_CHAT_* line in an agent's dotenv -- set-latch here, activate
# through the pinned script, restore from the skeleton -- so the canonical form
# is the only one that gets produced, and owning it is what lets this be four
# lines instead of a second implementation of Hermes's dotenv grammar.
#
# Measured before it was narrowed, not assumed: across all three dotenvs on this
# host -- 23 keys, three different producers, including the rentals agent's
# hand-added HOSTEX_TOKEN and SEAM_API_KEY -- every declaration is already this
# form. No `export`, no indent, no quotes, no duplicates. The compatibility
# matrix that used to live here parsed spellings nothing on the fleet emits.
#
# A hand edit in some other spelling reads as absent, and that is the loud
# failure this repo prefers: check-latch says the key is empty and names
# `agent-mgr set-latch` as the fix, which then writes the canonical line.
#
# Last-wins falls out of assigning as it reads rather than stopping at a match,
# which is what the gateway does too (hermes_cli/config.py assigns into a dict).
dotenv_read() {
    # No readability guard here: load_agent parses this same file for AGENT_TZ
    # before any caller reaches this, and parse_env_file names an unreadable one
    # there. A second check could only fire if the mode changed mid-command,
    # which nothing here does -- and an unreachable guard that a test appears to
    # cover is worse than none, because it reads as protection.
    # The separator is required BEFORE the key test. Under -F= a line carrying
    # no `=` puts the whole line in $1, so a stray bare `DOMO_MCP_TOKEN` matched
    # the key and then substr returned that line as its own value -- non-empty,
    # so the guard passed and the relay got `Bearer DOMO_MCP_TOKEN`, answered
    # 401, and check-latch told the operator to revoke a live credential over a
    # malformed line. The parser this replaced skipped `=`-less lines outright.
    key="$1" awk -F= '
        index($0, "=") && $1 == ENVIRON["key"] { v = substr($0, index($0, "=") + 1) }
        END { gsub(/^[ \t]+|[ \t]+$/, "", v); printf "%s", v }' "$2"
}

# Does the INSTALLED config declare a latch server? The config is the
# declaration, not the dotenv: a leftover DOMO_* pair from an earlier experiment
# sits in a dotenv long after the config stopped declaring a latch, and keying
# off the credential then probes a relay for an agent that cannot reach it.
#
# Two callers now -- check-latch reports it, set-latch refuses on it -- so the
# awk lives here rather than being written twice with one of the copies drifting.
config_declares_latch() {
    awk '/^mcp_servers:/{m=1;next} /^[^[:space:]]/{m=0} m && $1=="latch:"{found=1} END{exit !found}' "$1"
}

# GET /v1/chats as this agent, printed as the raw JSON body. Two callers --
# `chats` renders it, `set-home` refuses a uid that is not in it.
#
# Asked from INSIDE the container for check-latch's reason: egress, DNS and CA
# config all differ between this shell and that network namespace, and a
# host-side answer is exactly the evidence entering the namespace was meant to
# stop accepting. The token goes in on stdin as a curl config, never argv,
# where `ps` on a shared host reads it from any account.
#
# Caller must have run load_agent and require_running.
plow_chats_json() {
    local env_file="$AGENT_HOME/.env" tok base out code
    [ -f "$env_file" ] || die "no $env_file -- run 'agent-mgr restore $AGENT_NAME' first"
    tok="$(dotenv_read PLOW_CHAT_TOKEN "$env_file")"
    [ -n "$tok" ] || die "PLOW_CHAT_TOKEN is empty in $env_file -- run 'agent-mgr activate $AGENT_NAME' first"
    base="$(dotenv_read PLOW_CHAT_BASE_URL "$env_file")"
    out="$(printf 'header = "Authorization: Bearer %s"\n' "$tok" \
        | compose exec -T hermes curl -sS --max-time 30 --config - \
          -w '\n%{http_code}' "${base:-https://api.plow.co}/v1/chats")" \
        || die "could not reach ${base:-https://api.plow.co} from inside the container"
    code="${out##*$'\n'}"
    [ "$code" = 200 ] || die "GET /v1/chats answered $code -- the token in $env_file may be dead (...${tok: -3}); if so, re-run 'agent-mgr activate $AGENT_NAME'"
    printf '%s' "${out%$'\n'*}"
}

# The pinned Plow Chat plugin, into this agent's home. A function rather than
# only a subcommand because `restore` sequences it too: one deploy entry point
# means one place that knows the order, and duplicating the install inline would
# make that two.
#
# Reloads nothing -- callers do, once, after everything boot-read has landed.
install_plow_plugin() {
    # Bound here, not interpolated inside the die below. Inside the string it is
    # expanded only on the failure branch -- so a caller that forgot it looked
    # fine until the day it mattered, and then bash aborted on the expansion
    # BEFORE die ran, replacing the gh-auth diagnosis with "1: the caller must
    # say what landed". A caller contract belongs at entry.
    local landed="${1:?install_plow_plugin: the caller must say what landed}"
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
        || die "could not install the Plow Chat plugin from plow-pbc/hermes-plow-chat at ${ref:0:7} -- is 'gh' installed and authenticated (gh auth status)? $landed"
}

# The pinned fleet google-workspace skill, into this agent's home. It replaces
# the image-bundled copy of the same name, which teaches a local-OAuth path no
# instance has; hermes's skills_sync keeps a diverged user copy, so this
# override is durable. Sequenced by restore before the skills.tsv replay so an
# instance row can still deliberately override it (last-writer-wins).
#
# Same shape as install_plow_plugin above, deliberately: one caller contract,
# one pin-file idiom, one env override for tests. Reloads nothing -- callers do.
install_fleet_skill() {
    local landed="${1:?install_fleet_skill: the caller must say what landed}"
    local ref
    ref="${AGENT_MGR_SKILL_REF:-$(tr -d '[:space:]' < "$AGENT_MGR_ROOT/runtime/google-workspace-skill.ref")}"
    # A SHA, never a branch: a branch would silently re-point every agent's
    # Google path on the next upstream push.
    [[ "$ref" =~ ^[0-9a-f]{40}$ ]] || die "the fleet-skill ref must be a 40-char SHA, got: $ref"
    "$AGENT_MGR_ROOT/lib/fetch-tree" "$AGENT_HOME" skills SKILL.md \
        plow-pbc/plow "$ref" productivity/google-workspace \
        cloud-agents/hermes/image/seed/skills/productivity/google-workspace \
        || die "could not install the google-workspace fleet skill from plow-pbc/plow at ${ref:0:7} -- is 'gh' installed and authenticated (gh auth status)? $landed"
}

# Does this agent's own skills.tsv pin productivity/google-workspace? That
# instance pin is authoritative over the fleet copy: restore skips the fleet
# install and install-skill refuses, so no window -- not even a failed
# replay's -- leaves the fleet copy sitting where the reviewed instance pin
# should be.
instance_owns_google_workspace() {
    [ -s "$AGENT_DIR/skills.tsv" ] && \
        awk -F'\t' '$3 == "productivity/google-workspace" {found=1} END {exit !found}' "$AGENT_DIR/skills.tsv"
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
    # Once, before any row is read, and unconditionally -- invariant across the
    # loop (the sibling load_agent below runs in a subshell), and this is the
    # fail-closed side, so a resolver that cannot run should stop every
    # direct-write command rather than only those with a sibling to compare.
    local other odir ohome o err why skipped=0 skipped_named= self
    self="$(canonical_path "$AGENT_HOME")" \
        || die "refusing to write to $AGENT_HOME -- could not resolve it"
    while IFS=$'\t' read -r other odir; do
        [ -n "$other" ] && [ "$other" != "$AGENT_NAME" ] || continue
        # `|| true` is load-bearing under set -e: a bare assignment carries the
        # substitution's status, and this is a while BODY rather than a
        # condition, so a sibling load_agent refuses -- a registry row whose dir
        # was moved, a repo with no agent.env -- would abort the caller. With
        # both streams already redirected the operator would get exit 1 and no
        # output, from one unrelated stale row, on every direct-write command.
        # ONE invocation, with the streams kept apart. stdout is discarded --
        # capturing it would concatenate whatever load_agent prints in front of
        # the home, which is a silent corruption of the value the comparison
        # below depends on -- and stderr goes to a file so the reason survives
        # for the refusal. The previous shape ran load_agent twice per row on
        # every direct-write command and threw the second result away for every
        # row that resolves, which is the common case.
        err="$(mktemp)"
        ohome="$( load_agent "$other" >/dev/null 2>"$err" && printf '%s' "$AGENT_HOME" )" || true
        why="$(cat "$err")"; rm -f "$err"
        if [ -z "$ohome" ]; then
            skipped=1
            # The REASON, not just the name. load_agent refuses a present,
            # healthy, running agent whose descriptor it cannot read just as
            # readily as one whose repo is gone, and telling that operator to
            # unregister a live agent is worse than saying nothing.
            echo "agent-mgr: could not resolve $other -- ${why#agent-mgr: }" >&2
            skipped_named="$other"
            continue
        fi
        # Compared RESOLVED, unlike the shape check above. Two questions, two
        # normalisations: the shape rule needs the home as declared, because a
        # home symlinked onto a bigger disk is ordinary and following it would
        # match neither accepted shape. This one asks "is it the same
        # directory", and two spellings reaching one directory through a symlink
        # is exactly the aliasing this loop exists to catch.
        # Into a local for the same reason `self` is: inline in this `&&` list a
        # failure for THIS path alone would compare unequal, skip the die, and
        # open the very collision the loop exists to close.
        o="$(canonical_path "$ohome")" \
            || die "refusing to write to $AGENT_HOME -- could not resolve ${other}'s home ($ohome)"
        [ "$o" = "$self" ] \
            && die "refusing to write to $AGENT_HOME -- $other is already registered there"
    done < <(registry_list)

    # Unconditional, and deliberately not narrowed. Three rounds tried to scope
    # this to homes that "could alias" and each proxy was wrong in a new
    # direction, because aliasing is a relation between TWO paths: no property
    # of this home proves anything about the home of a sibling we could not
    # resolve, and that sibling's home is exactly the information we are
    # missing. A proxy would need the fact whose absence triggered the check.
    #
    # So the choice is refuse or proceed, and this is the one check in this tool
    # whose job is to fail closed. The cost is availability on a stale row, paid
    # down by the reason and the remedy in the message rather than by guessing.
    [ "$skipped" -eq 0 ] \
        || die "refusing to write to $AGENT_HOME -- ${skipped_named} could not be resolved (reason above), so this tool cannot prove no one else claims that home. Fix the file named above if the agent is still there; 'agent-mgr unregister ${skipped_named}' only if it is gone."

    case "$AGENT_HOME" in
        *"/.hermes-$AGENT_NAME") return 0 ;;
        */.hermes)
            # The legacy shape, allowed only when the descriptor says so -- the
            # convention can never produce a bare `.hermes`, so this is always a
            # deliberate declaration, and the collision check above is what
            # stops a second agent from making the same one.
            [ "${AGENT_HOME_DECLARED:-0}" = 1 ] && return 0
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
    ( cd "$AGENT_DIR" && env ${AGENT_HOOK_ENV[@]+"${AGENT_HOOK_ENV[@]}"} "$AGENT_PRE_TRANSITION" ) \
        || die "${AGENT_NAME}'s pre-transition guard refused -- not transitioning the container"
}
