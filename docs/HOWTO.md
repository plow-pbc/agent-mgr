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
agent-mgr sign-in errands     # device-code OAuth; hand the URL to whoever owns it
```

`activate` is the step only a person can finish. `POST /v1/auth/activate` carries
no credential — the account binding is *whichever phone texts the code back*. So
the code must be sent from the handset that should own the agent. A code texted
by the wrong person binds the agent to the wrong account, and it is a one-time
spend.

To let it drive a Mac, put the pair from that Mac in that instance's dotenv —
`$AGENT_HOME/.env`, which `agent-mgr resolve errands` prints:

```sh
DOMO_DEVICE_UID=dev_...
DOMO_MCP_TOKEN=...
agent-mgr check-latch errands   # "latch reachable ... (HTTP 200)"
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
BOB_HOME=$(agent-mgr resolve bob | sed -n 's/^AGENT_HOME=//p')
printf '\nAGENT_TZ=America/Chicago\n' >> "${BOB_HOME:?resolve printed no home}/.env" &&
agent-mgr up bob
```

**The zone goes after `restore` and before `up`, and both bounds bite.**
*Where a per-person value goes* in the [README](../README.md) owns why the zone
is special, including the leading newline; what this runbook owns is where it
sits. Earlier than `restore` and there is no dotenv skeleton — `restore` writes
one only when the file is absent, so creating it yourself means the
`PLOW_CHAT_*` and `DOMO_*` placeholders never land. Later than `up` and it is
simply not applied, because the zone reaches the container when the container is
**created**; changing it after that needs another `agent-mgr up`, never
`restart`, which does not recreate.

The `:?` and the `&&` are both load-bearing: without them a `resolve` that
printed nothing appends to `/.env`, and `up` then starts the agent anyway — on
the fleet default zone, quietly, which is the failure this whole step exists to
prevent.

`up` before the codes, not after: `sign-in` runs `hermes auth add` **inside the
container**, so it refuses until one is running. `activate` does not care — it
writes to the home and reloads only if something is up.

Then a strictly sequential exchange — each step finishes before the next starts,
because two of them block:

| | who | what |
|---|---|---|
| 1 | you | `agent-mgr activate bob` — prints `Plow Activate: <code>`, then **polls until it is texted** |
| 2 | **them** | text that code **from the handset that should own the agent** |
| 3 | you | `agent-mgr sign-in bob` — prints a device-code URL and a code, then **waits on the browser** |
| 4 | **them** | open the URL in *their* browser, enter the code |
| 5 | **them** | Plow Latch → Connect a client → mint an agent credential for this agent |
| 6 | you | put the pair in `"$BOB_HOME/.env"` — **their** instance's dotenv, bound above — then `check-latch bob` and `restart bob` |

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
prerequisite you cannot satisfy for them. If this agent drives no Mac, skip both
and delete the `latch:` block from its `config.yaml` — per the section above, a
declared latch with no credential is a broken agent rather than an unconfigured
one.

**The pair in step 5 is copy-once by design.** Latch drops it from memory once
they confirm they have saved it, which relaying it through a chat window
defeats — it lands in two message stores and both their backups, and that token
drives their Mac. Nothing here prevents that, and it is the ordinary route
today; just treat it as disclosed and re-mint from Latch once the agent is up.
Re-minting is free. The exposure is not.

**Tell them where their credentials live**, before step 2 rather than after:
their Plow token, and through it their mailbox, sit in `$AGENT_HOME/.env` on
this host — readable by whoever runs `agent-mgr`, which is not them.

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
```

Both `check-` commands ask **from inside the container**, because the container
is what has to reach `api.plow.co`. Egress, DNS and CA config all differ between
your shell and that network namespace, and every one of those failures is
invisible to a host-side probe. There is deliberately no host fallback.

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
agent-mgr add-skill errands plow-pbc/hermes-plow-chat --dest plow-connectors \
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
