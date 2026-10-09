"""Shared helpers: human-like delays, honeypot-safe element lookup, screenshots.

The BLS portal randomises field ids/names on every load and plants ~10 hidden
"Email" honeypots, so everything here works off *computed visibility* rather
than selectors that depend on names or ids.
"""

from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Sequence

from loguru import logger

SCREENSHOT_DIR = Path("logs/screenshots")
DOM_DIR = Path("logs/dom")

# CONFIRMED: on the BLS login form every input sits inside a `div.mb-3`; only the
# real field's wrapper is display:block, the honeypots' wrappers are display:none.
REAL_FIELD_WRAPPER = "div.mb-3"

# JS predicate: is this element genuinely visible to a human?
# Catches display/visibility/opacity, zero-size, offscreen and clipped honeypots.
_VISIBILITY_JS = """
(el) => {
  const rect = el.getBoundingClientRect();
  if (rect.width < 2 || rect.height < 2) return false;
  if (rect.right < -50 || rect.bottom < -50) return false;
  if (rect.left > window.innerWidth + 300) return false;
  if (el.disabled === true) return false;
  if (el.readOnly === true) return false;
  if (el.getAttribute('aria-hidden') === 'true') return false;
  if (el.type === 'hidden') return false;
  if (el.tabIndex === -1 && el.tagName === 'INPUT') return false;
  let node = el;
  while (node && node.nodeType === 1) {
    const s = getComputedStyle(node);
    if (s.display === 'none') return false;
    if (s.visibility === 'hidden' || s.visibility === 'collapse') return false;
    if (parseFloat(s.opacity || '1') < 0.1) return false;
    if (s.clipPath && s.clipPath.replace(/\\s/g, '').includes('inset(100%')) return false;
    if (s.clip && s.clip.replace(/\\s/g, '') === 'rect(0px,0px,0px,0px)') return false;
    node = node.parentElement;
  }
  return true;
}
"""


# --------------------------------------------------------------------------- #
# Timing
# --------------------------------------------------------------------------- #
async def human_delay(config: dict[str, Any] | None = None, *, scale: float = 1.0) -> float:
    """Sleep a random human-ish interval (config ``browser.min_delay``..``max_delay``)."""
    browser = (config or {}).get("browser", {}) or {}
    low = float(browser.get("min_delay", 0.5)) * scale
    high = float(browser.get("max_delay", 2.0)) * scale
    if high < low:
        low, high = high, low
    delay = random.uniform(low, high)
    await asyncio.sleep(delay)
    return delay


async def type_like_human(element: Any, text: str, *, per_char: float = 0.08) -> None:
    """Type into an element with per-keystroke jitter instead of a bulk fill."""
    await element.click()
    await asyncio.sleep(random.uniform(0.1, 0.3))
    await element.fill("")
    for char in text:
        await element.type(char, delay=random.uniform(per_char * 400, per_char * 1400))
    await asyncio.sleep(random.uniform(0.2, 0.6))


async def interruptible_sleep(seconds: float, state: Any = None, *, step: float = 1.0) -> None:
    """Sleep in small steps so a /stop request is honoured promptly."""
    waited = 0.0
    while waited < seconds:
        if state is not None and state.stop_requested:
            return
        chunk = min(step, seconds - waited)
        await asyncio.sleep(chunk)
        waited += chunk


# --------------------------------------------------------------------------- #
# Stealth
# --------------------------------------------------------------------------- #
async def apply_stealth(target: Any) -> bool:
    """Apply playwright-stealth to a page/context, tolerating 1.x and 2.x APIs."""
    try:  # playwright-stealth 1.x
        from playwright_stealth import stealth_async  # type: ignore

        await stealth_async(target)
        return True
    except ImportError:
        pass
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"stealth_async failed: {exc}")

    try:  # playwright-stealth 2.x
        from playwright_stealth import Stealth  # type: ignore
    except ImportError:
        logger.warning("playwright-stealth is not installed — fingerprinting risk")
        return False

    try:
        stealth = Stealth()
        for attr in ("apply_stealth_async", "apply_async"):
            fn = getattr(stealth, attr, None)
            if fn is not None:
                await fn(target)
                return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Stealth() failed: {exc}")
    return False


# --------------------------------------------------------------------------- #
# Honeypot-safe element lookup
# --------------------------------------------------------------------------- #
async def is_really_visible(element: Any) -> bool:
    """True only if the element is visible to a human (honeypot-proof)."""
    try:
        if not await element.is_visible():
            return False
        return bool(await element.evaluate(_VISIBILITY_JS))
    except Exception:
        return False


