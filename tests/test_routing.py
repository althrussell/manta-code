from manta_cli.routing import HeuristicRouter
from manta_cli.schemas import RouteName


def test_simple_question_routes_cheap():
    decision = HeuristicRouter().route("what does this error mean?")
    assert decision.route == RouteName.SIMPLE_ANSWER
    assert decision.pipeline == ["cheap_responder"]
    assert not decision.needs_planning


def test_security_task_routes_security_pipeline():
    decision = HeuristicRouter().route("add JWT auth token refresh and update secrets handling")
    assert decision.route == RouteName.SECURITY_SENSITIVE
    assert decision.needs_planning
    assert decision.needs_security_review
    assert "security_reviewer" in decision.pipeline


def test_complex_task_routes_planner():
    decision = HeuristicRouter().route("design an end-to-end workflow refactor across multiple files")
    assert decision.route == RouteName.COMPLEX_ARCHITECTURE
    assert decision.needs_planning
    assert "planner" in decision.pipeline
