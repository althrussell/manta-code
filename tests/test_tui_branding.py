"""Render the live upstream startup widget instead of obsolete banner constants."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("deepagents_code")

from deepagents_code import _invocation, config
from deepagents_code.app import DeepAgentsApp
from deepagents_code.terminal_title import TerminalTitle
from deepagents_code.tui.widgets import welcome
from deepagents_code.tui.widgets._copy_spans import copy_span_target
from textual.app import App
from textual.widgets import Header
from textual.widgets._header import HeaderTitle

from manta_code import _boot, branding


def _apply() -> bool:
    return branding.apply_tui_branding(
        unicode_banner=_boot.MANTA_UNICODE_BANNER,
        ascii_banner=_boot.MANTA_ASCII_BANNER,
        unicode_ray=_boot.MANTA_RAY_UNICODE,
        ascii_ray=_boot.MANTA_RAY_ASCII,
        version="9.8.7",
    )


@pytest.fixture(autouse=True)
def _isolate_branding(monkeypatch):
    monkeypatch.setattr(welcome.WelcomeBanner, "_build_banner", welcome.WelcomeBanner._build_banner)
    monkeypatch.setattr(
        welcome.WelcomeBanner,
        "on_resize",
        getattr(welcome.WelcomeBanner, "on_resize", None),
        raising=False,
    )
    monkeypatch.setattr(DeepAgentsApp, "TITLE", DeepAgentsApp.TITLE)
    monkeypatch.setenv("DEEPAGENTS_CODE_INVOKED_AS", "dcode")
    monkeypatch.setattr(config, "_charset_mode_cache", config.CharsetMode.UNICODE)
    monkeypatch.setattr(config, "_glyphs_cache", None)
    monkeypatch.setattr(welcome, "_is_editable_install", lambda: False)
    monkeypatch.setattr(welcome, "get_langsmith_project_name", lambda: None)
    for env in (
        welcome.HIDE_SPLASH_VERSION,
        welcome.DEBUG,
        welcome.EXPERIMENTAL,
        welcome.SPLASH_SHOW_MODEL,
        welcome.SPLASH_SHOW_CWD,
    ):
        monkeypatch.delenv(env, raising=False)
    yield
    _invocation.invoked_name.cache_clear()


def _app():
    class BannerApp(App):
        TITLE = DeepAgentsApp.TITLE

        def compose(self):
            yield Header()
            yield welcome.WelcomeBanner(
                model_provider="databricks",
                model_name="system.ai.gpt-6-luna",
                cwd="/tmp/manta-branding-test",
                thread_id="thread-123",
                id="welcome-banner",
            )

    return BannerApp()


def test_actual_widget_and_header_show_manta_art_and_product_version():
    assert _apply()
    assert DeepAgentsApp.TITLE == "Manta"

    async def exercise():
        app = _app()
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            rendered = app.query_one(welcome.WelcomeBanner).render().plain
            assert _boot.MANTA_RAY_UNICODE.splitlines()[1] in rendered
            assert _boot.MANTA_WORDMARK_UNICODE.splitlines()[-1] in rendered
            assert "Manta  v9.8.7" in rendered
            assert "dcode" not in rendered
            assert "Manta" in app.query_one(HeaderTitle).render().plain

    asyncio.run(exercise())


def test_narrow_widget_keeps_ray_and_rebuilds_wordmark_on_resize():
    assert _apply()

    async def exercise():
        app = _app()
        async with app.run_test(size=(32, 35)) as pilot:
            await pilot.pause()
            widget = app.query_one(welcome.WelcomeBanner)
            rendered = widget.render().plain
            assert _boot.MANTA_RAY_UNICODE.splitlines()[1] in rendered
            assert _boot.MANTA_WORDMARK_UNICODE.splitlines()[-1] not in rendered
            assert "Manta  v9.8.7" in rendered
            await pilot.resize_terminal(100, 35)
            await pilot.pause()
            assert _boot.MANTA_WORDMARK_UNICODE.splitlines()[-1] in widget.render().plain
            await pilot.resize_terminal(32, 35)
            await pilot.pause()
            assert _boot.MANTA_WORDMARK_UNICODE.splitlines()[-1] not in widget.render().plain

    asyncio.run(exercise())


def test_ascii_widget_has_ascii_ray_and_wordmark(monkeypatch):
    monkeypatch.setattr(config, "_charset_mode_cache", config.CharsetMode.ASCII)
    assert _apply()

    async def exercise():
        app = _app()
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            rendered = app.query_one(welcome.WelcomeBanner).render().plain
            assert "<##=======##>" in rendered
            assert _boot.MANTA_WORDMARK_ASCII.splitlines()[-1] in rendered
            assert "Manta  v9.8.7" in rendered
            rendered.encode("ascii")

    asyncio.run(exercise())


def test_metadata_links_and_copy_spans_survive_model_and_connection_updates(monkeypatch):
    monkeypatch.setenv(welcome.SPLASH_SHOW_MODEL, "1")
    monkeypatch.setenv(welcome.SPLASH_SHOW_CWD, "1")
    assert _apply()

    async def exercise():
        app = _app()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            widget = app.query_one(welcome.WelcomeBanner)
            widget._project_name = "manta-project"
            widget._project_urls = {"manta-project": "https://trace.example.com/project"}
            widget._show_thread_id = True
            widget.update_model(provider="databricks", model="system.ai.claude-sonnet-5-5")
            widget.set_connected(7, mcp_unauthenticated=2)
            widget.update_version("99.99.99")
            await pilot.pause()
            rendered = widget.render()
            assert "databricks:system.ai.claude-sonnet-5-5" in rendered.plain
            assert "/tmp/manta-branding-test" in rendered.plain
            assert "7 tools" in rendered.plain
            assert "2 MCP servers need login" in rendered.plain
            assert "Manta  v9.8.7" in rendered.plain
            assert "99.99.99" not in rendered.plain
            assert any(
                getattr(span.style, "link", "").startswith("https://trace.example.com")
                for span in rendered.spans
                if getattr(span.style, "link", None)
            )
            assert any(
                copy_span_target(span.style) == ("thread-123", "Thread ID")
                for span in rendered.spans
            )
            assert rendered.plain.count(_boot.MANTA_RAY_UNICODE.splitlines()[1]) == 1

    asyncio.run(exercise())


def test_hidden_version_stays_hidden_after_live_update(monkeypatch):
    monkeypatch.setenv(welcome.HIDE_SPLASH_VERSION, "1")
    assert _apply()

    async def exercise():
        app = _app()
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            widget = app.query_one(welcome.WelcomeBanner)
            widget.update_version("99.99.99")
            await pilot.pause()
            rendered = widget.render().plain
            assert "Manta" in rendered
            assert "9.8.7" not in rendered
            assert "99.99.99" not in rendered

    asyncio.run(exercise())


def test_reapplying_branding_does_not_duplicate_art_and_names_terminal_manta():
    assert _apply()
    assert _apply()
    assert _invocation.invoked_name() == "manta"
    terminal_title = TerminalTitle("{app_name} - {thread_name}")
    assert terminal_title._render(thread_name="Unity session", cwd="", branch="") == (
        "manta - Unity session"
    )

    async def exercise():
        app = _app()
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            rendered = app.query_one(welcome.WelcomeBanner).render().plain
            assert rendered.count(_boot.MANTA_RAY_UNICODE.splitlines()[1]) == 1
            assert rendered.count("Manta  v9.8.7") == 1

    asyncio.run(exercise())


def test_missing_live_widget_contract_is_reported(monkeypatch):
    monkeypatch.setattr(welcome.WelcomeBanner, "_build_banner", None)
    assert _apply() is False
