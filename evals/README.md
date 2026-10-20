# Evaluations

## Repository outcome baseline

The foundation suite evaluates edits in disposable fixture repositories, with
executable acceptance checks kept outside the agent's working repository:

| Task | Acceptance checks |
| --- | --- |
| `python-bug` | Exact decimal totals, final half-up rounding and preserved API |
| `python-refactor` | Single-pass iterable consumption, stable grouping and no mutation |
| `frontend-render` | Ordered HTML output and escaping; requires Node.js |
| `databricks-bundle` | Correct job/notebook/targets and serverless configuration; no deployment |

Run the hermetic grader contracts without inference:

```bash
.venv/bin/python -m pytest tests/test_repository_evals.py -q
```

These tests establish that unchanged code fails, valid edits pass, and a failed
agent process cannot earn a pass even with correct files. CI installs Node 24
before running the Python 3.12/3.14 matrix.

To test the actual agent against all four tasks:

```bash
.venv/bin/python -m evals.repositories --live --profile labs \
  --output /tmp/manta-repository-results.json
```

This makes paid Gateway requests using the selected profile. Each run uses a
temporary repository and pre-approves the SWE agent's ASK tools for those fixture
tasks. It uses the normal Manta user config/credentials unless you deliberately
provide isolated homes. No Databricks resources are deployed. Optional
`--model databricks:<exact-target>` selects a model; repeat `--task <id>` to run
a subset. Defaults are 600 seconds and 30 agent turns per task, configurable
with `--timeout` and `--max-turns`.

The JSON artifact records pass/fail, agent and acceptance exit codes, changed
files, diff, test output, elapsed time, usage tag, tokens, dominant model,
estimated cost and redacted runtime output. A pass requires a successful agent
exit, passing hidden checks and an actual edit. Missing usage or exact prices
leaves cost unknown. Inspect artifacts before sharing: diffs and task output
remain repository content, and pattern-based redaction cannot identify every
possible secret.

This small baseline checks coding outcomes; it does not establish comparative
model quality, routing savings, large-context accuracy or multi-agent throughput.
Those claims require held-out tasks and repeated comparable runs. Candidate live
results and remaining validation are tracked in
[the foundation record](../docs/19-dependable-foundations.md).

## Answer and cost smoke suite

`python -m evals` compares deterministic canned answers without calling a model.
Prices come from Manta's exact service pricing rules. An unknown service price
stays unknown, including in suite totals, savings, and win verdicts. The report
shows any known subtotal and how many tasks have prices.

A fully priced comparison exits successfully only when the candidate holds
quality and costs no more. An unpriced comparison runs as a quality-only demo:
it exits successfully when quality holds and explicitly reports that there is
no cost or savings verdict. Unit tests inject synthetic prices to qualify the
comparison arithmetic without inventing rates for current services.

`python -m evals --live` makes paid model requests through Manta. It reads tokens
and local cost estimates from rows tagged for that exact task, excluding other
concurrent runs. Missing records, missing prices, or incomplete usage leave cost
unknown. Local estimates do not establish Databricks billing charges.
Nonzero agent execution is a failed task even if stdout looks plausible, and
the live CLI returns failure when any task fails its acceptance score. These
answer-generation graders remain smoke checks; they do not replace executable
repository acceptance.
