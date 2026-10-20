"""Manta branding for the interactive runtime's live welcome widget.

Recent Deep Agents releases render ``WelcomeBanner`` directly and no longer
read the old ``config.get_banner`` artwork. Keep their status rows, links, and
update behavior, while replacing the product heading and adding Manta's art.
"""

from __future__ import annotations

import os
from functools import wraps
from typing import Any


def apply_tui_branding(
    *,
    unicode_banner: str,
    ascii_banner: str,
    unicode_ray: str,
    ascii_ray: str,
    version: str,
) -> bool:
    """Brand the actual startup widget, app header, and terminal command name.

    Wide terminals show the ray and full wordmark. Narrow terminals retain the
    compact ray and product heading without wrapping or mangling the wordmark.
    Upstream's own rendered metadata is retained as styled ``Content`` so its
    tracing links and thread-copy spans survive model and connection updates.

    Return ``False`` when the supported upstream widget contract is absent.
    Reapplying this function replaces our wrapper rather than nesting it.
    """
    try:
        from deepagents_code import _invocation
        from deepagents_code.app import DeepAgentsApp
        from deepagents_code.tui.widgets import welcome
        from rich.cells import cell_len
        from textual.content import Content
    except ImportError:
        return False

    current = getattr(welcome.WelcomeBanner, "_build_banner", None)
    if not callable(current) or not hasattr(DeepAgentsApp, "TITLE"):
        return False
    original = getattr(current, "__manta_original_banner__", current)

    @wraps(original)
    def build_banner(self: Any) -> Content:
        # ``update_version`` reports an upstream runtime update. The product
        # heading always identifies the running Manta release instead.
        self._version = version
        rendered = original(self)
        heading_end = rendered.plain.find("\n")
        if heading_end < 0:
            heading_end = len(rendered.plain)
        product_start = rendered.plain.find("dcode", 0, heading_end)
        if product_start >= 0:
            rendered = Content.assemble(
                rendered[:product_start],
                ("Manta", "bold"),
                rendered[product_start + len("dcode") :],
            )

        is_ascii = welcome.is_ascii_mode()
        full_art = ascii_banner if is_ascii else unicode_banner
        compact_art = ascii_ray if is_ascii else unicode_ray
        # Static is constructed before it has a layout size. The application
        # viewport already exists then; reserve its border and horizontal pad.
        width = self.content_size.width if getattr(self, "_parent", None) is not None else 0
        width = width or max(0, self.app.size.width - 6)
        art = full_art.strip("\n")
        if width and max(map(cell_len, art.splitlines()), default=0) > width:
            art = compact_art.strip("\n")
        if width and max(map(cell_len, art.splitlines()), default=0) > width:
            art = ""
        if not art:
            return rendered
        colors = welcome.theme.get_theme_colors(self)
        style = "bold" if self.app.theme in welcome._ANSI_THEMES else colors.primary
        return Content.assemble((art, style), "\n\n", rendered)

    build_banner.__manta_original_banner__ = original
    welcome.WelcomeBanner._build_banner = build_banner

    current_resize = getattr(welcome.WelcomeBanner, "on_resize", None)
    original_resize = getattr(current_resize, "__manta_original_resize__", current_resize)

    def on_resize(self: Any, event: Any) -> None:
        if original_resize is not None:
            original_resize(self, event)
        self.update(self._build_banner())

    on_resize.__manta_original_resize__ = original_resize
    welcome.WelcomeBanner.on_resize = on_resize
    DeepAgentsApp.TITLE = "Manta"

    # Manta launches upstream through ``python -m manta_code._boot``. Without
    # this supported override upstream infers ``dcode`` from that module path,
    # putting the wrong command in terminal titles and resume instructions.
    os.environ["DEEPAGENTS_CODE_INVOKED_AS"] = "manta"
    _invocation.invoked_name.cache_clear()
    return True
