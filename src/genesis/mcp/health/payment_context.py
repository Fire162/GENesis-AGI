"""browser_fill_card's flow, in a browser context no other tool can reach.

The checkout the agent opened in the main Camoufox context is reopened in a
second, short-lived ``BrowserContext`` of the same browser: same device
fingerprint, the merchant's cookies and storage handed over, nothing else.
This module is the only one that creates, holds or closes that context, its
pages and its element handles (a contract test pins it). Every other browser
tool acts on ``browser._active_page``, which never points into it.

The flow, in order: check the checkout's card fields, Pay control and amount
in the payment context, fill the named non-card fields, ask the vault for a
release (the owner's Telegram tap shows the site, amount, card, Pay button
label, field origins and the extra field names), wait for the tap, re-check
the page, claim the card once, fill the card fields through element handles,
click Pay, classify what followed, and close the context. Every document of
the context is masked (fully transparent) from creation, so the display shows
nothing of the checkout.

The agent gets back only a fixed code and text, the merchant host, the kinds
of fields typed, and a vault reason from its fixed set: never page text, a
URL path, an exception's text or a value. Card values live in locals of one
coroutine and are dropped after the fill.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from genesis.browser.profile import domain_matches, registrable_domain
from genesis.payments import card_fill as cf
from genesis.payments import release_client as rc

logger = logging.getLogger(__name__)

STEP_S = 15.0  # one Playwright step (a count, a fill, a read-back)
EXPORT_S = 120.0  # the storage export: one temporary-page visit per origin seen since launch
GOTO_MS = 30000
POLL_S = 2.0
STATUS_FAILS = 5  # consecutive unanswered status polls before giving up
# The vault refuses a pending release 720 s after its request and a granted
# one 120 s after the tap; past both (plus slack) nothing can be granted.
CEILING_S = 900.0
CANCEL_S = 25.0
CLICK_MS = 10000
SETTLE_S = 60.0  # after the Pay click, for a navigation or a challenge
# After the URL changes, how long a challenge may still appear: a new document's
# load (up to 10 s) plus 3 s, and an in-page URL change gets the same wait.
NAV_GRACE_S = 13.0
TICK_S = 0.25
CLOSE_S = 10.0
TEARDOWN_S = 60.0
ABORT_S = 70.0
_TITLE_CHARS = 120  # the vault cuts to 80; this keeps the request line small
_NONCE = re.compile(r"[A-Za-z0-9_-]{1,64}")
# 3DS challenge pages, by host or path. Inferred, not measured: an unknown
# challenge still ends as "challenge" when the page does not move on in time.
_CHALLENGE_HOSTS = ("cardinalcommerce.com", "3dsecure.io", "hooks.stripe.com")
_CHALLENGE_PATH = re.compile(r"3d[-_]?secure|three[-_]?ds|/3ds|/acs\b", re.IGNORECASE)

# The mask (from the payment-tab guard design): the root element of every
# document is made fully transparent, so its whole subtree (shadow trees,
# iframes, canvas) draws nothing; controls get transparent text as a second
# layer. Opacity changes no layout or hit testing, so clicks still land.
MASK_CSS = (
    ":root { opacity: 0 !important }"
    " input, textarea, select, [contenteditable] {"
    " color: transparent !important; -webkit-text-fill-color: transparent !important;"
    " caret-color: transparent !important; text-shadow: none !important }"
    " ::selection { color: transparent !important; background: transparent !important }"
)
_MASK_JS = (
    "() => { const id = '__genesis_payment_mask__';"
    " const root = document.documentElement;"
    " if (!root) return false;"
    " root.style.setProperty('opacity', '0', 'important');"
    " let s = document.getElementById(id);"
    " if (!s) { s = document.createElement('style'); s.id = id;"
    " (document.head || root).appendChild(s); }"
    " s.textContent = '" + MASK_CSS + "'; return true; }"
)
# A context-level init script: runs before the page's own scripts in every
# document and frame of the payment context, popups included.
MASK_INIT_JS = (
    "(() => { const m = " + _MASK_JS + ";"
    " if (!m()) { const o = new MutationObserver(() => { if (m()) o.disconnect(); });"
    " o.observe(document, {childList: true, subtree: true}); }"
    " document.addEventListener('DOMContentLoaded', m); })();"
)
_MASK_CHECK_JS = """() => {
  const clear = (v) => v === 'transparent' || /^rgba\\([^)]*,\\s*0\\)$/.test(v);
  const root = document.documentElement;
  if (!root || getComputedStyle(root).opacity !== '0') return false;
  for (const e of document.querySelectorAll('input, textarea, select')) {
    const s = getComputedStyle(e);
    if (!clear(s.color) || !clear(s.webkitTextFillColor) || !clear(s.caretColor)) return false;
    if (s.textShadow && s.textShadow !== 'none') return false;
  }
  return true;
}"""
# Whether ``e`` is a field of ``kind``: an enabled input or select whose
# autocomplete names that kind, or a type=tel input (a select for an expiry
# month or year) whose label, aria-label, placeholder, name or id says so.
# Returns the tag, or '' when it is not.
CARD_FIELD_JS = r"""(e, kind) => {
  const tag = e.tagName.toLowerCase();
  if (!['input', 'select'].includes(tag) || e.disabled || e.readOnly) return '';
  const type = (e.getAttribute('type') || 'text').toLowerCase();
  const bad = ['hidden', 'file', 'checkbox', 'radio', 'submit', 'button', 'image',
               'reset', 'range', 'color', 'password'];
  if (tag === 'input' && bad.includes(type)) return '';
  const tokens = {number: ['cc-number'], expiry: ['cc-exp'], exp_month: ['cc-exp-month'],
                  exp_year: ['cc-exp-year'], cvc: ['cc-csc'], name: ['cc-name'],
                  postal_code: ['postal-code']}[kind];
  if (!tokens) return '';
  const ac = (e.getAttribute('autocomplete') || '').toLowerCase().split(/\s+/);
  if (tokens.some((t) => ac.includes(t))) return tag;
  const label = {number: /card\s*(number|no\b|#)|card\s*num/i,
                 expiry: /expir|exp\.?\s*date|mm\s*\/\s*yy/i,
                 exp_month: /month|\bmm\b/i,
                 exp_year: /year|\byy(yy)?\b/i,
                 cvc: /\b(cvc|cvv|csc|cid)\b|security\s*code|card\s*code/i}[kind];
  if (!label) return '';
  const shaped = tag === 'select' ? ['exp_month', 'exp_year'].includes(kind) : type === 'tel';
  if (!shaped) return '';
  const text = [e.getAttribute('aria-label'), e.getAttribute('placeholder'),
                e.getAttribute('name'), e.id,
                ...Array.from(e.labels || []).map((l) => l.textContent)]
    .filter(Boolean).join(' ');
  return label.test(text) ? tag : '';
}"""
# The Pay control: a button (not type=reset) or an input type=submit; whether
# its effective form method is GET; whether it can be clicked; its label.
PAY_JS = r"""(e) => {
  const tag = e.tagName.toLowerCase();
  const type = (e.getAttribute('type') || '').toLowerCase();
  const button = tag === 'button' ? type !== 'reset' : tag === 'input' && type === 'submit';
  const f = e.form;
  const m = !f ? '' : (e.hasAttribute('formmethod') ? e.formMethod : f.method);
  const label = tag === 'input' ? (e.value || '') : (e.innerText || e.textContent || '');
  return {button, get: String(m).toLowerCase() === 'get',
          live: e.isConnected && !e.disabled, label: label.slice(0, 200)};
}"""
# An extra (non-card) field: an input, textarea or select whose autocomplete
# names no card token. Returns the tag, or ''.
EXTRA_FIELD_JS = r"""(e) => {
  const tag = e.tagName.toLowerCase();
  if (!['input', 'textarea', 'select'].includes(tag) || e.disabled || e.readOnly) return '';
  const ac = (e.getAttribute('autocomplete') || '').toLowerCase();
  return /(^|\s)cc-/.test(ac) ? 'card' : tag;
}"""
_SAME_JS = "(a, b) => a === b"
_TEXT_JS = "() => (document.body ? document.body.innerText : '')"


class PaymentInFlight(Exception):
    """A browser restart refused because a claimed card is in the payment context."""


@dataclass(frozen=True)
class Hooks:
    """What the flow needs from browser.py, which owns the main browser."""

    alive: Callable[[], bool]  # the main Camoufox context is still the one the flow began on
    touch: Callable[[], None]  # keep Camoufox's idle clock from reclaiming it
    interstitial: Callable[[object], Awaitable[bool]]  # a bot-check page?
    close_browser: Callable[[], Awaitable[None]]  # close all of Camoufox (takes its lock)


@dataclass(eq=False)
class _Field:
    frame: object
    handle: object
    tag: str
    origin: str


@dataclass(eq=False)
class _Flow:
    task: asyncio.Task | None
    host: str = ""
    pctx: object = None
    nonce: str | None = None
    claimed: bool = False
    typing: bool = False
    typed: list[str] = field(default_factory=list)
    paid: bool = False
    popup: bool = False
    navs: int = 0
    abort_reason: str | None = None
    tearing_down: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)


class _Stop(Exception):
    def __init__(self, code: str, **extra) -> None:
        super().__init__(code)
        self.code, self.extra = code, extra


_flow: _Flow | None = None
_x_input = 0  # Turnstile X-input phases running in this process
_prefs_pinned = False
_tasks: set[asyncio.Task] = set()


def set_prefs_pinned(pinned: bool) -> None:
    """Called by each Camoufox launch: whether it pinned ``CARD_FILL_PREFS``."""
    global _prefs_pinned
    _prefs_pinned = pinned


def prefs_pinned() -> bool:
    return _prefs_pinned


def in_flight() -> bool:
    """A fill holds the payment context or is still tearing it down."""
    return _flow is not None


def past_claim() -> bool:
    """A card was claimed and the flow has not finished tearing down."""
    return _flow is not None and _flow.claimed


def is_payment_page(page) -> bool:
    """Whether ``page`` belongs to the payment context (no tool may act on it)."""
    flow = _flow
    return (
        flow is not None and flow.pctx is not None and getattr(page, "context", None) is flow.pctx
    )


@contextlib.contextmanager
def try_x_input():
    """Turnstile's side of the one lock: yields False while a payment runs,
    else True, and a fill started meanwhile refuses ``turnstile_in_progress``."""
    global _x_input
    if _flow is not None:
        yield False
        return
    _x_input += 1
    try:
        yield True
    finally:
        _x_input -= 1


def _spawn(coro) -> asyncio.Task:
    """A task held until it ends, whose exception is retrieved here (asyncio's
    own logger has no card filter)."""
    task = asyncio.ensure_future(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    return task


async def abort(reason: str) -> None:
    """End a running fill now (MCP exit, the main browser closing) and wait,
    bounded, for its teardown: a release not yet claimed is cancelled."""
    flow = _flow
    if flow is None:
        return
    flow.abort_reason = reason
    task = flow.task
    if task and not task.done() and not flow.tearing_down and task is not asyncio.current_task():
        task.cancel()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(flow.done.wait(), timeout=ABORT_S)


async def fill_card(args: cf.CardArgs, main_ctx, main_page, hooks: Hooks, report) -> dict:
    """The whole flow; returns a fixed result. ``main_ctx`` and ``main_page``
    are the Camoufox context and active page, read once by the caller."""
    global _flow
    if _flow is not None:
        return cf.result("payment_in_flight")
    if _x_input:
        return cf.result("turnstile_in_progress")
    flow = _flow = _Flow(task=asyncio.current_task())
    try:
        try:
            return await _run(flow, args, main_ctx, main_page, hooks, report)
        except _Stop as s:
            return cf.result(s.code, flow.host, **s.extra)
        except asyncio.CancelledError:
            if flow.abort_reason is None:
                raise
            asyncio.current_task().uncancel()
            return _ended(flow, "browser_closed")
        except Exception as e:  # noqa: BLE001 - never an exception's text: it may quote a value
            logger.error("payment fill step failed (%s)", type(e).__name__)
            return _ended(flow, "internal_error")
    finally:
        # Shielded and held: a cancelled caller cannot skip the cancel or the close.
        flow.tearing_down = True  # an abort from here waits for it, keeping this result
        await asyncio.shield(_spawn(asyncio.wait_for(_teardown(flow, hooks), TEARDOWN_S)))


def _ended(flow: _Flow, before_claim: str) -> dict:
    """The result for a flow cut short, by how far it got."""
    if flow.paid:
        return cf.result("outcome_unknown", flow.host)
    if flow.typing:
        return cf.result("failed_before_pay", flow.host, filled=flow.typed)
    return cf.result("claimed_not_filled" if flow.claimed else before_claim, flow.host)


async def _step(aw):
    return await asyncio.wait_for(aw, timeout=STEP_S)


async def _teardown(flow: _Flow, hooks: Hooks) -> None:
    global _flow
    try:
        if flow.nonce is not None and not flow.claimed:
            try:
                await asyncio.wait_for(rc.cancel(flow.nonce), timeout=CANCEL_S)
            except Exception as e:  # noqa: BLE001 - the vault expires it on its own
                logger.warning("payment release cancel failed (%s)", type(e).__name__)
        if flow.pctx is not None:
            try:
                await asyncio.wait_for(flow.pctx.close(), timeout=CLOSE_S)
            except Exception as e:  # noqa: BLE001
                logger.error("payment context did not close (%s)", type(e).__name__)
                if hooks.alive():  # it may hold typed values: close the whole browser
                    _spawn(hooks.close_browser())
    finally:
        if _flow is flow:
            _flow = None
        flow.done.set()


async def _walk(frame, frames: tuple[str, ...]):
    """The frame a chain of iframe selectors leads to, or None."""
    for fsel in frames:
        loc = frame.locator(fsel)
        if await _step(loc.count()) != 1:
            return None
        handle = await _step(loc.element_handle(timeout=5000))
        try:
            frame = await _step(handle.content_frame())
        finally:
            with contextlib.suppress(Exception):
                await _step(handle.dispose())
        if frame is None:
            return None
    return frame


async def _resolve_one(page, spec: cf.Spec, kind: str) -> tuple[object, object, str]:
    frames, sel = spec
    frame = await _walk(page.main_frame, frames)
    if frame is None:
        raise _Stop(f"frame_not_found:{kind}")
    loc = frame.locator(sel)
    count = await _step(loc.count())
    if count != 1:
        raise _Stop(
            "pay_not_found"
            if kind == "pay"
            else f"field_{'not_found' if count == 0 else 'ambiguous'}:{kind}"
        )
    origin = cf.origin_of(frame.url)
    if origin is None:  # about:blank, srcdoc and data: frames are never filled
        raise _Stop("pay_not_found" if kind == "pay" else f"field_origin:{kind}")
    return frame, await _step(loc.element_handle(timeout=5000)), origin


async def _resolve(page, args: cf.CardArgs) -> tuple[dict[str, _Field], _Field, str]:
    """Each card field and the Pay control, resolved to exactly one element and
    checked for meaning on it; plus Pay's label. The fill and the click go
    through these handles, so a swapped element or a navigated frame makes them
    fail instead of acting on a document the tap did not show."""
    fields = {}
    for kind, spec in args.fields.items():
        frame, handle, origin = await _resolve_one(page, spec, kind)
        tag = await _step(handle.evaluate(CARD_FIELD_JS, kind))
        if tag not in ("input", "select"):
            raise _Stop(f"field_mismatch:{kind}")
        fields[kind] = _Field(frame, handle, tag, origin)
    frame, handle, origin = await _resolve_one(page, args.pay, "pay")
    info = await _step(handle.evaluate(PAY_JS))
    if not isinstance(info, dict) or info.get("button") is not True:
        raise _Stop("pay_not_button")
    if info.get("get") is not False:
        raise _Stop("pay_get_form")
    label = info.get("label")
    return fields, _Field(frame, handle, "pay", origin), label if isinstance(label, str) else ""


async def _mask_ok(page) -> bool:
    try:
        frames = list(page.frames)
        for frame in frames:
            if await _step(frame.evaluate(_MASK_CHECK_JS)) is not True:
                return False
        return bool(frames)
    except Exception as e:  # noqa: BLE001
        logger.warning("payment mask check failed (%s)", type(e).__name__)
        return False


async def _amount_ok(page, amount: str) -> bool:
    """The stated amount is in the page text. The text is read here and dropped:
    it is never returned or logged."""
    parts = []
    for frame in list(page.frames):
        with contextlib.suppress(Exception):
            parts.append(await _step(frame.evaluate(_TEXT_JS)))
    ok = cf.amount_on_page(amount, "\n".join(p for p in parts if isinstance(p, str)))
    parts.clear()
    return ok


def _gone(flow: _Flow, ppage, hooks: Hooks) -> str | None:
    if not hooks.alive():
        return "browser_closed"
    if ppage.is_closed():
        return "checkout_closed"
    return None


def _moved(ppage, top: str, fields: dict[str, _Field], pay: _Field, seen: dict) -> bool:
    """The page no longer shows what the tap was for: another top origin, or a
    field's or Pay's frame not on the origin seen before the tap."""
    try:
        if cf.origin_of(ppage.url) != top:
            return True
        return any(
            cf.origin_of(f.frame.url) != seen.get(k) for k, f in [*fields.items(), ("pay", pay)]
        )
    except Exception:  # noqa: BLE001
        return True


async def _fill_extras(ppage, args: cf.CardArgs, fields: dict[str, _Field]) -> None:
    for name, (sel, value) in args.extra.items():
        loc = ppage.main_frame.locator(sel)
        if await _step(loc.count()) != 1:
            raise _Stop("extra_field_not_found")
        handle = await _step(loc.element_handle(timeout=5000))
        tag = await _step(handle.evaluate(EXTRA_FIELD_JS))
        if tag == "card":
            raise _Stop("extra_field_is_card_field")
        if tag not in ("input", "textarea", "select"):
            raise _Stop("extra_field_not_found")
        for f in fields.values():
            if f.frame is ppage.main_frame and await _step(handle.evaluate(_SAME_JS, f.handle)):
                raise _Stop("extra_field_is_card_field")
        try:
            if tag == "select":
                await _select(handle, value)
            else:
                await _step(handle.fill(value, timeout=10000))
        except Exception:  # noqa: BLE001
            raise _Stop("extra_field_failed") from None
        logger.debug("payment extra field filled (%s)", name)


async def _select(handle, value: str, kind: str = "") -> None:
    """A select: by option value, then label; an expiry month or year also in
    the forms a site uses (01 or 1; 2034 or 34)."""
    cands = [value]
    if kind == "exp_month":
        cands.append(str(int(value)))
    elif kind == "exp_year":
        cands += [value[-2:], str(2000 + int(value) % 100)]
    for how in ("value", "label"):
        for cand in dict.fromkeys(cands):
            with contextlib.suppress(Exception):
                await _step(handle.select_option(**{how: cand}, timeout=2000))
                return
    raise RuntimeError("no matching option")


async def _await_grant(flow: _Flow, ppage, hooks: Hooks, report) -> None:
    """Poll release_status until granted; raises _Stop otherwise."""
    loop = asyncio.get_running_loop()
    ceiling = loop.time() + CEILING_S
    backstop, fails = ceiling, 0
    while True:
        if why := _gone(flow, ppage, hooks):
            raise _Stop(why)
        hooks.touch()
        final = loop.time() >= backstop
        try:
            status = await rc.release_status(flow.nonce)
            fails = 0
        except rc.ReleaseClientError as e:
            fails += 1
            if final or fails >= STATUS_FAILS:
                raise _Stop(e.code) from None
            await asyncio.sleep(POLL_S)
            continue
        state = status.get("state")
        if state == "granted":
            return
        if state in ("refused", "expired"):
            raise _Stop("not_approved" if state == "refused" else "release_expired")
        if state != "pending":
            raise _Stop("bad_reply")
        if final:
            raise _Stop("release_timed_out")
        left = status.get("expires_in_s")
        if isinstance(left, int) and not isinstance(left, bool) and left >= 0:
            backstop = min(ceiling, loop.time() + left + 30)
        if report is not None:
            await report(
                "waiting for the owner's approval in Telegram "
                f"({max(0, round(backstop - loop.time()))} s left)"
            )
        await asyncio.sleep(POLL_S)


def _is_challenge(url) -> bool:
    try:
        parts = urlsplit(url)
    except (ValueError, TypeError):
        return False
    host = (parts.hostname or "").lower()
    return bool(
        any(domain_matches(host, h) for h in _CHALLENGE_HOSTS)
        or re.match(r"acs[0-9]*[.-]", host) is not None
        or _CHALLENGE_PATH.search(parts.path or "")
    )


async def _outcome(flow: _Flow, ppage, start: str, before: set, site: str, hooks: Hooks) -> str:
    """What followed the Pay click (``start``: the URL before it; ``before``: the
    frame URLs then), from navigation and new frame URLs only."""
    loop = asyncio.get_running_loop()
    deadline, moved = loop.time() + SETTLE_S, False
    while loop.time() < deadline:
        if not hooks.alive() or ppage.is_closed():
            return "outcome_unknown"
        if flow.popup or any(
            _is_challenge(f.url) for f in list(ppage.frames) if f.url not in before
        ):
            return "challenge"
        if not moved and (ppage.url != start or flow.navs):
            moved, deadline = True, loop.time() + NAV_GRACE_S
        hooks.touch()
        await asyncio.sleep(TICK_S)
    if not moved:
        return "challenge"  # no settle within the bound: a bank check, a decline, or stuck
    host = urlsplit(ppage.url).hostname
    return "submitted_unverified" if host and domain_matches(host.lower(), site) else "challenge"


async def _run(flow: _Flow, args: cf.CardArgs, main_ctx, main_page, hooks: Hooks, report) -> dict:
    url = main_page.url
    main_host = urlsplit(url).hostname
    if cf.origin_of(url) is None or not main_host:
        raise _Stop("not_http")
    site = registrable_domain(main_host)
    flow.host = main_host

    # Handover. Frame chains resolve read-only in the main page first (their
    # origins choose the storage handed over); nothing there is filled or clicked.
    frame_origins = set()
    for kind, (frames, _sel) in [*args.fields.items(), ("pay", args.pay)]:
        if frames:
            frame = await _walk(main_page.main_frame, frames)
            if frame is None or (origin := cf.origin_of(frame.url)) is None:
                raise _Stop(f"frame_not_found:{kind}")
            frame_origins.add(origin)
    # storage_state reads origins without a live page through a temporary main-context
    # page that visits each one (answered locally, no network): it grows with history.
    state = await asyncio.wait_for(main_ctx.storage_state(indexed_db=True), EXPORT_S)
    handed = cf.filter_state(state, site, frame_origins)
    del state
    try:
        from camoufox.utils import STOCK_MEDIA_DEFAULTS

        pctx = flow.pctx = await _step(
            main_ctx.browser.new_context(
                storage_state=handed,
                no_viewport=True,  # as the persistent launch; a viewport can hang new_page()
                accept_downloads=False,
                **STOCK_MEDIA_DEFAULTS,
            )
        )
        del handed
        pctx.on("dialog", lambda d: _spawn(d.dismiss()))
        await _step(pctx.add_init_script(MASK_INIT_JS))
        ppage = await _step(pctx.new_page())
        # The context's "page" event fires for new_page() too: only another page is a popup.
        pctx.on("page", lambda p: p is not ppage and setattr(flow, "popup", True))
        await asyncio.wait_for(
            ppage.goto(url, wait_until="domcontentloaded", timeout=GOTO_MS), GOTO_MS / 1000 + 5
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("payment context load failed (%s)", type(e).__name__)
        raise _Stop("checkout_load_failed") from None
    if not await _mask_ok(ppage):
        raise _Stop("checkout_load_failed")
    host, top = urlsplit(ppage.url).hostname, cf.origin_of(ppage.url)
    if not host or top is None or registrable_domain(host) != site:
        raise _Stop("checkout_redirected")
    flow.host = host
    if await hooks.interstitial(ppage):
        raise _Stop("bot_check")

    fields, pay, label = await _resolve(ppage, args)
    origins = {k: f.origin for k, f in fields.items()}
    seen = {**origins, "pay": pay.origin}
    await _fill_extras(ppage, args, fields)  # before the tap: no card value exists yet
    if not await _mask_ok(ppage):
        raise _Stop("mask_unverified")
    if not await _amount_ok(ppage, args.amount):  # after the extras: an address can re-price
        raise _Stop("amount_not_on_page")

    # The tap.
    title = ""
    with contextlib.suppress(Exception):
        title = await _step(ppage.title())
    title = (title if isinstance(title, str) else "")[:_TITLE_CHARS]
    secret = secrets.token_hex(32)
    try:
        reply = await rc.request_release(
            args.card_ref,
            host,
            title,
            args.amount,
            hashlib.sha256(secret.encode()).hexdigest(),
            origins,
            label,
            list(args.extra),
        )
    except rc.ReleaseClientError as e:
        raise _Stop(e.code) from None
    if reply.get("ok") is not True:
        if reply.get("error") == "the approval text was refused":
            raise _Stop("approval_text_refused")
        raise _Stop("release_refused", reason=reply.get("error"))
    if not isinstance(reply.get("nonce"), str) or not _NONCE.fullmatch(reply["nonce"]):
        raise _Stop("bad_reply")
    flow.nonce = reply["nonce"]
    await _await_grant(flow, ppage, hooks, report)

    # Granted: the page must still show what the tap was for, fields re-resolved.
    fields, pay, label2 = await _resolve(ppage, args)
    if _moved(ppage, top, fields, pay, seen) or label2 != label:
        raise _Stop("page_changed")
    if not await _amount_ok(ppage, args.amount):
        raise _Stop("page_changed")
    if not await _mask_ok(ppage):
        raise _Stop("mask_unverified")
    if why := _gone(flow, ppage, hooks):
        raise _Stop(why)
    flow.claimed = True  # from here the vault may hand the card over: no cancel
    try:
        reply = await rc.claim(flow.nonce, secret)
    except rc.ReleaseClientError:
        raise _Stop("claim_unanswered") from None
    finally:
        del secret
    card = reply.pop("card", None)
    if reply.get("ok") is not True or not isinstance(card, dict):
        del card
        raise _Stop("claim_refused", reason=reply.get("error"))
    try:
        if set(card) != cf.reply_keys(fields):
            raise _Stop("claimed_not_filled")  # the vault and the tool disagree about the tap
        values = cf.values_for(card, list(fields), args.expiry_format)
    finally:
        del card

    # Typing. Nothing is typed into a page that changed after the tap.
    if (
        _gone(flow, ppage, hooks)
        or _moved(ppage, top, fields, pay, seen)
        or not await _mask_ok(ppage)
    ):
        values.clear()
        raise _Stop("claimed_not_filled")
    try:
        flow.typing = True
        for kind, f in fields.items():
            if cf.origin_of(f.frame.url) != origins[kind] or not await _mask_ok(ppage):
                raise _Stop("failed_before_pay", field=kind, filled=flow.typed)
            if f.tag == "select":
                await _select(f.handle, values[kind], kind)
            else:
                await _step(f.handle.fill(values[kind], timeout=10000))
            if not cf.value_landed(
                kind, values[kind], await _step(f.handle.input_value(timeout=5000))
            ):
                raise _Stop("failed_before_pay", field=kind, filled=[*flow.typed, kind])
            flow.typed.append(kind)
    finally:
        values.clear()

    # Pay.
    info = await _step(pay.handle.evaluate(PAY_JS))
    if (
        not isinstance(info, dict)
        or info.get("live") is not True
        or info.get("get") is not False
        or _moved(ppage, top, fields, pay, seen)
        or not await _mask_ok(ppage)
        or not await _amount_ok(ppage, args.amount)
    ):
        raise _Stop("failed_before_pay", filled=flow.typed)
    ppage.on(
        "framenavigated", lambda fr: fr is ppage.main_frame and setattr(flow, "navs", flow.navs + 1)
    )
    start, before = ppage.url, {f.url for f in list(ppage.frames)}
    flow.paid = True  # set before the click: a click that raised may still have landed
    with contextlib.suppress(Exception):
        await _step(pay.handle.click(timeout=CLICK_MS))
    code = await _outcome(flow, ppage, start, before, site, hooks)
    return cf.result(code, flow.host, filled=flow.typed)
