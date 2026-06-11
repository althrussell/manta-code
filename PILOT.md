# Manta pilot guide

Five minutes from zero to a working, governed, token-smart coding agent.

## Install

```bash
# Requires Python 3.12+, the Databricks CLI, and a workspace login.
pip install "manta-code[agent] @ git+https://github.com/althrussell/manta-code.git"
databricks auth login          # once, if you haven't
manta doctor                   # everything should be green
manta doctor --probe           # optional: live-test every pinned model (~1c/model)
```

No Databricks workspace? Manta still runs — `manta` launches against any
provider keys you configure in `/auth`; Databricks features stay dormant.

## First five minutes

```bash
manta                          # launch; chief-of-staff agent by default
```

Try, in order:
1. Ask anything — routine turns run on the cheap default model.
2. `@review look at <file> and give me findings` — deterministic delegation
   to the read-only reviewer (a different vendor than the coder, on purpose).
3. `@swe <small task> &` — runs detached; you get a desktop notification when
   it finishes. Steer it mid-flight: `manta task send <id> "also update docs"`.
4. `/agents` — switch agents; your conversation follows you, the model pin is
   applied truthfully (footer matches `manta agents`).
5. Tell it something durable ("we always use pytest fixtures, never mocks") —
   it saves with `manta_remember` and future sessions start warm.

## The economy

```bash
manta receipts                 # spend vs all-premium baseline, advisor stats
manta cost --by model          # where the tokens actually went
manta cost --advise            # structural savings recommendations
manta status                   # tasks × events × spend, one pane
```

Set a daily guardrail in `~/.manta/config.toml`:

```toml
[budget]
daily_max_usd = 25.0
```

## Governance you can trust

- Read-only agents **cannot** write or execute — enforced at the tool-call
  layer, not prompted.
- Ask-gated tools (`tools_ask`) prompt a human interactively and **fail
  closed** in unattended runs (`--allow-asks` to pre-approve, audited).
- Every approval, denial, steer, and budget event is in `manta status`.

## In CI

See `examples/ci/manta-pr-review.yml` — the enforced `@review` agent reviews
every PR headlessly and posts findings as a comment.

## Feedback we want from the pilot

1. Did anything feel slow, wrong-model, or expensive? (`manta receipts` after
   a week — does the savings number feel real?)
2. Did a background task ever surprise you (stuck, silent, wrong)?
3. What did you have to tell it twice? (That's a `manta_remember` gap.)
4. Any `manta doctor --probe` failures on your workspace's models?
