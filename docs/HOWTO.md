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
instance repo, so every command works from any directory.

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
```

Both `check-` commands ask **from inside the container**, because the container
is what has to reach `api.plow.co`. Egress, DNS and CA config all differ between
your shell and that network namespace, and every one of those failures is
invisible to a host-side probe. There is deliberately no host fallback.

## Three layers: where does my code go?

Ask: **if this agent's product vanished tomorrow, would this code still exist?**

| | repo | example |
|---|---|---|
| **mechanism** | `agent-mgr` | `up`, `activate`, `check-latch` |
| **instance** | `<agent>-hermes-agent` | `agent.env`, `config.yaml`, `skills.tsv` |
| **domain** | e.g. `plow-pbc/property-hunt` | the skill, its scripts, its recipes |

Worked example — publishing a property map from the Mac. All three pieces live
in `property-hunt`, none in `agent-mgr` and none in the instance repo:

- the instruction *"to publish the map, run `just serve-map` on the Mac through Latch"* → its `SKILL.md`, delivered to the **container**
- the recipe `serve-map:` → its `justfile`, delivered to the **Mac**
- the launchd plist that keeps it up → its `scripts/`, delivered to the **Mac**

The rule is about duplication, not size: a thin instance repo is still an
instance repo. And it is dynamic — something one agent uses today graduates to
`agent-mgr` when a second agent wants it.

## Adding a domain skill

```sh
agent-mgr add-skill property plow-pbc/property-hunt --dest productivity/property-hunt
```

The pin is recorded in the *instance* repo's `skills.tsv`, so what an agent runs
is reviewable beside its config. Refs are always 40-char SHAs — a branch would
silently re-point a running agent on the next upstream push.

**A skill can reach an agent by two routes.** `add-skill` installs the
instructions into the container from a pinned SHA; ClawHub installs the whole
bundle onto the Mac by semver. Only the first is pinned here, so the two can
skew — the container reading instructions the Mac's code does not implement. If
a skill misbehaves, compare the two versions before debugging the code.

## What an instance repo contains

The file list, and why each one is or is not there, is the *What belongs in an
instance repo* section of the [README](../README.md). Two things that only
matter once you are editing one:

`agent.env` can be empty. `AGENT_HOME` defaults to `~/.hermes-<name>`,
`AGENT_CONTAINER` and `AGENT_PROJECT` to `hermes-<name>`, and the image to the
fleet-wide digest in `runtime/image.ref`.

Relative paths do **not** work in `compose.override.yml`: Compose resolves them
against `agent-mgr`'s directory, not the instance's. Name paths through a
variable set in `agent.env`.

## Why `agent` uses `exec`

The Hermes image's s6 entrypoint starts a gateway *whatever command you pass
it*. `docker compose run ... chat -q` therefore brings up a **second** gateway
against the same `/opt/data`. It connects to the chat, answers every message
alongside the real one, and on exit posts a shutdown notice into the owners'
channel.

Measured on this host over two days before the fix: **25 gateway starts**
against a 1–6/day baseline, **21 shutdown notices** into an owners' channel in a
single day, and 6 sqlite errors from two gateways racing one session database.
`tests/test_no_second_gateway.py` keeps it from coming back.

## When something is wrong

| symptom | look at |
|---|---|
| `refusing to act: compose resolved ...` | the descriptor or override disagrees with the agent you named — this is the guard working |
| `HERMES_UID ... must be set` | you ran `docker compose` directly; go through `agent-mgr` |
| `... is REVOKED` | mint a fresh Latch credential from the Mac |
| `no answer from api.plow.co` | the credential was **not** tested; this is a network fault, not a bad token |
| a skill behaves oddly | compare the container's pinned SHA against the Mac's ClawHub version |

## Bumping the plugin pin

`agent-mgr restore <name>` installs the plugin as part of the whole deploy, and
that is the normal path. After bumping `runtime/plow-chat-plugin.ref` alone,
`agent-mgr install-plugin <name>` does just that step — useful when an agent's
restore hook is expensive (the rentals agent re-seeds a vault and rebuilds its
property hubs) and nothing else changed.
