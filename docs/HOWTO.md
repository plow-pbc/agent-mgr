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
agent-mgr new errands ~/services/errands-hermes-agent
agent-mgr restore errands
agent-mgr activate errands    # prints a code -- text it from THAT agent's phone
agent-mgr up errands
agent-mgr cron-sync errands   # only if its agent.env names a cron spec
agent-mgr sign-in errands     # device-code OAuth; hand the URL to whoever owns it
```

`activate` is the step only a person can finish. `POST /v1/auth/activate` carries
no credential — the account binding is *whichever phone texts the code back*. So
the code must be sent from the handset that should own the agent. A code texted
by the wrong person binds the agent to the wrong account, and it is a one-time
spend.

To let it drive a Mac, mint the pair on that Mac and hand it to `set-latch`,
which writes it into that instance's own dotenv — `$AGENT_HOME/.env`, the one
`agent-mgr resolve errands` prints, not the conventional spelling:

```sh
agent-mgr set-latch errands     # prompts for DOMO_DEVICE_UID, then DOMO_MCP_TOKEN
agent-mgr check-latch errands   # "latch reachable ... (HTTP 200)"
```

Both are read on **stdin**, never argv, and the token does not echo — a flag
would put a live relay credential in the process table of a shared host, where
`ps` reads it from any account, which is the same reason `check-latch` hands it
to curl as a config on stdin. `set-latch` carries every other key in the dotenv
through untouched, so an agent that keeps a PMS token or a lock API key beside
them keeps them.

Minting the pair needs the `relay:device` scope, which only the Mac running
Latch holds — so this is the one credential `agent-mgr` cannot fetch for you.
**Mint each agent's pair on the Mac it should drive.** The relay refuses a
`DOMO_DEVICE_UID` that is not the calling credential's own account, so the uid
is inert on its own and the *token* is what binds — which is why reusing
another person's pair, rather than reusing their uid, is what would point this
agent at their machine.

**An agent that drives no Mac deletes the `latch:` block from its
`config.yaml`.** The config is the declaration — `check-latch` reads it, not the
dotenv — so a declared latch with blank `DOMO_*` is a broken agent, not an
unconfigured one, and it is reported as such. With the block gone,
`check-latch` says "no latch configured" and exits clean.

## Onboarding someone who is not you

An agent for another person is the same deployment with one difference that
changes the procedure: **three of its steps happen on their devices, and you
cannot do any of them.** The account binding, the model credential and the Latch
credential are each held by hardware you do not have.

It is `register`, not `new` — the repo already exists, and a second row against
it is the whole mechanism (*One repo, several people* in the
[README](../README.md)):

```sh
agent-mgr register bob ~/services/shared-hermes-agent
agent-mgr restore bob
agent-mgr resolve bob        # prints AGENT_HOME -- bob's dotenv is the .env in it
```

Put `AGENT_TZ=America/Chicago` in that file, on its own line, then:

```sh
agent-mgr up bob
```

**The zone goes after `restore` and before `up`.** *Where a per-person value
goes* in the [README](../README.md) owns why the zone is special, and carries a
ready-made append recipe if you would rather not open an editor.

Both bounds have a reason, and only one of them is quiet. Before `restore`
there is no home to write into — `register` only adds a registry row, and the
home is deliberately created by `restore` — so an early attempt fails in front
of you. (Do not get around that by making the directory yourself: `restore`
writes the dotenv `0600` and only when it is absent, so a file you create first
keeps your umask — 644 on a stock host — for a file that ends up holding the
chat token.) After `up` is the bound that costs you silently: the zone reaches
the container when the container is **created**, so changing it later needs
`agent-mgr restart` or another `agent-mgr up`; both recreate the container from
the current descriptor and shared Compose template.

Check it before moving on — `agent-mgr resolve bob` reads that dotenv back, so
it prints your zone if the edit landed and the fleet default if it did not.

`up` before the codes, not after: `sign-in` runs `hermes auth add` **inside the
container**, so it refuses until one is running. `activate` does not care — it
writes to the home and reloads only if something is up.

Then a strictly sequential exchange — each step finishes before the next starts,
because two of them block:

| | who | what |
|---|---|---|
| 1 | you | `agent-mgr activate bob` — prints `Text Plow Activate: <code>` and the number, then **polls until it arrives** |
| 2 | **them** | text that code **from the handset that should own the agent** |
| 3 | you | `agent-mgr sign-in bob` — prints a device-code URL and a code, then **waits on the browser** |
| 4 | **them** | open the URL in *their* browser, enter the code |
| 5 | **them** | Plow Latch → Connect a client → mint an agent credential for this agent |
| 6 | you | put the pair in that same dotenv — the one `agent-mgr resolve bob` names — then `check-latch bob` and `restart bob` |
| 7 | you | `agent-mgr cron-sync bob` — only if the repo's `agent.env` names a cron spec; the gateway it needs is already up |
| 8 | **them** | reply to the agent's 👋 from that handset — it runs setup, brings the Pi up through Latch on their Mac (same LAN) and writes the wall's token to it (no Mac → texts them the lines to type) (`life-assistant-hermes-agent` README § Bring-up) |

**Neither code can be sent ahead.** `activate` does not return when it prints
the code; it polls `/v1/auth/activate/redeem` until the text arrives, and the
credential it writes is what the gateway reload at the end of it loads. `sign-in`
likewise holds a `compose exec` open until the browser step completes. So this
is one conversation, not a batch — and both halves need the person present, the
first at their handset and the second at a browser.

**Nothing else touches this agent while either one is waiting.** `sign-in`'s
session lives inside the container, and anything that restarts it drops that
session — `activate` and `restore` both reload a running gateway, and `restart`
is itself. The trap is `activate`'s own failure message, which tells you to run
`agent-mgr restart`: do that *before* starting `sign-in`, never during. If a
restart lands anyway, re-run `sign-in` — it costs nothing, unlike the step above
it.

**Start only when they are ready.** The activation code carries a server-side
TTL, and `activate`'s poll window is sized to match it — long enough to be
comfortable, short enough that minting it the night before does not work. It is
also a **one-time spend**: mint it while they are away and you cannot mint it
again.

**Step 2 is the account boundary, and the handset decides it.** `POST
/v1/auth/activate` carries no credential; the binding is whoever texts the code
back. A code texted by the wrong person binds the agent to the wrong account,
one time, permanently.

Steps 5 and 6 assume **their** Mac is already running Plow Latch, which is a
prerequisite you cannot satisfy for them. Confirm it before step 1 — not for
correctness, since 5 and 6 are free to run late, but for their calendar. A
missing Latch found at step 5 does not end when they install it: step 5 still
has to run, which is a second sitting with them — 6 is yours and follows. And
step 1 cannot be un-started, so the agent is live and Mac-less until 6 lands.

**The pair in step 5 is copy-once by design.** Latch drops it from memory once
they confirm they have saved it, which relaying it through a chat window
defeats — it lands in two message stores and both their backups, and that token
drives their Mac. Nothing here prevents that, and it is the ordinary route
today; just treat it as disclosed and re-mint from Latch once the agent is up.
Re-minting is free. The exposure is not.

**Tell them where their credentials live**, before step 2 rather than after:
their Plow token, and through it their mailbox, sit in that dotenv on this
host — readable by whoever runs `agent-mgr`, which is not them.

`check-latch` proves the Mac answered and `sign-in` proves the credential
minted. Nothing proves the person understood what they authorised, so that part
is a conversation, not a command.

## Day to day

```sh
agent-mgr ls
agent-mgr up errands / down errands / restart errands / logs errands
agent-mgr agent errands "what's on today?"
agent-mgr check-latch errands
agent-mgr check-connectors errands
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
`logs/`, `cache/` and `lazy-packages/` — 1.5 GB of homes becomes ~440 MB.

