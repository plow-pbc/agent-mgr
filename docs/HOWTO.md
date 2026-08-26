# Running a Hermes agent with agent-mgr

Every agent on this host is one Hermes container talking to **Plow Chat** (its
phone line) and **Plow Latch** (a Mac it can drive). `agent-mgr` owns everything
those agents have in common; each agent owns only what makes it itself.

## Install

```sh
git clone git@github.com:plow-pbc/agent-mgr.git ~/services/agent-mgr
ln -s ~/services/agent-mgr/agent-mgr ~/.local/bin/agent-mgr
agent-mgr ls
```

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

To let it drive a Mac, put the pair from that Mac in `~/.hermes-rowan/.env`:

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
agent-mgr backup rowan / backup --all
```

Both `check-` commands ask **from inside the container**, because the container
is what has to reach `api.plow.co`. Egress, DNS and CA config all differ between
your shell and that network namespace, and every one of those failures is
invisible to a host-side probe. There is deliberately no host fallback.

## Backing up a home

The repo is the image and the home is the volume. `restore` rebuilds the image
half from git any time you like — `config.yaml`, the plugin, the pinned skills.
Nothing rebuilds the volume half: `auth.json`, the dotenv, the sessions, the
memories and the kanban exist in exactly one place.

```sh
export AGENT_MGR_BACKUP_DIR=/somewhere/not/this/disk
agent-mgr backup --all              # one archive per registered agent
agent-mgr backup rowan --keep 30    # default retention is 14 days
```

`AGENT_MGR_BACKUP_DIR` has no default on purpose. A backup written somewhere
nobody named lands on the same disk as the thing it backs up, which is not a
backup — it is a second copy of the same failure.

`logs/`, `cache/` and `lazy-packages/` are excluded. They are most of the bytes
and none of the value: a home restored without them is the same agent on its
next boot, and an archive that carries them is one nobody keeps enough copies
of.

`--all` keeps going past a failure and then exits non-zero naming every agent it
could not archive. Aborting on the first bad row would leave the healthy agents
unbacked-up too, and the whole point of `--all` is that a row missed is an agent
with no copy at all.

**What the archive does and does not guarantee.** It is taken from a *running*
agent, so it is crash-consistent, not transaction-consistent. The irreplaceable
half — `auth.json`, the dotenv, `SOUL.md`, memories, kanban — are plain files
and come across whole. The gateway's SQLite session database is the exception:
its main file and its `-wal` are read at different instants, so a restored copy
may need SQLite's own recovery, and in the worst case loses the tail of the
session history. That is the deliberate trade — a nightly that stopped the
rentals gateway to get a clean read would cost more than the session tail is
worth. For a consistent copy, `agent-mgr down <name>` first and back up then.

`tar` exits 1 with *"file changed as we read it"* whenever a running gateway
writes mid-read, which is most nights; the archive it produced is complete and
`backup` treats that as success. Status 2 — an unstattable path, an unwritable
destination — is a real failure and fails the command. (`--warning=no-file-changed`
would only hide the message; the status stays 1.)

To restore one, stop the agent first — unpacking under a live gateway gives two
writers to one session database.

**If the home was a symlink, recreate the link before you do anything else.**
`agent-mgr resolve` reports `AGENT_HOME` as it was *declared* — `load_agent`
normalises rather than canonicalises, so you get the link path, never its
target.

Four states at the link path. Only one means "carry on":

Which of the four you have, before anything is bound or created — the literal
path, because `$home` is not set until the restore block further down:

```sh
ls -ld ~/.hermes-rowan   # prints the link and its target, without following it
[ -e ~/.hermes-rowan ] && echo "target reachable" || echo "no target: dangling, or nothing there"
```

`ls -ld` distinguishes a symlink from a plain directory and shows where it
points; the `-e` test then separates a **live** symlink from a **dangling** one,
because `-e` dereferences. Together they name the row.

| what is there | what it tells you | what to do |
|---|---|---|
| a live symlink | link and target both fine | nothing — skip to the restore block |
| a dangling symlink | the link survived, its target did not | recreate the target directory; leave the link |
| nothing at all | the link is gone; this says **nothing** about the target | look for the target first, recreate whichever is missing, then the link |
| a plain directory | **undecidable from here** — a previous pass may have run `mkdir -p` on a vanished link, or this home was never a symlink and that directory *is* the live agent | look inside it *before* moving anything |

Getting this wrong is silent. A `mkdir -p` on a vanished link path exits 0 and
creates a plain directory where the link was; the restore then lands on the
wrong volume with no error, and the agent comes up on a home it was never
configured with.

Recreating link and target, once you know you need to (GNU):

```sh
mkdir -p /big/disk/rowan
ln -sT /big/disk/rowan ~/.hermes-rowan
```

`ln -sT`, not a bare `ln -s`: onto a path that is already a plain directory a
bare `ln -s` treats it as a *destination directory*, silently creating
`~/.hermes-rowan/rowan` and exiting 0. `-n` does **not** help — measured; it
only covers a destination that is a symlink to a directory, not a real one.
A bare `mv` absorbs the same way: `mv ~/.hermes-rowan ~/.hermes-rowan.bak` onto
an existing `.bak` nests it as `.bak/.hermes-rowan` and exits 0. GNU `mv -T`
does refuse that — measured: onto a non-empty directory it fails with
`Directory not empty`. Note it does **not** refuse an *empty* one, which it
replaces silently, so `-T` narrows the hazard rather than removing it. Neither
`-T` exists on macOS at all.

So the flags help on GNU and run out on macOS, and even on GNU `mv -T` leaves
the empty-directory case open — which is why the table above is a decision you
make by looking, rather than a guard this document pretends to enforce.

Do not delete what is at that path. It is equally the shape of a home that was
never a symlink at all — in which case that directory *is* the live agent, and
the tarball is the only remaining copy, if one was ever taken.

```sh
agent-mgr down rowan
home=$(agent-mgr resolve rowan | sed -n 's/^AGENT_HOME=//p')
mkdir -p "$home"        # a no-op if the home is a live symlink — see the note above
tar -C "$home" -xzf /path/to/rowan-20260826.tar.gz
agent-mgr restore rowan # repo-owned config, plugin and skills win
agent-mgr up rowan
```

The `mkdir` is not belt-and-braces: `tar -C` on a missing directory exits 2
before extracting anything, and a home that is *gone* is the scenario this
command exists for. Reading the path from `resolve` rather than typing
`~/.hermes-rowan` covers the other case — a home that simply lives somewhere
else. The `restore` afterwards is what makes the archive's copy of `config.yaml`
and the installed plugin lose to whatever the repo says today: those are the
reproducible half, and the archive's copy is as old as the archive.

The archive is **contents-rooted** — it holds `./` entries, not a
`.hermes-rowan/` prefix — so it unpacks into a directory you name rather than
splatting a name into `$HOME`, and the `-C` above is what makes that safe. That
falls out of archiving from *inside* the home, which is what makes a symlinked
home back up at all: archiving from the parent stores the symlink as a single
symlink entry and exits 0, having captured no credentials, sessions or memories.

Retention prunes by the date in the archive's **name**, not by mtime, and only
names this command itself wrote. The destination is a directory you chose and
may hold anything else; and mtime is reset by the `rsync` or `cp` that carries a
backup directory off this host, which would leave retention silently never
firing on exactly the copies that matter most.

## Two layers: where does my code go?

The contract, the test and a worked example live in the *Two layers, and what
belongs in each* section of the [README](../README.md); the file table is in
the section after it, *What belongs in an agent's repo*. Ask: **would a
second agent want this?** No means it belongs in this agent's own repo,
whatever it is — identity, config, its skill, its scripts.

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
that is the normal path. After bumping `runtime/plow-chat-plugin.ref` alone,
`agent-mgr install-plugin <name>` does just that step — useful when an agent's
restore hook is expensive (the rentals agent re-seeds a vault and rebuilds its
property hubs) and nothing else changed.
