from __future__ import annotations

import re

from .schemas import Complexity, Intent, Risk, RouteDecision, RouteName

SECURITY_TERMS = {
    "auth",
    "oauth",
    "jwt",
    "token",
    "secret",
    "password",
    "credential",
    "encrypt",
    "decrypt",
    "permission",
    "rbac",
    "webhook",
    "subprocess",
    "shell",
    "network",
    "migration",
    "database",
    "sql injection",
}

COMPLEX_TERMS = {
    "architecture",
    "refactor",
    "multi-file",
    "multiple files",
    "design",
    "end-to-end",
    "e2e",
    "workflow",
    "pipeline",
    "deep plan",
    "system",
    "framework",
}

CODE_TERMS = {
    "add",
    "build",
    "fix",
    "implement",
    "create",
    "update",
    "change",
    "refactor",
    "test",
    "bug",
    "feature",
}

QUESTION_TERMS = {"what", "why", "how", "explain", "where", "when"}


class HeuristicRouter:
    """Cheap deterministic router used for dry-run and as a fallback.

    The real v1 router should call a cheap model and validate the output against
    RouteDecision. This fallback keeps the CLI useful without credentials and
    provides deterministic tests.
    """

    def route(self, prompt: str) -> RouteDecision:
        text = prompt.lower().strip()
        words = set(re.findall(r"[a-zA-Z0-9_-]+", text))

        security_hit = any(term in text for term in SECURITY_TERMS)
        complex_hit = any(term in text for term in COMPLEX_TERMS)
        code_hit = any(term in words for term in CODE_TERMS)
        question_hit = any(text.startswith(term + " ") for term in QUESTION_TERMS)

        if security_hit:
            return RouteDecision(
                intent=Intent.CODE_CHANGE if code_hit else Intent.SECURITY_REVIEW,
                complexity=Complexity.HIGH,
                risk=Risk.HIGH,
                route=RouteName.SECURITY_SENSITIVE,
                needs_planning=True,
                needs_review=True,
                needs_security_review=True,
                pipeline=["planner", "builder", "code_reviewer", "security_reviewer"],
                max_budget_usd=5.0,
                reason="Security-sensitive terms detected; route includes security reviewer.",
            )

        if complex_hit:
            return RouteDecision(
                intent=Intent.PLAN if "plan" in words or "design" in words else Intent.CODE_CHANGE,
                complexity=Complexity.HIGH,
                risk=Risk.MEDIUM,
                route=RouteName.COMPLEX_ARCHITECTURE,
                needs_planning=True,
                needs_review=True,
                needs_security_review=False,
                pipeline=["planner", "builder", "code_reviewer"],
                max_budget_usd=3.0,
                reason="Complexity or architecture signal detected; planner is justified.",
            )

        if code_hit:
            # A short prompt that looks like a small edit can skip review by default.
            if len(words) <= 8 and not any(term in words for term in {"tests", "test", "feature"}):
                return RouteDecision(
                    intent=Intent.CODE_CHANGE,
                    complexity=Complexity.LOW,
                    risk=Risk.LOW,
                    route=RouteName.TRIVIAL_CODE_CHANGE,
                    needs_planning=False,
                    needs_review=False,
                    needs_security_review=False,
                    pipeline=["builder"],
                    max_budget_usd=0.25,
                    reason="Small code-change prompt; direct builder route.",
                )
            return RouteDecision(
                intent=Intent.CODE_CHANGE,
                complexity=Complexity.MEDIUM,
                risk=Risk.LOW,
                route=RouteName.NORMAL_CODE_CHANGE,
                needs_planning=False,
                needs_review=True,
                needs_security_review=False,
                pipeline=["builder", "code_reviewer"],
                max_budget_usd=1.0,
                reason="Normal code-change prompt; builder plus reviewer route.",
            )

        if question_hit:
            return RouteDecision(
                intent=Intent.QUESTION,
                complexity=Complexity.LOW,
                risk=Risk.LOW,
                route=RouteName.SIMPLE_ANSWER,
                needs_planning=False,
                needs_review=False,
                needs_security_review=False,
                pipeline=["cheap_responder"],
                max_budget_usd=0.02,
                reason="Simple question; cheap responder route.",
            )

        return RouteDecision(
            intent=Intent.UNKNOWN,
            complexity=Complexity.LOW,
            risk=Risk.LOW,
            route=RouteName.SIMPLE_ANSWER,
            needs_planning=False,
            needs_review=False,
            needs_security_review=False,
            pipeline=["cheap_responder"],
            max_budget_usd=0.02,
            reason="No code or risk signal detected; cheap responder fallback.",
        )