async def find_real_input(
    page: Any,
    input_type: str = "text",
    *,
    wrapper: str = REAL_FIELD_WRAPPER,
) -> Any | None:
    """Find the one real input among the BLS honeypots.

    BLS login: 10 fields, random ids, **random position** (confirmed: Field 0 on
    one load, Field 3 on the next). Only the real field's parent ``div.mb-3`` has
    ``display: block`` — every honeypot's parent is ``display: none``.

    So the wrapper's computed display is the discriminator. Never use ``.first``,
    a fixed index, an id, or a name — all of those change between page loads.
    """
    try:
        inputs = await page.query_selector_all(f'input[type="{input_type}"]')
    except Exception as exc:
        logger.debug(f"find_real_input: query failed for {input_type!r}: {exc}")
        return None

    if not inputs:
        logger.debug(f"find_real_input: no input[type={input_type!r}] on the page")
        return None

    candidates: list[tuple[int, Any]] = []
    for index, element in enumerate(inputs):
        display = await _wrapper_display(element, wrapper)
        if display == "block":
            candidates.append((index, element))

    total = len(inputs)
    if not candidates:
        logger.warning(
            f"find_real_input: none of {total} {input_type!r} input(s) had a "
            f"{wrapper!r} parent with display:block"
        )
        return None

    if len(candidates) == 1:
        index, element = candidates[0]
        logger.info(
            f"find_real_input: real {input_type!r} field is index {index} of {total} "
            f"(parent {wrapper} display:block)"
        )
        return element

    # More than one wrapper is visible — disambiguate on actual visibility.
    logger.warning(
        f"find_real_input: {len(candidates)} of {total} {input_type!r} inputs look "
        "real; falling back to a visibility check to pick one"
    )
    for index, element in candidates:
        if await is_really_visible(element):
            logger.info(f"find_real_input: chose index {index} (genuinely visible)")
            return element
    return candidates[0][1]


async def _wrapper_display(element: Any, wrapper: str) -> str | None:
    """Computed ``display`` of the element's nearest ``wrapper`` ancestor.

    Returns None when there is no such ancestor — ``closest()`` yields null and
    ``getComputedStyle(null)`` would throw, so the guard lives inside the JS.
    """
    try:
        return await element.evaluate(
            """
            (el, selector) => {
              const parent = el.closest(selector);
              if (!parent) return null;
              return window.getComputedStyle(parent).display;
            }
            """,
            wrapper,
        )
    except Exception as exc:
        logger.debug(f"_wrapper_display failed: {exc}")
        return None


async def visible_elements(page: Any, selector: str) -> list[Any]:
    """All elements matching ``selector`` that pass the visibility predicate."""
    found: list[Any] = []
    try:
        handles = await page.query_selector_all(selector)
    except Exception as exc:
        logger.debug(f"query_selector_all({selector!r}) failed: {exc}")
        return found
    for handle in handles:
        if await is_really_visible(handle):
            found.append(handle)
    return found


async def first_visible(page: Any, selectors: str | Sequence[str]) -> Any | None:
    """First genuinely visible element across one or more selectors."""
    if isinstance(selectors, str):
        selectors = [selectors]
    for selector in selectors:
        matches = await visible_elements(page, selector)
        if matches:
            return matches[0]
    return None


async def find_visible_input(
    page: Any,
    *,
    kinds: Sequence[str] = ("text", "email"),
    keywords: Sequence[str] = (),
    skip: Iterable[Any] = (),
) -> Any | None:
    """Find the one real input among the honeypots.

    Strategy: collect every visible input of the given types, then prefer the
    ones whose surrounding attributes hint at ``keywords``. Never matches on id
    or name alone, since BLS randomises both on every page load.
    """
    selectors = [f'input[type="{kind}"]' for kind in kinds]
    if "text" in kinds:
        selectors.append("input:not([type])")  # defaults to text

    candidates: list[Any] = []
    skip_list = list(skip)
    for selector in selectors:
        for element in await visible_elements(page, selector):
            if any(element is other for other in skip_list):
                continue
            if any(element is other for other in candidates):
                continue
            candidates.append(element)

    if not candidates:
        return None
    if not keywords or len(candidates) == 1:
        return candidates[0]

    lowered = [kw.lower() for kw in keywords]
    for element in candidates:
        haystack = " ".join(
            part
            for part in [
                await _attr(element, "placeholder"),
                await _attr(element, "aria-label"),
                await _attr(element, "autocomplete"),
                await _attr(element, "type"),
                await _label_text(element),
            ]
            if part
        ).lower()
        if any(kw in haystack for kw in lowered):
            return element
    return candidates[0]


