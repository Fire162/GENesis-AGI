"""browser_fill_card's flow (genesis.mcp.health.payment_context) against a fake
vault socket and a fake two-context browser, plus its browser.py hooks.

The vault side is a real unix socket speaking vault_service's protocol (one
JSON line each way), scripted per test; the release state machine has its own
tests (tests/test_payments). The browser is a small fake of the Playwright
surface the flow uses: a main context whose ``browser.new_context`` makes the
payment context, frames, locators, element handles, fill, select_option,
input_value and click. A live run is the harness E2E, not here.

The only card number is Stripe's public test Visa. The CVC, name and postal
code are generated per run.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import secrets
import string
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.browser.types import BrowserLayer
from genesis.mcp.health import browser
from genesis.mcp.health import payment_context as pc
from genesis.payments import card_fill as cf
from genesis.payments import release_client

PAN = "4242424242424242"
CARD_REF = "card_" + "ab12" * 3
# frozen-clock-ok: 3-year margin; the test expiry stays valid for any run shorter than that
YEAR = datetime.now(UTC).year + 3
CVC = "".join(secrets.choice(string.digits[1:]) for _ in range(3))
NAME = "Zq" + "".join(secrets.choice(string.ascii_lowercase) for _ in range(8))
POSTAL = "Q" + "".join(secrets.choice(string.digits) for _ in range(4)) + "X"
EMAIL = "zq" + "".join(secrets.choice(string.ascii_lowercase) for _ in range(6)) + "@example.org"
SECRETS = (PAN, "4242 4242 4242 4242", CVC, NAME, POSTAL, EMAIL, f"12/{YEAR % 100:02d}")
SHOP = "https://shop.example.com/checkout?step=pay"
CARDS = "https://js.cards.example.net/frame"


def _sweep(*blobs) -> None:
    for blob in blobs:
        text = blob if isinstance(blob, str) else json.dumps(blob, default=str)
        for s in SECRETS:
            assert s not in text, f"a value reached a result or log ({s[:2]}..)"


# ---------------------------------------------------------------------------
# The fake browser
# ---------------------------------------------------------------------------


class El:
    def __init__(self, tag="input", kinds=(), options=(), child=None, pay=None, extra=None):
        self.tag, self.kinds, self.options, self.child = tag, set(kinds), list(options), child
        self.pay = pay  # PAY_JS's answer, for a Pay control
        self.extra = extra  # EXTRA_FIELD_JS's answer
        self.value = ""
        self.mangle = False  # the page shows something other than what was typed
        self.on_click = None


class Handle:
    def __init__(self, el):
        self.el = el

    async def content_frame(self):
        return self.el.child

    async def dispose(self):
        pass

    async def evaluate(self, js, arg=None):
        if js is pc.CARD_FIELD_JS:
            return self.el.tag if arg in self.el.kinds else ""
        if js is pc.PAY_JS:
            return dict(self.el.pay)
        if js is pc.EXTRA_FIELD_JS:
            return self.el.extra or self.el.tag
        if js is pc._SAME_JS:
            return arg.el is self.el
        if js is pc.EXTRA_VALUE_JS:
            return [self.el.value]
        raise AssertionError("unexpected script")

    async def fill(self, value, timeout=None):
        self.el.value = "x" if self.el.mangle else value

    async def select_option(self, timeout=None, value=None, label=None):
        want = value if value is not None else label
        if want not in self.el.options:
            raise RuntimeError("no option")
        self.el.value = want

    async def input_value(self, timeout=None):
        return self.el.value

    async def click(self, timeout=None):
        if self.el.on_click is not None:
            self.el.on_click()


class Loc:
    def __init__(self, frame, sel):
        self.frame, self.sel = frame, sel

    async def count(self):
        return len(self.frame.elements.get(self.sel, []))

    async def element_handle(self, timeout=None):
        return Handle(self.frame.elements[self.sel][0])


class Frame:
    def __init__(self, url, elements=None, text=""):
        self.url, self.elements, self.text = url, elements or {}, text
        self.masked = True
        self.session: dict = {}

    def locator(self, sel):
        return Loc(self, sel)

    async def evaluate(self, js):
        if js == pc._MASK_CHECK_JS:
            return self.masked
        if js == pc._TEXT_JS:
            return self.text
        if js == pc._SESSION_JS:
            return dict(self.session)
        raise AssertionError("unexpected script")


class Page:
    def __init__(self, url, frames=(), title="Checkout"):
        self.url = url
        self.main_frame = Frame(url)
        self.extra = list(frames)
        self._closed = False
        self._title = title
        self.handlers: dict = {}
        self.goto_to: str | None = None  # where goto lands (a redirect)
        self.context = None

    @property
    def frames(self):
        return [self.main_frame, *self.extra]

    def on(self, event, fn):
        self.handlers.setdefault(event, []).append(fn)

    def is_closed(self):
        return self._closed

    async def title(self):
        return self._title

    async def goto(self, url, **_kw):
        self.url = self.main_frame.url = self.goto_to or url

    def navigate(self, url):
        self.url = self.main_frame.url = url
        for fn in self.handlers.get("framenavigated", []):
            fn(self.main_frame)


class PCtx:
    def __init__(self, page, log):
        self.page, self.log = page, log
        self.handlers: dict = {}
        self.closed = False
        self.close_fails = False
        page.context = self

    def on(self, event, fn):
        self.handlers.setdefault(event, []).append(fn)

    async def add_init_script(self, js):
        self.log.append(("init_script", js))
        log = self.log

        class Disposable:
            async def dispose(self):
                log.append(("dispose", js))

        return Disposable()

    async def new_page(self):
        self.log.append(("new_page",))
        for fn in self.handlers.get("page", []):  # as Playwright: new_page() fires it too
            fn(self.page)
        return self.page

    async def close(self):
        if self.close_fails:
            raise RuntimeError("close failed")
        self.closed = True
        self.page._closed = True


class MainCtx:
    def __init__(self, pctx):
        self.pctx = pctx
        self.new_context_kwargs = None
        self.state = {
            "cookies": [
                {"name": "cart", "domain": ".example.com", "value": "c1"},
                {"name": "m", "domain": "js.cards.example.net", "value": "c2"},
                {"name": "other", "domain": ".unrelated.org", "value": "c3"},
            ],
            "origins": [
                {
                    "origin": "https://shop.example.com",
                    "localStorage": [{"name": "k", "value": "v"}],
                },
                {"origin": "https://unrelated.org", "localStorage": [{"name": "k", "value": "v"}]},
            ],
        }
        outer = self

        class _Browser:
            async def new_context(self, **kw):
                outer.new_context_kwargs = kw
                return outer.pctx

        self.browser = _Browser()

    async def storage_state(self, indexed_db=False):
        assert indexed_db is True
        self.exported = True
        return json.loads(json.dumps(self.state))


def checkout(select_expiry=False):
    """Main and payment pages with the number and CVC in a cross-origin iframe,
    the rest at top level, and a Pay button. Returns a namespace of parts."""

    def build():
        card_frame = Frame(CARDS)
        els = {
            "number": El(kinds={"number"}),
            "cvc": El(kinds={"cvc"}),
            "name": El(kinds={"name"}),
            "pay": El(
                tag="button",
                pay={"button": True, "get": False, "live": True, "label": "Pay $19.99"},
            ),
            "email": El(),
        }
        card_frame.elements = {"#num": [els["number"]], "#cvc": [els["cvc"]]}
        page = Page(SHOP, frames=[card_frame])
        page.main_frame.text = "Order total $19.99"
        page.main_frame.elements = {
            "iframe#card": [El(tag="iframe", child=card_frame)],
            "#name": [els["name"]],
            "#pay": [els["pay"]],
            "#email": [els["email"]],
        }
        if select_expiry:
            els["exp_month"] = El(
                tag="select", kinds={"exp_month"}, options=[str(m) for m in range(1, 13)]
            )
            els["exp_year"] = El(
                tag="select", kinds={"exp_year"}, options=[str(YEAR + i) for i in range(5)]
            )
            page.main_frame.elements["#mm"] = [els["exp_month"]]
            page.main_frame.elements["#yy"] = [els["exp_year"]]
        else:
            els["expiry"] = El(kinds={"expiry"})
            card_frame.elements["#exp"] = [els["expiry"]]
        return page, els

    main_page, _main_els = build()
    ppage, els = build()
    ppage.url = ppage.main_frame.url = "about:blank"
    log: list = []
    pctx = PCtx(ppage, log)
    main = MainCtx(pctx)
    fields = {
        "number": {"selector": "#num", "frames": ["iframe#card"]},
        "cvc": {"selector": "#cvc", "frames": ["iframe#card"]},
        "name": "#name",
    }
    if select_expiry:
        fields.update(exp_month="#mm", exp_year="#yy")
    else:
        fields["expiry"] = {"selector": "#exp", "frames": ["iframe#card"]}
    els["pay"].on_click = lambda: ppage.navigate("https://shop.example.com/done")

    class NS:
        pass

    ns = NS()
    ns.main_page, ns.ppage, ns.pctx, ns.main, ns.els, ns.fields, ns.log = (
        main_page,
        ppage,
        pctx,
        main,
        els,
        fields,
        log,
    )
    return ns


# ---------------------------------------------------------------------------
# The fake vault
# ---------------------------------------------------------------------------


class Vault:
    def __init__(self, path: Path):
        self.path = path
        self.requests: list[dict] = []
        self.statuses: list[dict] = [{"ok": True, "state": "granted", "expires_in_s": 120}]
        self.request_reply: dict = {"ok": True, "nonce": "nonce-1"}
        self.claim_reply: dict | None = None
        self.half_closed: list[str] = []
        self.on_status = None
        self.on_claim = None
        self.server = None

    def ops(self, op):
        return [r for r in self.requests if r.get("op") == op]

    def card(self, keys=None):
        card = {
            "number": PAN,
            "exp_month": 12,
            "exp_year": YEAR,
            "cvc": CVC,
            "name": NAME,
            "postal_code": POSTAL,
            "label": "Test card",
            "last4": "4242",
        }
        req = self.ops("release_request")[-1]
        want = cf.reply_keys(req["field_origins"]) if keys is None else keys
        return {"ok": True, "card": {k: v for k, v in card.items() if k in want}}

    async def handle(self, reader, writer):
        req = json.loads(await reader.readline())
        self.requests.append(req)
        await asyncio.sleep(0)
        if reader.at_eof():  # the service reads a half-close as a gone client
            self.half_closed.append(req.get("op"))
        op = req.get("op")
        if op == "release_request":
            reply = self.request_reply
        elif op == "release_status":
            if self.on_status is not None:
                self.on_status()
            reply = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        elif op == "release_claim":
            if self.on_claim is not None:
                self.on_claim()
            reply = self.claim_reply if self.claim_reply is not None else self.card()
        elif op == "release_cancel":
            reply = {"ok": True}
        else:
            reply = {"ok": False, "error": "bad request"}
        writer.write((json.dumps(reply) + "\n").encode())
        await writer.drain()
        writer.close()


@pytest.fixture
async def vault(tmp_path, monkeypatch):
    v = Vault(tmp_path / "v.sock")
    v.server = await asyncio.start_unix_server(v.handle, path=str(v.path))
    monkeypatch.setattr(release_client, "vault_socket_path", lambda: v.path)
    yield v
    v.server.close()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(pc, "_flow", None)
    monkeypatch.setattr(pc, "_x_input", 0)
    monkeypatch.setattr(pc, "_prefs_pinned", True)
    for name, value in (
        ("POLL_S", 0.01),
        ("SETTLE_S", 0.3),
        ("NAV_GRACE_S", 0.05),
        ("TICK_S", 0.01),
    ):
        monkeypatch.setattr(pc, name, value)
    yield


@pytest.fixture
def logs():
    lines: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage() + repr(record.args) + repr(record.exc_info))

    h = Grab(level=logging.DEBUG)
    names = ("genesis.mcp.health.payment_context", "genesis.payments.release_client", "asyncio")
    for n in names:
        logging.getLogger(n).addHandler(h)
        logging.getLogger(n).setLevel(logging.DEBUG)
    yield lines
    for n in names:
        logging.getLogger(n).removeHandler(h)


class Hooks:
    def __init__(self):
        self.up = True
        self.close_browser = AsyncMock()
        self.interstitial = AsyncMock(return_value=False)

    def make(self):
        return pc.Hooks(
            alive=lambda: self.up,
            touch=lambda: None,
            interstitial=self.interstitial,
            close_browser=self.close_browser,
        )


def _args(ns, **over):
    kw = dict(
        card_ref=CARD_REF,
        amount="$19.99",
        fields=ns.fields,
        pay="#pay",
        expiry_format="MM/YY",
        extra_fields={"email": {"selector": "#email", "value": EMAIL}},
    )
    kw.update(over)
    return cf.validate(**kw)


async def _run(ns, hooks=None, report=None, **over):
    hooks = hooks or Hooks()
    out = await pc.fill_card(_args(ns, **over), ns.main, ns.main_page, hooks.make(), report)
    return out, hooks


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------


async def test_pays_in_an_isolated_context_and_returns_fixed_text(vault, logs):
    ns = checkout()
    progress: list[str] = []

    async def report(m):
        progress.append(m)

    vault.statuses = [{"ok": True, "state": "pending", "expires_in_s": 100}, vault.statuses[0]]
    out, _ = await _run(ns, report=report)

    assert out["code"] == "submitted_unverified"
    assert out["host"] == "shop.example.com"
    assert out["status"] == cf.TEXTS["submitted_unverified"]
    assert out["filled"] == ["number", "expiry", "cvc", "name"]
    assert set(out) == {"code", "status", "host", "filled"}
    # Typed in the payment context only; the main page is untouched.
    assert ns.els["number"].value == PAN and ns.els["cvc"].value == CVC
    assert ns.els["expiry"].value == f"12/{YEAR % 100:02d}"
    assert ns.els["email"].value == EMAIL
    assert all(e.value == "" for e in _main_values(ns))
    # The tap request: Pay's label, the extra field NAMES, the card fields' origins.
    req = vault.ops("release_request")[0]
    assert req["button_label"] == "Pay $19.99"
    assert req["also_filled"] == ["email"]
    assert req["field_origins"] == {
        "number": "https://js.cards.example.net",
        "expiry": "https://js.cards.example.net",
        "cvc": "https://js.cards.example.net",
        "name": "https://shop.example.com",
    }
    assert req["host"] == "shop.example.com" and req["amount"] == "$19.99"
    assert EMAIL not in json.dumps(req)
    # The context: mask script before the first page, viewport as the main launch,
    # merchant and card-frame storage only, closed at the end.
    assert ns.log[0] == ("init_script", pc.MASK_INIT_JS) and ns.log[1] == ("new_page",)
    kw = ns.main.new_context_kwargs
    assert kw["no_viewport"] is True and kw["accept_downloads"] is False
    assert sorted(c["name"] for c in kw["storage_state"]["cookies"]) == ["cart", "m"]
    assert [o["origin"] for o in kw["storage_state"]["origins"]] == ["https://shop.example.com"]
    assert ns.pctx.closed and not pc.in_flight()
    assert vault.ops("release_cancel") == [] and vault.half_closed == []
    assert progress and all("approval" in m for m in progress)
    _sweep(out, logs, progress)


def _main_values(ns):
    els = []
    for frame in ns.main_page.frames:
        for group in frame.elements.values():
            els += [e for e in group if e.tag != "iframe"]
    return els


async def test_select_expiry_is_matched_to_an_option(vault):
    ns = checkout(select_expiry=True)
    out, _ = await _run(ns)
    assert out["code"] == "submitted_unverified"
    assert ns.els["exp_month"].value == "12" and ns.els["exp_year"].value == str(YEAR)


async def test_a_challenge_frame_after_pay_closes_at_once(vault, logs):
    ns = checkout()

    def challenge():  # the page moves on within the site AND shows a bank frame
        ns.ppage.extra.append(Frame("https://acs.bank.example/x"))
        ns.ppage.navigate("https://shop.example.com/processing")

    ns.els["pay"].on_click = challenge
    out, _ = await _run(ns)
    assert out["code"] == "challenge"
    assert "do NOT retry" in out["status"]
    assert ns.pctx.closed and not pc.in_flight()
    _sweep(out, logs)


async def test_no_settle_is_a_challenge_never_not_paid(vault):
    ns = checkout()
    ns.els["pay"].on_click = None
    out, _ = await _run(ns)
    assert out["code"] == "challenge"


async def test_a_navigation_off_the_merchant_site_is_a_challenge(vault):
    ns = checkout()
    ns.els["pay"].on_click = lambda: ns.ppage.navigate("https://bank-verify.example.org/start")
    out, _ = await _run(ns)
    assert out["code"] == "challenge"


async def test_a_popup_after_pay_is_a_challenge(vault):
    ns = checkout()
    ns.els["pay"].on_click = lambda: [fn(Page("about:blank")) for fn in ns.pctx.handlers["page"]]
    out, _ = await _run(ns)
    assert out["code"] == "challenge"


async def test_the_browser_closing_after_pay_is_outcome_unknown(vault):
    ns = checkout()
    hooks = Hooks()

    def gone():
        hooks.up = False

    ns.els["pay"].on_click = gone
    out, _ = await _run(ns, hooks=hooks)
    assert out["code"] == "outcome_unknown"
    assert "do NOT retry" in out["status"]


async def test_a_click_that_raises_still_counts_as_paid(vault):
    ns = checkout()

    def boom():
        raise RuntimeError(PAN)

    ns.els["pay"].on_click = boom
    out, _ = await _run(ns)
    assert out["code"] == "challenge"  # never "not paid"
    _sweep(out)


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (
            lambda ns: setattr(ns.ppage.main_frame, "text", "Order total $119.99"),
            "amount_not_on_page",
        ),
        (lambda ns: ns.els["pay"].pay.update(get=True), "pay_get_form"),
        (lambda ns: ns.els["pay"].pay.update(button=False), "pay_not_button"),
        (
            lambda ns: setattr(ns.ppage, "goto_to", "https://evil.example.org/pay"),
            "checkout_redirected",
        ),
        (lambda ns: setattr(ns.ppage.main_frame, "masked", False), "checkout_load_failed"),
        (lambda ns: ns.els["number"].kinds.clear(), "field_mismatch:number"),
        (lambda ns: ns.ppage.main_frame.elements.pop("#pay"), "pay_not_found"),
        (lambda ns: ns.main_page.main_frame.elements.pop("iframe#card"), "frame_not_found:number"),
        (lambda ns: setattr(ns.els["email"], "extra", "card"), "extra_field_is_card_field"),
    ],
)
async def test_a_failed_check_before_the_tap_requests_nothing(vault, setup, code):
    ns = checkout()
    setup(ns)
    out, _ = await _run(ns)
    assert out["code"] == code
    assert vault.requests == []
    assert ns.els["number"].value == ""
    assert not pc.in_flight()


async def test_an_extra_field_that_is_a_card_field_element_is_refused(vault):
    ns = checkout()
    ns.ppage.main_frame.elements["#email"] = ns.ppage.main_frame.elements["#name"]
    out, _ = await _run(ns)
    assert out["code"] == "extra_field_is_card_field"


async def test_a_bot_check_is_refused(vault):
    ns = checkout()
    hooks = Hooks()
    hooks.interstitial.return_value = True
    out, _ = await _run(ns, hooks=hooks)
    assert out["code"] == "bot_check" and vault.requests == []


@pytest.mark.parametrize(
    ("state", "code"), [("refused", "not_approved"), ("expired", "release_expired")]
)
async def test_no_grant_fills_nothing(vault, state, code):
    ns = checkout()
    vault.statuses = [{"ok": True, "state": state, "expires_in_s": 0}]
    out, _ = await _run(ns)
    assert out["code"] == code
    assert vault.ops("release_claim") == [] and ns.els["number"].value == ""


async def test_a_vault_refusal_passes_only_a_fixed_reason(vault):
    ns = checkout()
    vault.request_reply = {"ok": False, "error": "no such active card"}
    out, _ = await _run(ns)
    assert out == cf.result("release_refused", "shop.example.com", reason="no such active card")
    vault.request_reply = {"ok": False, "error": f"odd {PAN}"}
    out, _ = await _run(ns)
    assert "reason" not in out
    vault.request_reply = {"ok": False, "error": "the approval text was refused"}
    out, _ = await _run(ns)
    assert out["code"] == "approval_text_refused"


async def test_a_page_that_moved_while_waiting_cancels_the_release(vault):
    ns = checkout()
    vault.on_status = lambda: setattr(ns.ppage.extra[0], "url", "https://evil.example.org/f")
    out, _ = await _run(ns)
    assert out["code"] == "page_changed"
    assert vault.ops("release_claim") == [] and len(vault.ops("release_cancel")) == 1


async def test_the_payment_window_closing_while_waiting_cancels(vault):
    ns = checkout()
    vault.statuses = [{"ok": True, "state": "pending", "expires_in_s": 100}]
    vault.on_status = lambda: setattr(ns.ppage, "_closed", True)
    out, _ = await _run(ns)
    assert out["code"] == "checkout_closed" and len(vault.ops("release_cancel")) == 1


async def test_a_page_that_moved_during_the_claim_types_nothing(vault):
    ns = checkout()
    vault.on_claim = lambda: setattr(ns.ppage, "url", "https://evil.example.org/x")
    out, _ = await _run(ns)
    assert out["code"] == "claimed_not_filled"
    assert ns.els["number"].value == "" and vault.ops("release_cancel") == []


@pytest.mark.parametrize("change", ["fewer", "more"])
async def test_a_reply_with_other_fields_than_tapped_types_nothing(vault, change):
    ns = checkout()

    def reply():
        card = vault.card()["card"]
        if change == "fewer":
            card.pop("cvc")
        else:
            card["postal_code"] = POSTAL
        vault.claim_reply = {"ok": True, "card": card}

    vault.on_status = reply
    out, _ = await _run(ns)
    assert out["code"] == "claimed_not_filled" and ns.els["number"].value == ""


async def test_a_value_that_does_not_land_stops_before_pay(vault, logs):
    ns = checkout()
    ns.els["cvc"].mangle = True
    clicked = []
    ns.els["pay"].on_click = lambda: clicked.append(1)
    out, _ = await _run(ns)
    assert out["code"] == "failed_before_pay"
    assert out["field"] == "cvc" and out["filled"] == ["number", "expiry", "cvc"]
    assert clicked == [] and ns.pctx.closed
    _sweep(out, logs)


async def test_pay_disabled_after_filling_stops_before_pay(vault):
    ns = checkout()
    vault.on_claim = lambda: ns.els["pay"].pay.update(live=False)
    out, _ = await _run(ns)
    assert out["code"] == "failed_before_pay" and "field" not in out


async def test_an_unanswered_claim_is_not_cancelled(vault):
    ns = checkout()

    def stop():
        vault.server.close()
        raise ConnectionError

    vault.on_claim = stop
    out, _ = await _run(ns)
    assert out["code"] == "claim_unanswered"
    assert vault.ops("release_cancel") == []


async def test_a_refused_claim_carries_the_reason(vault):
    ns = checkout()
    vault.claim_reply = {"ok": False, "error": "requester gone"}
    out, _ = await _run(ns)
    assert out["code"] == "claim_refused" and out["reason"] == "requester gone"


async def test_one_payment_at_a_time_and_turnstile_excluded(vault):
    ns = checkout()
    gate = asyncio.Event()
    vault.statuses = [{"ok": True, "state": "pending", "expires_in_s": 100}]
    vault.on_status = gate.set
    task = asyncio.ensure_future(_run(ns))
    await gate.wait()
    assert pc.in_flight() and not pc.past_claim()
    out, _ = await _run(checkout())
    assert out["code"] == "payment_in_flight"
    with pc.try_x_input() as ok:
        assert ok is False
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not pc.in_flight()
    with pc.try_x_input() as ok:
        assert ok is True
        out, _ = await _run(checkout())
        assert out["code"] == "turnstile_in_progress"


async def test_a_cancelled_call_still_cancels_the_release_and_closes(vault):
    ns = checkout()
    gate = asyncio.Event()
    vault.statuses = [{"ok": True, "state": "pending", "expires_in_s": 100}]
    vault.on_status = gate.set
    task = asyncio.ensure_future(_run(ns))
    await gate.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(vault.ops("release_cancel")) == 1 and ns.pctx.closed


async def test_abort_ends_the_flow_with_a_result_and_cancels(vault):
    ns = checkout()
    gate = asyncio.Event()
    vault.statuses = [{"ok": True, "state": "pending", "expires_in_s": 100}]
    vault.on_status = gate.set
    task = asyncio.ensure_future(_run(ns))
    await gate.wait()
    await pc.abort("mcp_exit")
    out, _ = await task
    assert out["code"] == "browser_closed"
    assert len(vault.ops("release_cancel")) == 1 and ns.pctx.closed and not pc.in_flight()


@pytest.mark.parametrize(
    ("hook", "change", "code"),
    [
        ("on_status", lambda ns: ns.els["pay"].pay.update(label="Pay $99.99"), "page_changed"),
        ("on_status", lambda ns: setattr(ns.ppage.main_frame, "text", "Total $29.99"), "page_changed"),
        ("on_claim", lambda ns: setattr(ns.ppage.main_frame, "text", "Total $29.99"), "claimed_not_filled"),
        ("on_claim", lambda ns: setattr(ns.ppage.extra[0], "masked", False), "claimed_not_filled"),
    ],
)
async def test_each_check_after_the_tap_holds(vault, hook, change, code):
    ns = checkout()
    clicked = []
    ns.els["pay"].on_click = lambda: clicked.append(1)
    setattr(vault, hook, lambda: change(ns))
    out, _ = await _run(ns)
    assert out["code"] == code and clicked == []


async def test_the_mask_is_checked_before_each_field(vault):
    ns = checkout()

    real = Handle.fill

    async def fill(self, value, timeout=None):
        await real(self, value, timeout)
        if self.el is ns.els["number"]:
            ns.ppage.main_frame.masked = False

    Handle.fill = fill
    try:
        out, _ = await _run(ns)
    finally:
        Handle.fill = real
    assert out["code"] == "failed_before_pay" and out["field"] == "expiry"
    assert ns.els["expiry"].value == ""


async def test_a_frame_moved_before_pay_stops_before_pay(vault):
    ns = checkout()
    real = Handle.fill

    async def fill(self, value, timeout=None):
        await real(self, value, timeout)
        if self.el is ns.els["name"]:  # the last field: only the Pay check is left
            ns.ppage.extra[0].url = "https://evil.example.org/f"

    Handle.fill = fill
    try:
        out, _ = await _run(ns)
    finally:
        Handle.fill = real
    assert out["code"] == "failed_before_pay" and "field" not in out


async def test_a_challenge_like_frame_present_before_pay_is_ignored(vault):
    ns = checkout()
    ns.ppage.extra.append(Frame("https://hooks.stripe.com/widget"))
    out, _ = await _run(ns)
    assert out["code"] == "submitted_unverified"


async def _filling(ns, last_kind, change):
    """Run with ``change`` applied right after ``last_kind`` is typed."""
    real = Handle.fill

    async def fill(self, value, timeout=None):
        await real(self, value, timeout)
        if self.el is ns.els[last_kind]:
            change()

    Handle.fill = fill
    try:
        return (await _run(ns))[0]
    finally:
        Handle.fill = real


@pytest.mark.parametrize(
    "change",
    [
        lambda ns: ns.els["pay"].pay.update(label="Pay $999.99"),
        lambda ns: ns.els["pay"].pay.update(button=False),
        lambda ns: setattr(ns.els["email"], "value", "other@example.org"),
        lambda ns: setattr(ns.ppage.main_frame, "text", "Total $29.99"),
        lambda ns: setattr(ns.ppage.extra[0], "url", "https://evil.example.org/f"),
    ],
)
async def test_pay_and_extras_are_rechecked_right_before_pay(vault, change):
    ns = checkout()
    clicked = []
    ns.els["pay"].on_click = lambda: clicked.append(1)
    out = await _filling(ns, "name", lambda: change(ns))
    assert out["code"] == "failed_before_pay" and clicked == []


async def test_an_extra_field_changed_while_waiting_cancels(vault):
    ns = checkout()
    vault.on_status = lambda: setattr(ns.els["email"], "value", "")
    out, _ = await _run(ns)
    assert out["code"] == "page_changed" and vault.ops("release_claim") == []


async def test_an_extra_field_that_does_not_keep_its_value_requests_nothing(vault):
    ns = checkout()
    ns.els["email"].mangle = True
    out, _ = await _run(ns)
    assert out["code"] == "extra_field_failed" and vault.requests == []


async def test_two_extra_names_on_one_element_are_refused(vault):
    ns = checkout()
    out, _ = await _run(
        ns,
        extra_fields={
            "email": {"selector": "#email", "value": EMAIL},
            "company": {"selector": "#email", "value": "Acme"},
        },
    )
    assert out["code"] == "extra_field_not_found" and vault.requests == []


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (lambda ns: setattr(ns.ppage, "goto_to", "http://shop.example.com/checkout"), "not_https"),
        (lambda ns: setattr(ns.ppage.extra[0], "url", "http://js.cards.example.net/f"), "field_origin:number"),
        (lambda ns: setattr(ns.main, "browser", None), "checkout_load_failed"),
    ],
)
async def test_plain_http_and_no_browser_are_refused(vault, setup, code):
    ns = checkout()
    setup(ns)
    out, _ = await _run(ns)
    assert out["code"] == code and vault.requests == []
    assert ns.els["number"].value == ""
    if code == "checkout_load_failed":  # refused before anything is read or created
        assert not getattr(ns.main, "exported", False)


async def test_a_merchant_hosted_bank_check_is_a_challenge(vault):
    ns = checkout()
    ns.els["pay"].on_click = lambda: ns.ppage.navigate("https://shop.example.com/pay/3ds/start")
    out, _ = await _run(ns)
    assert out["code"] == "challenge"


async def test_session_storage_is_handed_over_for_the_merchant_origin(vault):
    ns = checkout()
    ns.main_page.main_frame.session = {"cart": "c-1"}
    out, _ = await _run(ns)
    assert out["code"] == "submitted_unverified"
    scripts = [e[1] for e in ns.log if e[0] == "init_script"]
    assert scripts[0] == pc.MASK_INIT_JS and len(scripts) == 2
    assert '{"cart": "c-1"}' in scripts[1] and json.dumps(SHOP) in scripts[1]
    assert ("dispose", scripts[1]) in ns.log  # seeds the first document only
    assert ("dispose", scripts[0]) not in ns.log


async def test_progress_increases(monkeypatch):
    clock = [0.0]

    async def impl(*_a, report=None, **_k):
        for _ in range(3):
            clock[0] += 10
            await report("waiting")
        return {"code": "x"}

    sent = []
    ctx = MagicMock()
    ctx.report_progress = AsyncMock(side_effect=lambda p, message=None: sent.append(p))
    monkeypatch.setattr(browser, "_impl_browser_fill_card", impl)
    monkeypatch.setattr(browser, "time", MagicMock(monotonic=lambda: clock[0]))
    fn = getattr(browser.browser_fill_card, "fn", browser.browser_fill_card)
    await fn(CARD_REF, "$1", {"number": "#n"}, "#p", ctx=ctx)
    assert sent == [1, 2, 3]


async def test_abort_after_the_pay_click_is_outcome_unknown(vault):
    ns = checkout()
    ns.els["pay"].on_click = lambda: asyncio.get_running_loop().call_soon(
        lambda: asyncio.ensure_future(pc.abort("mcp_exit"))
    )
    out, _ = await _run(ns)
    assert out["code"] == "outcome_unknown" and ns.pctx.closed


async def test_a_context_that_will_not_close_closes_the_browser_first(vault):
    ns = checkout()
    ns.pctx.close_fails = True
    hooks = Hooks()
    hooks.close_browser.side_effect = lambda: setattr(hooks, "up", False)
    out, _ = await _run(ns, hooks=hooks)
    assert out["code"] == "submitted_unverified"
    hooks.close_browser.assert_awaited_once()  # awaited before the guard is released
    assert not pc.in_flight()


@pytest.mark.parametrize("fails", [False, True])
async def test_a_browser_that_will_not_close_keeps_payments_refused_while_it_lives(vault, fails):
    ns = checkout()
    ns.pctx.close_fails = True
    hooks = Hooks()
    if fails:
        hooks.close_browser.side_effect = RuntimeError("lock busy")
    out, _ = await _run(ns, hooks=hooks)  # the close does not detach the browser
    for _ in range(3):
        await asyncio.sleep(0)
    assert hooks.close_browser.await_count == 2  # tried again in the background
    assert pc.in_flight()
    with pc.try_x_input() as ok:
        assert ok is False
    again, _ = await _run(checkout())
    assert again["code"] == "payment_in_flight"
    hooks.up = False  # that browser is gone: the guard retires with it
    assert not pc.in_flight()


async def test_the_main_browser_restarting_before_the_claim_cancels(vault):
    ns = checkout()
    hooks = Hooks()
    vault.statuses = [{"ok": True, "state": "pending", "expires_in_s": 100}]
    vault.on_status = lambda: setattr(hooks, "up", False)
    out, _ = await _run(ns, hooks=hooks)
    assert out["code"] == "browser_closed" and len(vault.ops("release_cancel")) == 1
    hooks.close_browser.assert_not_awaited()


async def test_past_claim_holds_until_teardown_ends(vault):
    ns = checkout()
    seen = []
    ns.els["pay"].on_click = lambda: seen.append((pc.in_flight(), pc.past_claim()))
    await _run(ns)
    assert seen == [(True, True)] and not pc.past_claim()


async def test_is_payment_page_only_for_the_payment_context(vault):
    ns = checkout()
    seen = []
    ns.els["pay"].on_click = lambda: seen.append(
        (pc.is_payment_page(ns.ppage), pc.is_payment_page(ns.main_page))
    )
    await _run(ns)
    assert seen == [(True, False)]
    assert not pc.is_payment_page(ns.ppage)  # torn down


# ---------------------------------------------------------------------------
# browser.py: the entry point and the lifecycle hooks
# ---------------------------------------------------------------------------


@pytest.fixture
def camoufox(monkeypatch):
    page = MagicMock()
    page.url = SHOP
    page.is_closed.return_value = False
    ctx = MagicMock()
    monkeypatch.setattr(browser, "_active_page", page)
    monkeypatch.setattr(browser, "_stealth_page", page)
    monkeypatch.setattr(browser, "_stealth_browser", ctx)
    monkeypatch.setattr(browser, "_browser_lock", asyncio.Lock())
    monkeypatch.setattr(browser, "_layer_last_used", {})
    monkeypatch.setattr(
        browser, "_layer_of", lambda p: BrowserLayer.CAMOUFOX if p is page else None
    )
    monkeypatch.setattr(cf, "card_fill_enabled", lambda: True)
    monkeypatch.delenv("DEBUG", raising=False)
    return page, ctx


def _impl_args(**over):
    kw = dict(card_ref=CARD_REF, amount="$1", fields={"number": "#n"}, pay="#p")
    kw.update(over)
    return kw


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (lambda m: m.setattr(cf, "card_fill_enabled", lambda: False), "card_fill_disabled"),
        (lambda m: m.setenv("DEBUG", "pw:*"), "playwright_debug_on"),
        (lambda m: m.setattr(browser, "_active_page", None), "no_page"),
        (
            lambda m: m.setattr(browser, "_layer_of", lambda p: BrowserLayer.CHROMIUM),
            "not_camoufox",
        ),
        (lambda m: m.setattr(pc, "_prefs_pinned", False), "prefs_not_pinned"),
    ],
)
async def test_impl_prechecks(camoufox, monkeypatch, setup, code):
    run = AsyncMock()
    monkeypatch.setattr(pc, "fill_card", run)
    setup(monkeypatch)
    out = await browser._impl_browser_fill_card(**_impl_args())
    assert out["code"] == code
    run.assert_not_awaited()


async def test_impl_refuses_bad_arguments_and_hands_over_the_main_page(camoufox, monkeypatch):
    page, ctx = camoufox
    run = AsyncMock(return_value={"code": "x"})
    monkeypatch.setattr(pc, "fill_card", run)
    assert (await browser._impl_browser_fill_card(**_impl_args(pay=None)))["code"] == "bad_pay"
    assert (await browser._impl_browser_fill_card(**_impl_args()))["code"] == "x"
    _args, main_ctx, main_page, hooks, _report = run.await_args.args
    assert main_ctx is ctx and main_page is page and hooks.alive()
    monkeypatch.setattr(browser, "_stealth_browser", MagicMock())
    assert not hooks.alive()


async def test_impl_turns_an_exception_into_fixed_text(camoufox, monkeypatch):
    monkeypatch.setattr(pc, "fill_card", AsyncMock(side_effect=RuntimeError(PAN)))
    out = await browser._impl_browser_fill_card(**_impl_args())
    assert out["code"] == "internal_error"
    _sweep(out)


async def test_a_stale_restart_is_refused_past_the_claim(camoufox, monkeypatch):
    page, _ctx = camoufox
    page.is_closed.return_value = True
    cleanup = AsyncMock()
    monkeypatch.setattr(browser, "_cleanup_camoufox", cleanup)
    monkeypatch.setattr(pc, "_flow", pc._Flow(task=None, claimed=True))
    with pytest.raises(pc.PaymentInFlight):
        await browser._ensure_browser()
    cleanup.assert_not_awaited()
    monkeypatch.setattr(browser, "_get_page", AsyncMock(side_effect=pc.PaymentInFlight("x")))
    out = await browser._impl_browser_navigate("https://example.com")
    assert out["code"] == "payment_in_flight"


async def test_idle_reclaim_skips_camoufox_while_a_payment_runs(camoufox, monkeypatch):
    cleanup = AsyncMock()
    monkeypatch.setattr(browser, "_cleanup_camoufox", cleanup)
    monkeypatch.setattr(browser, "_layer_open", lambda layer: layer is BrowserLayer.CAMOUFOX)
    browser._layer_last_used[BrowserLayer.CAMOUFOX] = 0.0
    monkeypatch.setattr(pc, "_flow", pc._Flow(task=None))
    await browser._reclaim_idle_layers(browser._IDLE_TIMEOUT_S + 10)
    cleanup.assert_not_awaited()
    monkeypatch.setattr(pc, "_flow", None)
    await browser._reclaim_idle_layers(browser._IDLE_TIMEOUT_S + 10)
    cleanup.assert_awaited_once()


async def test_turnstile_x_clicks_stand_down_while_a_payment_runs(monkeypatch):
    click = AsyncMock(return_value=True)
    monkeypatch.setattr(browser, "_vnc_click_now", click)
    monkeypatch.setattr(pc, "_flow", pc._Flow(task=None))
    assert await browser._vnc_click_turnstile(MagicMock()) is False
    click.assert_not_awaited()
    monkeypatch.setattr(pc, "_flow", None)
    assert await browser._vnc_click_turnstile(MagicMock()) is True


async def test_get_page_refuses_a_payment_page(monkeypatch):
    page = MagicMock()
    monkeypatch.setattr(browser, "_ensure_vnc", AsyncMock())
    monkeypatch.setattr(browser, "_ensure_browser", AsyncMock(return_value=page))
    monkeypatch.setattr(browser, "_start_idle_watcher", lambda: None)
    monkeypatch.setattr(browser, "_active_page", None)
    monkeypatch.setattr(pc, "_flow", pc._Flow(task=None, pctx=page.context))
    with pytest.raises(RuntimeError):
        await browser._get_page()
    assert browser._active_page is None


async def test_the_escalation_closes_only_the_launch_the_payment_ran_on(monkeypatch):
    cleanup = AsyncMock()
    monkeypatch.setattr(browser, "_cleanup_camoufox", cleanup)
    monkeypatch.setattr(browser, "_browser_lock", asyncio.Lock())
    ctx = MagicMock()
    monkeypatch.setattr(browser, "_stealth_browser", MagicMock())  # relaunched since
    await browser._close_browser_for_payment(ctx)
    cleanup.assert_not_awaited()
    monkeypatch.setattr(browser, "_stealth_browser", ctx)
    await browser._close_browser_for_payment(ctx)
    cleanup.assert_awaited_once()


async def test_the_main_window_closing_aborts_the_payment_outside_the_lock(monkeypatch):
    ctx = MagicMock()
    seen = []

    async def abort(reason):
        seen.append((reason, browser._browser_lock.locked()))

    monkeypatch.setattr(pc, "abort", abort)
    monkeypatch.setattr(browser, "_stealth_browser", ctx)
    monkeypatch.setattr(browser, "_browser_lock", asyncio.Lock())
    monkeypatch.setattr(browser, "_pending_closes", set())
    monkeypatch.setattr(browser, "_cleanup_camoufox", AsyncMock())
    browser._on_local_context_closed(BrowserLayer.CAMOUFOX, ctx)
    await asyncio.wait(set(browser._pending_closes))
    assert seen == [("browser_closed", False)]


async def test_mcp_exit_aborts_the_payment_first(monkeypatch):
    order = []
    monkeypatch.setattr(pc, "abort", AsyncMock(side_effect=lambda r: order.append(("abort", r))))
    for name in (
        "_cleanup_remote_cdp",
        "_cleanup_tinyfish",
        "_cleanup_chromium",
        "_cleanup_camoufox",
    ):
        monkeypatch.setattr(browser, name, AsyncMock(side_effect=lambda n=name: order.append(n)))
    monkeypatch.setattr(browser, "_idle_task", None)
    monkeypatch.setattr(browser, "_pending_closes", set())
    await browser.async_cleanup()
    assert order[0] == ("abort", "mcp_exit")


async def test_a_launch_pins_card_fill_prefs_only_when_on(tmp_path, monkeypatch):
    import sys
    from unittest.mock import patch

    from genesis.browser import engine, profile_backup

    monkeypatch.setenv("DISPLAY", ":0")  # a launch sets it

    for on in (True, False):
        monkeypatch.setattr(browser, "_stealth_page", None)
        monkeypatch.setattr(browser, "_stealth_browser", None)
        monkeypatch.setattr(browser, "_stealth_cm", None)
        monkeypatch.setattr(browser, "_browser_lock", asyncio.Lock())
        monkeypatch.setattr(browser, "_PROFILE_DIR", tmp_path / "profile")
        monkeypatch.setattr(profile_backup, "backup_camoufox_profile", lambda *a: None)
        monkeypatch.setattr(cf, "card_fill_enabled", lambda on=on: on)
        page = MagicMock()
        page.is_closed.return_value = False
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=MagicMock(pages=[page]))
        ctor = MagicMock(return_value=cm)
        api = MagicMock(AsyncCamoufox=ctor)
        ready = engine.EngineStatus(engine.READY, "Camoufox", tmp_path / "bin")
        with (
            patch.object(engine, "resolve_camoufox_engine", return_value=ready),
            patch.dict(
                sys.modules, {"camoufox": MagicMock(async_api=api), "camoufox.async_api": api}
            ),
        ):
            await browser._ensure_browser()
        prefs = ctor.call_args.kwargs["firefox_user_prefs"]
        assert ("places.history.enabled" in prefs) is on
        assert prefs["browser.sessionhistory.max_entries"] == 10
        assert pc.prefs_pinned() is on


# ---------------------------------------------------------------------------
# The isolation contract
# ---------------------------------------------------------------------------

_SRC = Path(__file__).resolve().parents[2] / "src" / "genesis"
_OWNER = _SRC / "mcp" / "health" / "payment_context.py"
# CDP layers whose own browsers are walked (remote Chrome, TinyFish), by receiver name.
_CDP_RECEIVERS = {"remote_browser", "_remote_browser", "_tinyfish_browser"}
_CAMOUFOX_ROOTS = {"_stealth_browser", "_stealth_page", "_active_page", "_stealth_cm"}


def _root(node):
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.value if not isinstance(node, ast.Call) else node.func
    return node.id if isinstance(node, ast.Name) else None


def test_only_payment_context_reaches_a_second_camoufox_context():
    """An accident guard, not a proof: no module but payment_context reads
    ``.browser`` off a Camoufox object, or touches ``.contexts`` / ``new_context``
    outside the CDP layers; and nothing reads payment_context's private state."""
    bad = []
    for path in _SRC.rglob("*.py"):
        if path == _OWNER:
            continue
        text = path.read_text()
        if not any(w in text for w in (".browser", "contexts", "new_context", "payment_context")):
            continue  # nothing the rules below look at (keeps the walk fast)
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            root = _root(node.value)
            where = f"{path.relative_to(_SRC)}:{node.lineno}"
            if node.attr == "browser" and root in _CAMOUFOX_ROOTS:
                bad.append(where)
            if node.attr in ("contexts", "new_context") and root not in _CDP_RECEIVERS:
                bad.append(where)
            if root in ("payment_context", "pc") and node.attr.startswith("_"):
                bad.append(where)
    assert bad == []


def test_the_contract_test_catches_a_reach():
    tree = ast.parse("x = _stealth_browser.browser.contexts[1]")
    attrs = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute)]
    assert {(n.attr, _root(n.value)) for n in attrs} == {
        ("contexts", "_stealth_browser"),
        ("browser", "_stealth_browser"),
    }
