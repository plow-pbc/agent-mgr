# Running a Hermes agent with agent-mgr

Every agent on this host is one Hermes container talking to **Plow Chat** (its
phone line) and **Plow Latch** (a Mac it can drive). `agent-mgr` owns everything
those agents have in common; each agent owns only what makes it itself.

## Install

Clone and symlink: [the README's install block](../README.md#agent-mgr) — one
clone, one symlink. Kept there rather than copied here, because this block and
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
agent-mgr backup-homes ~/agent-backups
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
agent-mgr backup-homes /somewhere/not/this/disk
```

It globs `~/.hermes*` rather than reading the registry, so it does not depend on
a row being current and it catches a home whose agent is mid-migration. A home
declared *outside* `~/.hermes*` would not be archived, silently, while the run
still reported success on the others — nothing does that today. It skips
`logs/`, `cache/` and `lazy-packages/` — 1.5 GB of homes becomes ~440 MB — and
writes mode-0600 archives, because they hold credentials.

Nightly, with 14 days kept:

```sh
0 4 * * * ~/.local/bin/agent-mgr backup-homes ~/agent-backups && find ~/agent-backups -name '*.tar.gz' -mtime +14 -delete
```

### What an archive is worth

It is a **live copy**. `tar` reports *"file changed as we read it"* for any file
rewritten while it is being read — measured on a plain file, not only on a
database — and that status is accepted and the archive published, so such a file
can restore partially. Most of a home is quiescent between turns; the gateway's
SQLite session database is the one written continuously. Refusing that status
would fail the nightly every night, so it is a trade: for a consistent copy,
`agent-mgr down <name>` first.

Two further ways the archive *container* can go wrong, both accepted: a killed
run leaves a truncated archive, and two runs overlapping in time splice into one
file. Neither is repaired — names are date-stamped, so nothing overwrites a bad
archive and it stays the **newest** until retention prunes it at 14 days.

`gzip -t <archive>` tests the container and nothing else: a mid-rewrite archive
passes it cleanly. So check before restoring, and fall back to the previous
night's.

### Restoring one

**You move the existing home aside — but not until step 1 has run.** The recipe
deliberately does not move it for you: naming that copy is a decision, and three
attempts at automating it each produced a worse hazard than the last — a name
inside the nightly's `~/.hermes*` glob (so the cron archived a dead home as
live), a fixed name that moved the home *inside* the previous copy on a re-run,
and a unique name the operator could not refer to from a later shell.

Where it goes, decided before you run anything: **a name that does not match `~/.hermes*`, on the
same disk, not `/tmp`.** That copy is the only thing holding state newer than the
archive — everything written since 04:00, the `-wal` and `-shm` sidecars, any
turn the agent took this morning — so a `/tmp` that is reaped on reboot loses
it. Keep it until you have watched the restored agent actually run, then delete
it.

Step 1 — verify the archive, stop the agent, resolve the home:

```sh
a=~/agent-backups/hermes-rowan-20260826.tar.gz \
  && gzip -t "$a" \
  && agent-mgr down rowan \
  && home=$(readlink -f "$(agent-mgr resolve rowan | sed -n 's/^AGENT_HOME=//p')") \
  && echo "move $home aside now — same disk, not /tmp, and a name that does not match ~/.hermes* (a sibling like .hermes-rowan.old matches it); keep it until the restored agent is verified running. Then run step 2."
```

Then move it. Step 2, **in the same shell** — `$home` and `$a` come from step 1:

```sh
mkdir "$home" \
  && tar -C "$home" -xzf "$a" \
  && agent-mgr restore rowan \
  && agent-mgr up rowan
```

`mkdir`, not `mkdir -p`: it fails with `File exists` if the home is still there,
which is the emptiness check for free. `tar -xzf` overlays rather than replaces,
so unpacking over a live home would leave every file the archive does not
contain — including the `-wal` and `-shm` sidecars of the very session databases
you are rolling back.

**The archive is bound once** because the prose above tells you to change it —
edit only the `tar` line and `gzip -t` validates a different file, which stops
nothing.

One `&&` chain, not `set -e`: this is a paste-into-your-shell block, and
`errexit` in an interactive shell closes the session on the first failure — over
SSH, taking the error you need to read with it. It starts at `gzip -t` because a
bad archive has to stop the restore before anything else runs, and `down` is in
the chain because the veto can legitimately refuse: it runs the agent's
`AGENT_PRE_TRANSITION` hook, and the rentals agent declines to stop mid-ingest
by design.

The archive is contents-rooted (`./` entries), which is why it needs `-C` and
cannot splat into `$HOME`; `logs/`, `cache/` and `lazy-packages/` are excluded
from it and are not recreated, which is expected rather than a truncated
archive.

`readlink -f` in the binding is what makes a **symlinked home** work — supported,
though nothing uses it today. It resolves `$home` to the real directory, so you
move and recreate the *target* and the link keeps pointing at it. Without it you
would move the link, the restore would land on the wrong volume, and step 2's
`mkdir` would fail on the name regardless, since `mkdir` does not follow a
trailing symlink.



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
