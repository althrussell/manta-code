"""Graders: deterministic, explainable scoring of agent output.

A grader maps an output string to a :class:`Grade` (score in ``[0, 1]`` plus a
human-readable note). They are intentionally simple and rule-based — keyword and
structure checks — so eval scores are reproducible in CI and easy to audit. They
are *not* a substitute for human review; they are a regression tripwire and a way
to compare two configurations on the same rubric.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

#: A grader scores one output string.
Grader = Callable[[str], "Grade"]


@dataclass
class Grade:
    """A single graded outcome."""

    score: float
    notes: str = ""

    def __post_init__(self) -> None:
        # Clamp so a buggy grader can't push aggregate scores out of range.
        self.score = max(0.0, min(1.0, float(self.score)))


def contains_all(keywords: list[str]) -> Grader:
    """Score = fraction of ``keywords`` (case-insensitive) present in the output."""
    wanted = [k.lower() for k in keywords]

    def grade(output: str) -> Grade:
        low = output.lower()
        hits = [k for k in wanted if k in low]
        score = len(hits) / len(wanted) if wanted else 1.0
        missing = [k for k in wanted if k not in low]
        note = "all key concepts present" if not missing else f"missing: {', '.join(missing)}"
        return Grade(score, note)

    return grade


def contains_any(keywords: list[str]) -> Grader:
    """Score 1.0 if any keyword is present, else 0.0."""
    wanted = [k.lower() for k in keywords]

    def grade(output: str) -> Grade:
        low = output.lower()
        hit = next((k for k in wanted if k in low), None)
        return Grade(1.0 if hit else 0.0, f"matched '{hit}'" if hit else "no expected concept found")

    return grade


def has_code_block() -> Grader:
    """Score 1.0 if the output contains a fenced code block."""

    def grade(output: str) -> Grade:
        ok = "```" in output
        return Grade(1.0 if ok else 0.0, "code block present" if ok else "no code block")

    return grade


def regex_present(pattern: str, *, flags: int = re.IGNORECASE) -> Grader:
    """Score 1.0 if ``pattern`` matches anywhere in the output."""
    compiled = re.compile(pattern, flags)

    def grade(output: str) -> Grade:
        ok = bool(compiled.search(output))
        return Grade(1.0 if ok else 0.0, f"/{pattern}/ {'matched' if ok else 'not found'}")

    return grade


def penalize_if(pattern: str, *, flags: int = re.IGNORECASE) -> Grader:
    """Score 0.0 if ``pattern`` is present (a guardrail check), else 1.0."""
    compiled = re.compile(pattern, flags)

    def grade(output: str) -> Grade:
        bad = bool(compiled.search(output))
        return Grade(0.0 if bad else 1.0, f"forbidden /{pattern}/ present" if bad else "clean")

    return grade


def _extract_sql(output: str) -> str:
    """Pull SQL out of a fenced ```sql block, else return the output verbatim."""
    fence = re.search(r"```(?:sql)?\s*(.*?)```", output, re.DOTALL | re.IGNORECASE)
    return fence.group(1).strip() if fence else output.strip()


def sql_is_read_only() -> Grader:
    """Score 1.0 if the SQL in the output is read-only.

    Reuses Manta's own read-only classifier so the eval exercises the same logic
    the SQL tool enforces at runtime. Markdown ```sql fences are stripped first so
    a fenced answer is graded on the query itself.
    """

    def grade(output: str) -> Grade:
        sql = _extract_sql(output)
        try:
            from manta_code.databricks_tools import is_read_only_sql
        except Exception:  # noqa: BLE001 - if unavailable, fall back to a regex
            ok = not re.search(r"\b(insert|update|delete|drop|alter|create|merge)\b", sql, re.I)
            return Grade(1.0 if ok else 0.0, "regex read-only check")
        ok = is_read_only_sql(sql)
        return Grade(1.0 if ok else 0.0, "read-only" if ok else "contains a write statement")

    return grade


def weighted(graders: list[tuple[Grader, float]]) -> Grader:
    """Combine graders by weight into one averaged :class:`Grade`."""

    def grade(output: str) -> Grade:
        total_weight = sum(w for _, w in graders) or 1.0
        score = 0.0
        notes: list[str] = []
        for g, w in graders:
            r = g(output)
            score += r.score * w
            notes.append(f"{r.notes} ({r.score:.2f})")
        return Grade(score / total_weight, "; ".join(notes))

    return grade