Each run gets its own directory under `backup-homes/`, named for the UTC second and the pid:

```
~/agent-backups/backup-homes/20260827T040112Z-4171/hermes-errands.tar.gz
```

That directory, its `backup-homes/` parent and the archives inside are all
closed to other accounts, because they hold `auth.json` and the dotenv.

**The destination must be a directory you own that nobody else can write**, and
the command refuses one that is not. It may be a symlink onto a bigger disk;
the check follows it to the target.

```sh
mkdir -p ~/agent-backups && chmod 700 ~/agent-backups
```

The `chmod` is the load-bearing half: `mkdir -m 700` sets a mode only on
directories it *creates*, so on an existing `~/agent-backups` it exits 0 and
changes nothing — and a default umask of 002 makes a plain `mkdir` produce 0775.

Nightly, with 14 days kept:

```sh
0 4 * * * { ~/.local/bin/agent-mgr backup-homes ~/agent-backups && ~/.local/bin/agent-mgr prune-backups ~/agent-backups 14 ; } >> ~/backup-homes.log 2>&1
```

Runs live in a `backup-homes/` child of your destination, and that is what keeps
retention away from your own files: `prune-backups` deletes directories *inside*
that child and nothing outside it, so what protects a `photos/` you keep beside
the backups is the layout rather than a pattern that has to be right. A run
written before the child existed sits directly in the destination and is never
pruned — let the first run create `backup-homes/`, then move any you have into
it, or delete them. Creating that directory yourself is refused: it carries a
marker written when this command makes it, so a `backup-homes/` that happens to
be *yours* is never adopted and never pruned.

