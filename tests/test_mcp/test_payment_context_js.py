"""payment_context's page scripts, run in a real (headless Chromium) DOM.

The flow tests fake the page, so they cannot see what a script does. These run
the scripts themselves on local HTML (set_content: no network, no profile, no
display). Skipped where Playwright's Chromium is not installed (as in CI).
"""

from __future__ import annotations

import pytest

from genesis.mcp.health import payment_context as pc


@pytest.fixture(scope="module")
def page():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright is not installed")
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001 - no browser binary here
            pytest.skip(f"no Playwright Chromium ({type(e).__name__})")
        ctx = browser.new_context()
        ctx.add_init_script(pc.MASK_INIT_JS)
        yield ctx.new_page()
        browser.close()


def _el(page, html: str, sel: str):
    page.set_content(html)
    return page.query_selector(sel)


@pytest.mark.parametrize(
    ("html", "kind", "tag"),
    [
        ('<input id=x type=password autocomplete="cc-csc">', "cvc", "input"),
        ('<input id=x type=password autocomplete="cc-number">', "number", ""),
        ('<input id=x autocomplete="cc-number">', "number", "input"),
        ("<label for=x>Card number</label><input id=x type=tel>", "number", "input"),
        ('<input id=x type=hidden autocomplete="cc-csc">', "cvc", ""),
    ],
)
def test_card_field_js(page, html, kind, tag):
    el = _el(page, html, "#x")
    assert el.evaluate(pc.CARD_FIELD_JS, kind) == tag


@pytest.mark.parametrize(
    ("html", "button", "get"),
    [
        ("<form method=post><button id=x>Pay $5</button></form>", True, False),
        ("<form method=get><button id=x>Pay $5</button></form>", True, True),
        ("<form method=post><button id=x formmethod=get>Pay $5</button></form>", True, True),
        ("<button id=x type=reset>Pay $5</button>", False, False),
        ("<input id=x type=submit value='Pay $5'>", True, False),
    ],
)
def test_pay_js(page, html, button, get):
    info = (_el(page, html, "#x")).evaluate(pc.PAY_JS)
    assert (info["button"], info["get"], info["label"].strip()) == (button, get, "Pay $5")


def test_the_mask_survives_a_page_script_that_removes_it_during_input(page):
    page.set_content(
        "<input id=x><script>document.getElementById('x').addEventListener('input', () => {"
        " document.getElementById('__genesis_payment_mask__').remove();"
        " document.documentElement.removeAttribute('style'); });</script>"
    )
    assert page.evaluate(pc._MASK_CHECK_JS) is True
    page.fill("#x", "1")
    # By the time any later task (a paint, this evaluate) runs, the observer has restored it.
    assert page.evaluate(pc._MASK_CHECK_JS) is True
    assert page.evaluate("() => getComputedStyle(document.documentElement).opacity") == "0"


def test_extra_value_js_reads_a_select_by_value_and_label(page):
    el = _el(page, "<select id=x><option value=FR>France</option></select>", "#x")
    assert el.evaluate(pc.EXTRA_VALUE_JS) == ["FR", "France"]


def test_the_mask_survives_a_media_attribute_on_its_style(page):
    page.set_content("<input id=x>")
    page.evaluate("() => document.getElementById('__genesis_payment_mask__').media = 'print'")
    assert page.evaluate(pc._MASK_CHECK_JS) is True
