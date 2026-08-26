# agent-mgr

One CLI for the Hermes agent fleet. It starts and manages containers running
Hermes with **Plow Chat** and **Plow Latch**, so standing up a new agent is a
command rather than a copy-paste of the last one.

```sh
agent-mgr new rowan          # scaffold an instance repo, both platforms wired
agent-mgr restore rowan      # the whole deploy: config, plugin, restore hook
agent-mgr activate rowan     # prints a code; text it from the agent's phone
agent-mgr up rowan
agent-mgr sign-in rowan
```

See [docs/HOWTO.md](docs/HOWTO.md) for the full walkthrough.

## What this builds on

```
  plow-pbc/seed-plow-chat        the Plow Chat wire protocol, REST + WSS
            │                    (spec, not SDK — this repo never calls it directly)
            │ implemented by
  plow-pbc/seed-hermes-plow      the plow-chat-platform plugin: the phone line
            │
            │ pinned in runtime/plow-chat-plugin.ref
            ▼
        agent-mgr ───── pinned in runtime/image.ref ─────▶ nousresearch/hermes-agent
            │                                              (the runtime; third party)
            └───── templates/config.yaml ─────▶ Plow Latch, over the relay at api.plow.co
```

| dependency | what it is | pinned as |
|---|---|---|
| `nousresearch/hermes-agent` | the agent runtime; third-party image | `runtime/image.ref`, a **digest** |
| `plow-pbc/seed-hermes-plow` | the `plow-chat-platform` plugin — the phone line | `runtime/plow-chat-plugin.ref`, a **40-char SHA** |
| `plow-pbc/seed-plow-chat` | the protocol that plugin implements | not consumed directly |
| Plow Latch | the Mac an agent drives, over the relay | `templates/config.yaml`; credentials come from the agent's own dotenv, never from git |

Both pins are exact on purpose. A tag or a branch re-resolves on the next pull,
which silently changes a large unreviewed surface under a running agent that
holds live credentials — and for the plugin, one that holds the chat token.

## Sharing with `plow-pbc/plow`

`plow-pbc/plow`'s `cloud-agents/hermes` stands up the *same* Hermes runtime for
Plow's customers: one VM per tenant, native under systemd, provisioned by
`POST /v1/agents/cloud`. This repo is the other end — one host, many agents,
Docker, driven by a person at a terminal.

Same protocol underneath, different products around it. So the posture is:

**Converge on the artifacts.** The plugin, the upstream image and the
integration reference are the *same facts* on both sides, and a fix to one
should reach the other. Where they have already forked, it is tracked rather
than tolerated:

- [`plow-pbc/plow#1394`](https://github.com/plow-pbc/plow/issues/1394) —
  `cloud-agents/hermes` carries its own copy of the plugin, under the same id
- [`#2`](https://github.com/plow-pbc/agent-mgr/issues/2) — the upstream image
  pin here drifts from plow's blessed base
- [`plow-pbc/seed-hermes-plow#15`](https://github.com/plow-pbc/seed-hermes-plow/issues/15) —
  the plugin this repo pins has a defect plow's copy already fixed

**Keep the managers separate.** Provisioning here is a bash CLI over Compose;
there it is a `Provider` protocol behind an HTTP endpoint. Activation here is a
code a person texts from the owning handset; there a token is minted
machine-side. Latch, the domain layer and instance repos have no tenant
equivalent at all. Unifying those would be an abstraction over two things that
are not the same thing.

The test is the same shape as the layer test below: **is this the same fact, or
does it only look similar?** A pinned SHA is the same fact. A lifecycle is not.

## Three layers, and what belongs in each

This repo holds **mechanism only**. It carries no agent instances, no personal
paths, and no credentials.

| layer | lives in | what it is |
|---|---|---|
| **mechanism** | this repo | true of *every* Hermes agent, whatever it does |
| **instance** | `<agent>-hermes-agent` | *which* agent this is: name, home, which Mac it drives, which skills it pins |
| **domain** | e.g. `plow-pbc/property-hunt` | what the agent actually *does* |

The test for where something belongs: **if this agent's product vanished
tomorrow, would this code still exist?**

- `agent-mgr up` — yes, the other agents still need it → **mechanism**
- `agent.env` — no, it *is* this agent's identity → **instance**
- a recipe that publishes a property map — no, it exists only for that product → **domain**

The rule is about duplication, not size. A thin instance repo is still an
instance repo; domain code stays put however little else surrounds it. And the
rule is dynamic: something used by one agent today graduates to this repo when a
second agent wants it.

### What an instance repo contains

```
rowans-life-hermes-agent/
  agent.env               written by `new`; overrides only, every key optional
  config.yaml             written by `new`; model, plugins, mcp_servers
  skills.tsv              created by the first `add-skill`; pinned domain skills
  compose.override.yml    hand-written, and only for a derived image or extra mounts
```

`agent.env` can be empty. `AGENT_HOME` defaults to `~/.hermes-<name>`,
`AGENT_CONTAINER` and `AGENT_PROJECT` to `hermes-<name>`, and the image to the
fleet-wide digest in `runtime/image.ref`.

Relative paths do **not** work in `compose.override.yml`: Compose resolves them
against *this* repo's directory, not the instance's. Name paths through a
variable set in `agent.env`.

An instance repo names one agent, and everything in it may say so.

### What a domain repo contains

```
property-hunt/
  SKILL.md        the instructions, delivered to the CONTAINER at a pinned SHA
  scripts/        what actually runs — in the container, or on the Mac via Latch
  justfile        the recipes the agent invokes, and this repo's own tests
  references/     detail the skill loads on demand
```

Installed by `agent-mgr add-skill`, which records the pin in the *instance*
repo's `skills.tsv`. Never vendored into an agent repo — a committed copy is a
fork that no longer receives the upstream's fixes.

Worked example — publishing a property map from the Mac. All three pieces live
in `property-hunt`, none in this repo and none in the instance repo:

- the instruction *"to publish the map, run `just serve-map` on the Mac through Latch"* → its `SKILL.md`, delivered to the **container**
- the recipe `serve-map:` → its `justfile`, delivered to the **Mac**
- the launchd plist that keeps it up → its `scripts/`, delivered to the **Mac**

The distinguishing rule: **a domain repo names no agent.** One agent installs it
today and another could tomorrow, so anything that names *which* agent, *which*
home or *which* Mac belongs in the instance repo instead. A domain repo that
cannot be installed twice is an instance repo wearing the wrong name.

## Why `agent` uses `exec`

The Hermes image's s6 entrypoint starts a gateway *whatever command you pass
it*. So `docker compose run ... chat -q` brings up a **second** gateway against
the same `/opt/data`, which connects to the chat, answers alongside the real
one, and on exit posts a shutdown notice into the owners' channel.

That is not hypothetical. Over two days on one host it produced 25 gateway
starts against a baseline of 1–6 per day, 21 shutdown notices into an owners'
channel in a single day, and 6 sqlite errors from two gateways racing one
session database. `tests/test_no_second_gateway.py` is the guard.