A `backup-homes/` written by an **earlier version** of this command has no
marker either, and both halves refuse it rather than guess. Do not move that one
aside — its runs would land in the destination where nothing prunes them.
Adopt it:

```sh
touch ~/agent-backups/backup-homes/.written-by-backup-homes
```

`prune-backups` is its own command rather than a `find` written out here: it is
an `rm -rf`, it shipped over-broad three times while it lived in this file, and
the only way to test it here was to parse the snippet back out and re-run it.
Its reasoning is in `agent_mgr/backups.py`. The day count must be a whole number of
**at least 1**, and is checked, because it lands inside `find`'s own expression:
`-1` becomes `-mtime +-1`, which matches *fresh* directories, and `0` would
delete the run written seconds earlier in the same line.

The `&&` comes first in importance: retention runs only if the backup it is
pruning *for* succeeded. Split that into two crontab lines, or use `;`, and a
run of failed nights — a full disk, a destination whose mode changed, `no homes
matched` under the wrong account — prunes the destination empty while writing
nothing.

The braces and the redirect are what make any of it observable: cron has no
`MAILTO` here, and on a host with no working MTA — the macOS default — its
output is discarded, so a failing night leaves no trace at all. `{ …; }` groups
both halves, so the log catches the backup's diagnostics and not only the
prune's. The log lives **outside** the destination on purpose: the night the
destination is missing — an unmounted disk, the case worth hearing about — a log
inside it could not be opened either, and the entry would fail silently.

### What an archive is worth

It is a **live copy**. `tar` reports *"file changed as we read it"* for any file
rewritten while it is being read — measured on a plain file, not only on a
database — and that is tolerated and the archive published, so such a file can
restore partially. Most of a home is quiescent between turns; the gateway's
SQLite session database is the one written continuously. Refusing it would fail
the nightly every night, so it is a trade: for a consistent copy,
`agent-mgr down <name>` first.

What is **not** tolerated is a file tar could not read at all — an unreadable
`auth.json`, a path it could not stat. `tar` exits 1 for that too, so the
decision is made on its message rather than its status: everything on a measured
list of race warnings passes, anything else fails that home loudly, and the
archive tar had begun is **deleted** rather than kept. A complete, valid,
`gzip -t`-clean archive missing exactly one credential file is worse than no
archive, because a restore reaches for it as the newest thing there.

The status alone could not carry that. GNU tar exits 1 for the race; bsdtar on
macOS exits 0 for it and uses 1 for the unreadable member — both measured.

One home failing does not stop the others: the run archives what it can and then
exits non-zero, so the cron's `&&` still holds retention back.

**That gate is not free, and it is the thing to watch.** A home that fails
*every* night — a permission problem nobody fixes, a diagnostic not yet on the
benign list — makes every run exit non-zero, so the prune never runs and the
documented 14-day retention stops being true. The
destination then grows by a full sweep a night until the disk fills, at which
point every home starts failing. From the outside it looks healthy the whole
time: a new run directory each night with current archives in it.

Two lines say so, both in `~/backup-homes.log`: `tar failed on <home>` names which home,
and `one or more homes were not archived` appears once per run however many did
— that second one is what to grep for.

One further way the archive *container* can go wrong: a killed run leaves a
truncated archive. Nothing repairs or replaces it — every run writes into a new
directory, so the truncated one stays the newest until retention prunes it at 14
days. Running the command twice in a day is safe and simply produces two runs.

`gzip -t <archive>` tests the container and nothing else: a mid-rewrite archive
passes it cleanly. So check before restoring, and fall back to the previous
night's.
### Restoring one

Two blocks, because **you** move the old home aside between them. Naming that
copy is a decision, and three attempts at automating it each produced a worse
hazard than the last.

Step 1 — verify the archive, stop the agent, resolve the home:

```sh
a=~/agent-backups/backup-homes/20260826T040112Z-4171/hermes-errands.tar.gz \
  && gzip -t "$a" \
  && agent-mgr down errands \
  && home=$(readlink -f "$(agent-mgr resolve errands | sed -n 's/^AGENT_HOME=//p')") \
  && echo "move $home aside now — same disk, not /tmp, and a path that neither matches nor sits under ~/.hermes*. Keep it until the restored agent is verified running. Then run step 2."
```

Move `$home` aside now: **same disk, not `/tmp`, and a path that neither matches
nor sits under `~/.hermes*`** — a sibling like `.hermes-errands.old` matches it,
and the nightly would then archive a dead home as a live one. That copy holds
everything written since the archive, including this morning's turns and the
`-wal` and `-shm` sidecars, so keep it until you have watched the restored agent
run.