async def _attr(element: Any, name: str) -> str:
    try:
        return (await element.get_attribute(name)) or ""
    except Exception:
        return ""


async def _label_text(element: Any) -> str:
    """Text of the nearest <label>/wrapper, used as a soft field hint."""
    try:
        return await element.evaluate(
            """
            (el) => {
              const bits = [];
              if (el.id) {
                const lab = document.querySelector(`label[for="${el.id}"]`);
                if (lab) bits.push(lab.innerText);
              }
              const wrap = el.closest('label, .form-group, .input-group, div');
              if (wrap) bits.push((wrap.innerText || '').slice(0, 120));
              return bits.join(' ');
            }
            """
        )
    except Exception:
        return ""


async def click_by_text(
    page: Any,
    texts: Sequence[str],
    *,
    roles: Sequence[str] = ("button", "a", 'input[type="submit"]', 'input[type="button"]'),
    config: dict[str, Any] | None = None,
) -> bool:
    """Click the first visible button/link whose text matches any of ``texts``."""
    for text in texts:
        for role in roles:
            for element in await visible_elements(page, role):
                label = " ".join(
                    filter(
                        None,
                        [
                            (await _safe_text(element)),
                            await _attr(element, "value"),
                            await _attr(element, "aria-label"),
                            await _attr(element, "title"),
                        ],
                    )
                ).strip().lower()
                if text.lower() in label:
                    await human_delay(config, scale=0.5)
                    try:
                        await element.click()
                        logger.debug(f"clicked element matching {text!r}")
                        return True
                    except Exception as exc:
                        logger.debug(f"click on {text!r} failed: {exc}")
    return False


async def element_text(element: Any) -> str:
    """Inner text of an element, empty string if it cannot be read."""
    try:
        return (await element.inner_text()) or ""
    except Exception:
        return ""


# Internal alias kept for readability inside this module.
_safe_text = element_text


async def select_option_like(element: Any, wanted: str) -> str | None:
    """Pick the <select> option whose label best matches ``wanted``.

    Returns the chosen label, or None when nothing matched.
    """
    try:
        options = await element.eval_on_selector_all(
            "option",
            "(nodes) => nodes.map(n => ({value: n.value, label: (n.innerText||'').trim()}))",
        )
    except Exception:
        return None

    target = _normalise(wanted)
    exact = [o for o in options if _normalise(o["label"]) == target]
    partial = [
        o
        for o in options
        if o["value"] and (target in _normalise(o["label"]) or _normalise(o["label"]) in target)
    ]
    for pool in (exact, partial):
        for option in pool:
            if not option["value"]:
                continue
            try:
                await element.select_option(value=option["value"])
                return option["label"]
            except Exception:
                continue
    return None


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


# --------------------------------------------------------------------------- #
# Page inspection
# --------------------------------------------------------------------------- #
async def page_text(page: Any) -> str:
    """Lower-cased visible text of the page, for phrase heuristics."""
    try:
        text = await page.inner_text("body")
    except Exception:
        try:
            text = await page.content()
        except Exception:
            return ""
    return (text or "").lower()


def contains_any(haystack: str, phrases: Iterable[str]) -> str | None:
    """Return the first phrase present in ``haystack``, else None."""
    low = (haystack or "").lower()
    for phrase in phrases or []:
        if phrase and phrase.lower() in low:
            return phrase
    return None


COOLDOWN_FILE = Path("logs/cooldown.json")


def record_block(reason: str) -> None:
    """Remember that the portal just blocked us, so the next run can hold off.

    Repeated automated attempts escalated an intermittent 403 into an IP-level
    block that hit on the very first request. Persisting the timestamp lets the
    bot refuse to run during the cooldown instead of digging the hole deeper.
    """
    try:
        COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
        import json

        COOLDOWN_FILE.write_text(
            json.dumps({"blocked_at": datetime.now().isoformat(), "reason": reason[:200]}),
            encoding="utf-8",
        )
        logger.warning(f"block recorded in {COOLDOWN_FILE}")
    except Exception as exc:
        logger.debug(f"could not record the block: {exc}")


