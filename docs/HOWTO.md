# Running a Hermes agent with agent-mgr

Every agent on this host is one Hermes container talking to **Plow Chat** (its
phone line) and **Plow Latch** (a Mac it can drive). `agent-mgr` owns everything
those agents have in common; each agent owns only what makes it itself.

## Install

Clone and symlink: [the README's install block](../README.md#agent-mgr) — one
clone, two symlinks. Kept there rather than copied here, because this block and
the README's had already drifted apart on `mkdir -p` and `ln -sf`. Then:

```sh
agent-mgr ls
```

Needs **`gh`, authenticated** (`gh auth status`). `restore` installs the Plow
Chat plugin through the same `gh api` snapshot that installs a skill, so every
agent needs it, not only one that ships a `skills.tsv`.

The registry lives at `~/.config/agent-mgr/agents` and maps a name to an
agent repo, so every command works from any directory.

## Stand up a new agent

```sh
agent-mgr new rowan ~/services/rowans-life-hermes-agent
agent-mgr restore rowan
agent-mgr activate rowan      # prints a code -- text it from THAT agent's phone
agent-mgr up rowan
agent-mgr sign-in rowan       # device-code OAuth; hand the URL to whoever owns it
```

`activate` is the step only a person can finish. `POST /v1/auth/activate` carries
no credential — the account binding is *whichever phone texts the code back*. So
the code must be sent from the handset that should own the agent. A code texted
by the wrong person binds the agent to the wrong account, and it is a one-time
spend.

To let it drive a Mac, put the pair from that Mac in that instance's dotenv —
`$AGENT_HOME/.env`, which `agent-mgr resolve rowan` prints:

```sh
DOMO_DEVICE_UID=dev_...
DOMO_MCP_TOKEN=...
agent-mgr check-latch rowan   # "latch reachable ... (HTTP 200)"
```

Minting them needs the `relay:device` scope, which only the Mac running Latch
holds. **`DOMO_DEVICE_UID` decides which Mac an agent can drive** — Latch
sandboxes and asks per action, but a credential minted against your Mac lets
that agent drive *your* machine. Mint each agent's against the Mac it should
have.

**An agent that drives no Mac deletes the `latch:` block from its
`config.yaml`.** The config is the declaration — `check-latch` reads it, not the
dotenv — so a declared latch with blank `DOMO_*` is a broken agent, not an
unconfigured one, and it is reported as such. With the block gone,
`check-latch` says "no latch configured" and exits clean.

## Day to day

```sh
agent-mgr ls
agent-mgr up rowan / down rowan / restart rowan / logs rowan
agent-mgr agent rowan "what's on today?"
agent-mgr check-latch rowan
agent-mgr check-connectors rowan
backup-homes ~/agent-backups
```

Both `check-` commands ask **from inside the container**, because the container
is what has to reach `api.plow.co`. Egress, DNS and CA config all differ between
your shell and that network namespace, and every one of those failures is
invisible to a host-side probe. There is deliberately no host fallback.

## Backing up the homes

The repo is the image and the home is the volume. `restore` rebuilds the image
half from git any time; nothing rebuilds the volume half — `auth.json`, the
dotenv, the sessions, the memories, the kanban.

```sh
backup-homes /somewhere/not/this/disk
```

It globs `~/.hermes*` rather than reading the registry, so it does not depend on
a row being current and it catches a home whose agent is mid-migration. It skips
`logs/`, `cache/` and `lazy-packages/` — 1.5 GB of homes becomes ~440 MB — and
writes mode-0600 archives, because they hold credentials.

Nightly, with 14 days kept:

```sh
0 4 * * * ~/.local/bin/backup-homes ~/agent-backups && find ~/agent-backups -name '*.tar.gz' -mtime +14 -delete
```

To restore one, stop the agent first — two writers to one session database
otherwise — and unpack into an **empty** directory: `tar -xzf` overlays rather
than replaces, so restoring over a live home leaves every file the archive does
not contain, including the `-wal` and `-shm` sidecars of the very session
databases you are rolling back. That is a mixture of two points in time,
reported as a success.

```sh
set -e   # every step below is a precondition of the next

agent-mgr down rowan                   # can be VETOED — str refuses mid-ingest
home=$(agent-mgr resolve rowan | sed -n 's/^AGENT_HOME=//p')
real=$(readlink -f "$home")            # the actual directory, symlink or not
b=$(basename "$real")
aside="$(dirname "$real")/restoring-${b#.}-$(date -u +%Y%m%d%H%M%S)"
mv "$real" "$aside"
mkdir -p "$real"
tar -C "$real" -xzf ~/agent-backups/hermes-rowan-20260826.tar.gz
agent-mgr restore rowan                # repo-owned config, plugin and skills win
agent-mgr up rowan
```

Three things that recipe is doing on purpose:

- **`readlink -f`**, then move and recreate the *target*. A home symlinked onto
  a bigger disk is supported, and moving `$home` itself would move the **link** —
  the next `mkdir -p` would then make a plain directory on the root disk, the
  restore would land on the wrong volume, and the real data would be orphaned at
  the old target. Resolving first makes the symlinked and plain cases identical.
- **`set -e` over the whole block, not just an `&&` chain from `mv`.** The
  precondition is `down`, and it can legitimately fail: it goes through
  `compose_transition`, which runs `resolve-guard` and the agent's
  `AGENT_PRE_TRANSITION` veto — the rentals agent refuses to stop mid-ingest by
  design. On a vetoed `down` an unguarded recipe renames the home out from under
  a *running* container (the rename succeeds; the gateway keeps writing to the
  moved inode), extracts into a fresh `$real`, and brings the agent up on it —
  the live writes land in the set-aside and the restored copy goes live. A
  failed `mv` has the same shape one step later: `mkdir -p` succeeds trivially
  and `tar` overlays the live home. A sibling because `dirname "$real"` is the same filesystem, so the
  move is a rename; parking it under `$HOME` instead would make it a
  cross-device *copy* of the whole home onto the root disk, which is exactly the
  disk that was too small in the symlinked case.
- **`restoring-${b#.}-<stamp>`.** The dot is stripped so a plain `ls` shows it —
  `backup-homes` made the same call for the same reason, and a hidden
  credential-bearing copy is one nobody remembers to delete. The prefix keeps it
  out of the `~/.hermes*` glob, so the nightly does not start archiving a dead
  home as though it were live. The timestamp means a second restore attempt
  cannot move the new home *inside* the first set-aside. Delete it once the
  restore is verified.
- The archive is **contents-rooted** (`./` entries), which is why it needs a
  named target and cannot splat into `$HOME`. `logs/`, `cache/` and
  `lazy-packages/` are excluded from it and are not recreated; the agent
  rebuilds them.

## Two layers: where does my code go?

The contract, the test and a worked example live in the *Two layers, and what
belongs in each* section of the [README](../README.md); the file table is in
the section after it, *What belongs in an agent's repo*. Ask: **would a
second agent want this?** No means it belongs in this agent's own repo,
whatever it is — identity, config, its skill, its scripts.

One case splits further. When several instances are registered against one
checkout, a value belonging to a *person* rather than to the agent cannot live
in `agent.env` either, because every instance reads it. That goes in the
instance's own `$AGENT_HOME/.env` — `~/.hermes-<name>` by convention, but
whatever that instance resolved, which `agent-mgr resolve <name>` prints. See
*One repo, several people* in the [README](../README.md).

## Adding a shared skill

For a skill **another agent also wants**. One agent's own skill is just its
code — it lives in that agent's repo and needs no pin, because there is no
second copy to keep in step.

```sh
agent-mgr add-skill rowan plow-pbc/hermes-plow-chat --dest plow-connectors \
    --src ref/hermes-skill/plow-connectors
```

The pin is recorded in the agent repo's `skills.tsv`, so what an agent runs is
reviewable beside its config. Refs are always 40-char SHAs — a branch would
silently re-point a running agent on the next upstream push.

**One route, now.** A skill used to be able to reach an agent two ways: this
one, pinned into the container by SHA, and ClawHub installing the whole bundle
onto the Mac by semver. Only the first was ever pinned here, so the two could
skew — the container reading instructions the Mac's code did not implement.
ClawHub is no longer a delivery path (`property-hunt`'s listing is published
and unsupported), so that skew class is gone and there is nothing to compare.
What can still go stale is the pin itself, against fixes upstream has landed
since.

## What an agent's repo contains

The file list, and why each one is or is not there, is the *What belongs in an
agent's repo* section of the [README](../README.md). What only matters once you
are editing one:

`agent-mgr new` writes **two** files, `agent.env` and `config.yaml`.
`skills.tsv` appears the first time you `add-skill`, and `compose.override.yml`
only if you write one -- which you need when the agent has a derived image or
extra mounts, and there is no template for it. Every key in `agent.env` is an
override, so the file may be empty.

Relative paths do **not** work in `compose.override.yml`: Compose resolves them
against `agent-mgr`'s directory, not the agent's. Name paths through a variable
set in `agent.env`.

A `build:` must carry `pull_policy: never` (or `build`) beside it. Without one
Compose **pulls** the tag when it is absent locally -- the default and `missing`
both do -- so a registry image can land on top of what this host built and then
run with the agent's credentials. `resolve-guard` wants either that line or a
digest `image:`, and until it has one it refuses every command that resolves
Compose for this agent -- all of them but the registry bookkeeping (`ls`, `register`,
`unregister`, `new`, `resolve`). `activate` is the one that neither refuses nor
works: it reaches the guard through `reload-if-running`, whose refusal it
deliberately swallows so a spent activation is never re-spent, so it writes the
credential, skips the reload and tells you to `restart` -- which then refuses.

A container already up keeps running and answering its chat traffic: the guard
stops nothing. What you lose is the agent-mgr surface over it -- you cannot
prompt it, sign it in, probe it, read its logs, restart it or deploy to it until
the line is there.

```yaml
services:
  hermes:
    build: { context: "${AGENT_BUILD_CONTEXT}" }
    image: my-agent:local
    pull_policy: never
```

## Running a one-off container

```sh
agent-mgr compose <name> run --entrypoint bash --rm --no-deps -T hermes -c '...'
```

**`--entrypoint` must be the first argument after `run`**, and it must have a
value. Without a replaced entrypoint the image's s6 boots a gateway beside the
live one; with it, s6 never starts.

First position rather than merely "before the service", because every looser
rule needs something the argv cannot supply — locating the service needs a
complete list of which flags take values, and a missing entry silently puts the
boundary in the wrong place. Getting the order wrong is refused by name, so the
fix is visible; the alternative fails by letting a second gateway through.

## Why `agent` uses `exec`

Because the image's s6 entrypoint starts a gateway whatever command you pass it,
so `docker compose run` boots a **second** one against the same `/opt/data`.
Measured cost and the guarding test:
the *Why `agent` uses `exec`* section of the [README](../README.md).

## When something is wrong

| symptom | look at |
|---|---|
| `refusing to act: compose resolved ...` | the descriptor or override disagrees with the agent you named — this is the guard working |
| `HERMES_UID ... must be set` | you ran `docker compose` directly; go through `agent-mgr` |
| `... builds its image but its pull_policy is ...` | a service that builds must set `pull_policy: never` (or `build`). The default and `missing` both **pull** when the local tag is absent, replacing what this host built |
| `refusing a fetch that could replace a built image` | `pull` has no accepted form — use `up`, which fetches under the file's `pull_policy` that `resolve-guard` checks. A `--pull` takes only `never` or `build`, except on `build`, where it is a boolean that re-pulls the base image and rebuilds |
| `refusing 'compose run'...` | `--entrypoint` must be the **first** argument after `run` and carry a non-empty value — see § Running a one-off container |
| `... is REVOKED` | mint a fresh Latch credential from the Mac |
| `no answer from api.plow.co` | the credential was **not** tested; this is a network fault, not a bad token |
| a shared skill behaves oddly | compare the SHA in `skills.tsv` against what upstream has since fixed |

## Bumping the plugin pin

`agent-mgr restore <name>` installs the plugin as part of the whole deploy, and
that is the normal path.

**There are two SHA pins and only one of them may move.** Before bumping either,
read the *What this builds on* section of the [README](../README.md), which owns
that rule — restating it here is how this page came to state the opposite of the
code for a commit.

After bumping `runtime/plow-chat-plugin.ref` alone,
`agent-mgr install-plugin <name>` does just that step — useful when an agent's
restore hook is expensive (the rentals agent re-seeds a vault and rebuilds its
property hubs) and nothing else changed.
