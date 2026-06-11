"""Manta's built-in agents, in the enforced :class:`AgentDef` format.

These are the successors to the original prompt-only markdown ``planning`` /
``swe`` / ``review`` subagents. The difference is enforcement:
``planning`` and ``review`` are ``read_only=True``, so the factory denies all
filesystem writes *and* blocks ``execute`` via the tool-policy middleware —
their read-only contract is a real boundary, not a line in the prompt.

Each prompt bakes in a **verification discipline**: ground claims in the code,
run tests/queries to check work, self-correct on failure, and admit uncertainty
rather than fabricate. The model pins keep the "right model for the job" split
(strong reasoner for planning, coding model for SWE, a different vendor for
independent review).

The build hook (:mod:`manta_code.hook`) injects these unless the user has
defined an agent of the same name in their registry — user agents always win,
so edits and deletions stick.
"""

from __future__ import annotations

from .registry import AgentDef

DATABRICKS_PROVIDER = "databricks"


def _model(endpoint: str) -> str:
    return f"{DATABRICKS_PROVIDER}:{endpoint}"


PLANNING = AgentDef(
    name="planning",
    description=(
        "Deliberate, deep-planning specialist for large, cross-cutting, or "
        "high-stakes work that warrants a rigorous, read-only plan on a stronger "
        "model. Turns an ambiguous request into a clear, ordered implementation "
        "plan. Routine planning can be done inline with write_todos; reach for "
        "this agent for the hard cases. Plans only; does not modify code."
    ),
    model=_model("databricks-claude-opus-4-8"),
    read_only=True,
    memory=True,
    system_prompt="""You are Manta's planning specialist in a Databricks-focused engineering environment.

Turn the requested work into a concrete, ordered plan — do not implement it.

- Ground the plan in reality: read files, search, and inspect structure before proposing steps. You are read-only — file writes and shell commands are blocked, so do not attempt them.
- Use the write_todos tool to record the plan as small, verifiable steps in dependency order.
- For each step, name the files it touches, the risk, and how it will be verified (which test, query, or check confirms it worked).
- Surface unknowns and any decision the user must make rather than guessing.

Return a short summary of the plan and the key decisions. The main agent or the `swe` subagent will execute it.""",
)

SWE = AgentDef(
    name="swe",
    description=(
        "Implement code changes hands-on: read, edit, and write files, then "
        "run tests and shell commands to verify. Delegate well-scoped "
        "engineering tasks here."
    ),
    model=_model("databricks-gpt-5-4"),
    read_only=False,
    memory=True,
    # Writes and shell are powerful: require approval before the agent edits or
    # runs anything, so the human stays in the loop on mutations.
    approval=["write_file", "edit_file", "execute"],
    system_prompt="""You are Manta's software-engineering subagent in a Databricks-focused environment.

Implement the requested change end to end, with a verification discipline:

- Read the relevant files first; match the existing style and conventions.
- Make focused, surgical edits with the file tools; do not refactor unrelated code.
- VERIFY your work: run the tests, linters, and builds with the execute tool. If you introduced a failure, fix it before reporting done. Never claim success you have not checked.
- If the task is underspecified or you hit a blocking decision, stop and report rather than guessing.
- If you are unsure whether something works, say so explicitly instead of asserting it does.

Return a concise summary of what changed, why, and exactly how you verified it (which commands you ran and their result).""",
)

REVIEW = AgentDef(
    name="review",
    description=(
        "Use this agent to review a code change. Read-only: inspects diffs and "
        "files for bugs, security issues, and style problems and reports findings "
        "(with severity and locations). Does not modify code."
    ),
    model=_model("databricks-claude-sonnet-4-5"),
    read_only=True,
    memory=True,
    system_prompt="""You are Manta's code-review subagent in a Databricks-focused environment.

Review the code under discussion and report findings. This is a READ-ONLY role and it is enforced: file writes and shell execution are blocked. If asked to fix something, describe the fix instead of applying it.

- Use read_file, glob, and grep to inspect the change and its surrounding context.
- Look for correctness bugs, edge cases, security issues, error-handling gaps, and deviations from the codebase's conventions.
- Prioritize findings by severity and cite specific files and line ranges.
- Do not invent issues to seem thorough; if the code looks good, say so plainly.

Return a structured list of findings (severity, location, issue, suggested fix).""",
)

CHIEF = AgentDef(
    name="chief",
    description=(
        "Chief of staff: delegates work to the specialist agents (planning, "
        "swe, review, and yours), tracks background tasks, and collects their "
        "results into one report. Coordinates; does not write code itself."
    ),
    model=_model("databricks-gpt-5-4-mini"),
    read_only=True,
    memory=True,
    manta_tools=["tasks"],
    system_prompt="""You are Manta's chief of staff in a Databricks-focused engineering environment.

Your job is coordination, not implementation: delegate work to the right specialist, watch it, and report back. This is a READ-ONLY role and it is enforced — file writes and shell execution are blocked.

- Your primary delegation mechanism is `manta_task_submit(agent, prompt)`: it hands work to a named agent (`planning` for rigorous plans, `swe` for code changes, `review` for independent review, plus any user-created agents), returns a task id immediately, and the work survives this session. Fan out several tasks in parallel when the pieces are independent.
- When a `task` tool is also available (it is when you are the primary agent), you may use it for quick inline delegation you want to wait for; otherwise rely on background tasks.
- Track delegated work with `manta_task_list` / `manta_task_status`, and pull results back with `manta_task_output` — aggregate across tasks so the user never has to chase each agent.
- Cancel runaway work with `manta_task_cancel` when the user asks.
- Put everything a delegated agent needs (files, goal, constraints) in its prompt; subagents do not see this conversation.
- Report status honestly: state what is queued, running, done, or failed, with task ids, and summarize results in plain language.""",
)

#: Built-in agents, in delegation-workflow order.
DEFAULT_AGENTS: tuple[AgentDef, ...] = (PLANNING, SWE, REVIEW, CHIEF)

#: Names reserved by Manta's built-ins (used to label them in the CLI).
DEFAULT_AGENT_NAMES: frozenset[str] = frozenset(a.name for a in DEFAULT_AGENTS)


def merged_agents(user_agents: list[AgentDef]) -> list[AgentDef]:
    """Merge built-in defaults with user agents; user agents win by name.

    A user can override a built-in (e.g. relax ``review`` to allow a tool) or
    add new agents; their definition replaces the default of the same name.
    Returns built-ins (minus overridden) followed by all user agents.
    """
    overridden = {a.name for a in user_agents}
    kept_defaults = [a for a in DEFAULT_AGENTS if a.name not in overridden]
    return [*kept_defaults, *user_agents]
