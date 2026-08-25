# agent-mgr

One CLI for the Hermes agent fleet. It starts and manages containers running
Hermes with **Plow Chat** and **Plow Latch**, so standing up a new agent is a
command rather than a copy-paste of the last one.

```sh
agent-mgr new rowan          # scaffold an instance repo, both platforms wired
agent-mgr restore rowan      # config + a dotenv skeleton into its home
agent-mgr install-plugin rowan
agent-mgr activate rowan     # prints a code; text it from the agent's phone
agent-mgr up rowan
agent-mgr sign-in rowan
```

See [docs/HOWTO.md](docs/HOWTO.md) for the full walkthrough.

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

## Why `agent` uses `exec`

The Hermes image's s6 entrypoint starts a gateway *whatever command you pass
it*. So `docker compose run ... chat -q` brings up a **second** gateway against
the same `/opt/data`, which connects to the chat, answers alongside the real
one, and on exit posts a shutdown notice into the owners' channel.

That is not hypothetical. Over two days on one host it produced 25 gateway
starts against a baseline of 1–6 per day, 21 shutdown notices into an owners'
channel in a single day, and 6 sqlite errors from two gateways racing one
session database. `tests/test_no_second_gateway.py` is the guard.
