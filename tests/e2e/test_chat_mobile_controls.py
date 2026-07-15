"""Mobile chat floating-control layout regressions."""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api",
                    reason="install with: uv add --group dev pytest-playwright")
from playwright.sync_api import Page, expect  # noqa: E402


def _login(page: Page, base: str, token: str) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(base, wait_until="domcontentloaded")
    page.wait_for_selector(".login, .chat-tabs-list", state="visible", timeout=5000)
    if page.locator(".login").is_visible():
        page.fill('.login input[type="password"]', token)
        page.keyboard.press("Enter")
    expect(page.locator(".chat-tabs-list")).to_be_visible(timeout=5000)
    page.wait_for_function(
        """() => {
          const app = document.querySelector('#app')?._x_dataStack?.[0];
          return app && app.authed && app.appReady && app._sessionsInitialized;
        }"""
    )


def test_mobile_chat_navigation_buttons_form_a_visible_stack(
        page: Page, backend_url, auth_token):
    _login(page, backend_url, auth_token)
    layout = page.evaluate(
        """async () => {
          const app = document.querySelector('#app')._x_dataStack[0];
          app.mobileTab = 'chat';
          app.atBottom = false;
          await new Promise(resolve => requestAnimationFrame(
            () => requestAnimationFrame(resolve)));
          const wrap = document.querySelector('.chat-scroll-wrap');
          const transcript = document.querySelector('.chat-transcript-wrap');
          const input = document.querySelector('.chat-input');
          const read = selector => {
            const el = document.querySelector(selector);
            const r = el?.getBoundingClientRect();
            const cs = el && getComputedStyle(el);
            return el && r ? {
              display: cs.display, visibility: cs.visibility,
              x: r.x, y: r.y, width: r.width, height: r.height,
              bottom: r.bottom,
            } : null;
          };
          return {
            wrap: wrap?.getBoundingClientRect().toJSON(),
            transcript: transcript?.getBoundingClientRect().toJSON(),
            input: input?.getBoundingClientRect().toJSON(),
            down: read('.chat-body > .jump-bottom'),
            outline: read('.chat-body > .chat-outline-fab'),
            up: read('.chat-body > .chat-prevuser-fab'),
          };
        }"""
    )
    for name in ("down", "outline", "up"):
        assert layout[name]
        assert layout[name]["display"] != "none"
        assert layout[name]["visibility"] == "visible"
        assert layout[name]["width"] >= 36
        assert layout[name]["height"] >= 36

    # All controls stay inside transcript space, above the variable-height
    # composer, and form a non-overlapping top→bottom stack.
    assert layout["up"]["y"] < layout["outline"]["y"] < layout["down"]["y"]
    assert layout["down"]["bottom"] <= layout["input"]["top"]
    assert layout["up"]["y"] >= layout["transcript"]["top"]
    assert layout["up"]["bottom"] <= layout["outline"]["y"]
    assert layout["outline"]["bottom"] <= layout["down"]["y"]
