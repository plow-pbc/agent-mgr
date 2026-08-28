# agent-mgr — notes for agents operating the fleet

## One agent on the host is an external user's — leave it alone

`agent-mgr ls` lists every agent on the host, not every agent you may
operate. `mark-property` (container `hermes-mark-property`, home
`~/.hermes-mark-property`, repo `property-hunt-hermes-agent`) belongs to
Mark, an external user. A fleet-wide action — `install-plugin`, `restore`,
`restart`, a config or pin rollout — skips it, and nothing runs on Mark's own
machine either. If a change needs to reach his agent, hand that step to Sam,
and say in the rollout report that it was skipped.