Step 2, **in the same shell** — `$home` and `$a` come from step 1:

```sh
mkdir "$home" \
  && tar -C "$home" -xzf "$a" \
  && agent-mgr restore errands \
  && agent-mgr up errands
```

Why the blocks are shaped that way:

- **`gzip -t` first, inside the chain.** A bad archive has to stop the restore
  before `down` runs.
- **`&&`, not `set -e`.** This is pasted into your shell, and `errexit` there
  closes the session on the first failure — over SSH, taking the error with it.
- **`down` can legitimately refuse.** It runs the agent's
  `AGENT_PRE_TRANSITION` hook, and the rentals agent declines to stop
  mid-ingest by design.
- **`a=` binds the archive once.** Edit only the `tar` line and `gzip -t`
  validates a different file, which stops nothing.
- **`readlink -f`** resolves a symlinked home to its target, so you move and
  recreate the target and the link keeps pointing at it.
- **`mkdir`, not `mkdir -p`.** It fails with `File exists` if the home is still
  there, which is the emptiness check for free — and `tar -xzf` overlays rather
  than replaces, so unpacking over a live home would leave behind every file the
  archive does not contain.
- **`-C`, because the archive is contents-rooted** (`./` entries) and would
  otherwise splat into `$HOME`. `logs/`, `cache/` and `lazy-packages/` are
  excluded from it and are not recreated: expected, not a truncated archive.

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
second copy to keep in step. It reaches the container by a read-only mount
declared in that repo's `compose.override.yml`, which makes `git pull` the
update and `git rev-parse HEAD` the version;
[`plow-pbc/property-hunt-hermes-agent`](https://github.com/plow-pbc/property-hunt-hermes-agent)
is a worked example.

```sh
agent-mgr add-skill errands plow-pbc/plow --dest plow-connectors \
    --src cloud-agents/hermes/image/seed/skills/productivity/plow-connectors
```

(`plow-connectors` is the retiring REST path — Gmail/Calendar work is moving
to the fleet `google-workspace` skill, Plow Latch / `plow-gog` through the
owner's Mac, which `install-skill` already pins; the REST skill stays for
Slack.)

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
works: it reaches the guard during its final reload, whose refusal it
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
| `configured group(s) not on this agent's line` after a re-activation | § Recovering a line after re-activation |

## Recovering a line after re-activation

A Plow token is **user-scoped**; the agent's *line* identity is client-side —
it is whichever line the home chat (`PLOW_HOME_CHANNEL`) is on. `activate`
mints a fresh token but also provisions a fresh DM on a **randomly assigned**
pool line and writes that DM as the home, which strands every group chat on
the line the agent used to hold: the plugin logs `configured group(s) not on
this agent's line` once a minute and the groups go quiet.

Nothing was lost. The old line's chats still exist server-side and the new
token can see all of them, because authorization is by owner, not by line:

```sh
agent-mgr chats str        # every chat, with its line and number; home marked *
agent-mgr set-home str cht_TheOldLinesDm   # keep the new token, take back the old line
```

`set-home` refuses a uid the token cannot see, writes the dotenv under the
same containment as `set-latch`, and reloads a running gateway. The DM
`activate` provisioned on the new line is simply abandoned — harmless.

There is deliberately no way to *request* a line at activation (the server
assigns one at random), so `set-home` back to the old line's DM is the whole
recovery. First proved live on `str`, 2026-08-27, after a dead token forced a
re-activation onto a fresh line.

## Bumping the plugin pin

`agent-mgr restore <name>` installs the plugin as part of the whole deploy, and
that is the normal path.

**There are two SHA pins and only one of them may move.** Before bumping either,
read the *What this builds on* section of the [README](../README.md), which owns
that rule — restating it here is how this page came to state the opposite of the
code for a commit.

After bumping `artifacts.plow_chat_plugin.revision` in `runtime/stack.json` alone,
`agent-mgr install-plugin <name>` does just that step (plus the idempotent
legacy-dotenv migration every install path carries) — useful when an agent's
restore hook is expensive (the rentals agent re-seeds a vault and rebuilds its
property hubs) and nothing else changed.

`agent-mgr install-skill <name>` is the same shape for the fleet skills
(`artifacts.google_workspace_skill` and `artifacts.plow_invite_skill` in the same lock) —
also the first thing to run on an agent still reporting `NOT_AUTHENTICATED`
from the image-bundled local-OAuth `google-workspace` copy. A destination the
agent's own `skills.tsv` pins is skipped per skill, there and in `restore`
alike: the instance pin is authoritative, so bump that row and re-run
`restore` to change that one copy — the other fleet skills still install.