def block_cooldown_remaining(minutes: float) -> float:
    """Minutes still to wait before another attempt is sensible. 0 = go ahead."""
    if minutes <= 0:
        return 0.0
    try:
        import json

        if not COOLDOWN_FILE.exists():
            return 0.0
        data = json.loads(COOLDOWN_FILE.read_text(encoding="utf-8"))
        blocked_at = datetime.fromisoformat(data["blocked_at"])
    except Exception:
        return 0.0
    elapsed = (datetime.now() - blocked_at).total_seconds() / 60.0
    return max(0.0, minutes - elapsed)


async def dump_page_html(page: Any, label: str) -> str | None:
    """Save the full rendered DOM to logs/dom/ and return the path."""
    try:
        DOM_DIR.mkdir(parents=True, exist_ok=True)
        path = DOM_DIR / f"{_stamp()}_{_safe_name(label)}.html"
        path.write_text(await page.content(), encoding="utf-8")
        logger.info(f"DOM dumped: {path}")
        return str(path)
    except Exception as exc:
        logger.warning(f"dump_page_html failed ({label}): {exc}")
        return None


async def dump_widget_dom(
    page: Any,
    selectors: Sequence[str],
    label: str,
) -> dict[str, Any]:
    """Dump the outerHTML of every element matching ``selectors``.

    Used to capture a captcha/challenge widget in full so it can be analysed
    offline without re-triggering a login. Also records every iframe on the
    page, since third-party challenges are usually iframed.
    """
    report: dict[str, Any] = {"matches": [], "iframes": [], "file": None}
    chunks: list[str] = []

    for selector in selectors:
        try:
            handles = await page.query_selector_all(selector)
        except Exception as exc:
            logger.debug(f"dump_widget_dom: bad selector {selector!r}: {exc}")
            continue
        for index, handle in enumerate(handles):
            try:
                html = await handle.evaluate("(el) => el.outerHTML")
            except Exception:
                continue
            if not html:
                continue
            visible = await is_really_visible(handle)
            report["matches"].append(
                {
                    "selector": selector,
                    "index": index,
                    "visible": visible,
                    "length": len(html),
                }
            )
            chunks.append(
                f"\n{'=' * 78}\n# selector: {selector}  [{index}]  visible={visible}\n"
                f"{'=' * 78}\n{html}\n"
            )

    try:
        report["iframes"] = await page.evaluate(
            """
            () => Array.from(document.querySelectorAll('iframe')).map(f => ({
              src: f.src || '', name: f.name || '', id: f.id || '',
              title: f.title || '', width: f.clientWidth, height: f.clientHeight
            }))
            """
        )
    except Exception as exc:
        logger.debug(f"dump_widget_dom: iframe scan failed: {exc}")

    if chunks or report["iframes"]:
        try:
            DOM_DIR.mkdir(parents=True, exist_ok=True)
            path = DOM_DIR / f"{_stamp()}_{_safe_name(label)}_widget.html"
            header = (
                f"<!-- url: {page.url}\n"
                f"     matches: {len(report['matches'])}\n"
                f"     iframes: {report['iframes']}\n-->\n"
            )
            path.write_text(header + "".join(chunks), encoding="utf-8")
            report["file"] = str(path)
            logger.info(f"widget DOM dumped: {path} ({len(report['matches'])} match(es))")
        except Exception as exc:
            logger.warning(f"dump_widget_dom: write failed: {exc}")

    return report


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _safe_name(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", label)[:60] or "page"


async def screenshot(page: Any, label: str) -> str | None:
    """Save a full-page screenshot to logs/screenshots and return its path."""
    try:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SCREENSHOT_DIR / f"{_stamp()}_{_safe_name(label)}.png"
        await page.screenshot(path=str(path), full_page=True)
        logger.info(f"screenshot saved: {path}")
        return str(path)
    except Exception as exc:
        logger.warning(f"screenshot failed ({label}): {exc}")
        return None


# --------------------------------------------------------------------------- #
# Retries
# --------------------------------------------------------------------------- #
async def with_retries(
    factory: Callable[[], Awaitable[Any]],
    *,
    attempts: int = 3,
    delay: float = 5.0,
    description: str = "operation",
    on_error: Callable[[Exception, int], Awaitable[None]] | None = None,
) -> Any:
    """Run an async factory with retries; re-raises the last error on failure."""
    last: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return await factory()
        except Exception as exc:
            last = exc
            logger.warning(f"{description} failed (attempt {attempt}/{attempts}): {exc}")
            if on_error is not None:
                try:
                    await on_error(exc, attempt)
                except Exception as hook_exc:  # pragma: no cover - defensive
                    logger.debug(f"retry hook failed: {hook_exc}")
            if attempt < attempts:
                await asyncio.sleep(delay * attempt)
    assert last is not None
    raise last
