# agent-mgr — notes for agents operating a fleet

## Not every agent on the host is the operator's to operate

`agent-mgr ls` lists every agent registered on the host — including ones that
belong to other people and merely live here. Nothing in the tooling tells
them apart, and this repo carries no instance names (see README § What
belongs in this repo), so *which* agents are someone else's is recorded in the
operator's own host-local instructions. Read those before any fleet-wide
action — `install-plugin`, `restore`, `restart`, a config or pin rollout —
skip the agents they name, never work on those people's machines, and say in
the rollout report what was skipped. A change that has to reach such an agent
is handed to the operator, not applied.
