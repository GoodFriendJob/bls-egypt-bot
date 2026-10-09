"""Login + session persistence for the BLS Spain Egypt portal.

Selector policy — read before touching anything here
----------------------------------------------------
Confirmed via DevTools on the live portal (CLAUDE.md → Portal Research Findings):

* The login form renders 10 ``input[type="text"]`` fields. Nine are honeypots.
* Every id and name is regenerated on each page load.
* **The real field's position also randomises on each load** — Field 0 on one
  load, Field 3 on the next. ``.first`` and any fixed index are therefore wrong.
* The only stable discriminator: each input sits in a ``div.mb-3`` wrapper, and
  only the real field's wrapper has computed ``display: block``. Honeypot
  wrappers are ``display: none``.

So every field in this module is located with ``utils.find_real_input()``, which
implements exactly that check. Nothing here may select an input by index, id,
name, or generated class.

Buttons are a different story — ``#btnVerify`` is a confirmed stable id and is
used directly.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from loguru import logger
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)

from modules import utils, vision


def _sample_stamp() -> str:
    """Timestamp used to name saved captcha grid samples."""
    return datetime.now().strftime("%Y%m%d-%H%M%S")


SESSION_FILE = Path("session/cookies.json")

# The real email box is an input[type="text"] whose div.mb-3 wrapper is display:block.
EMAIL_INPUT_TYPE = "text"

# CONFIRMED from live runs: the password page repeats the email page's honeypot
# pattern exactly — 10 inputs, only one real, and its index randomises per load
# (observed at index 8 on one run and index 4 on the next). The real box IS
# input[type="password"] and its parent div.mb-3 is the only one with
# display:block, so find_real_input() resolves it directly.
# The "text" entry stays as a fallback in case BLS swaps the input type.
PASSWORD_INPUT_TYPES = ("password", "text")

VERIFY_BUTTON_SELECTOR = "#btnVerify"  # CONFIRMED: stable id
VERIFY_BUTTON_TEXTS = ("verify", "continue", "next", "submit", "proceed", "تحقق", "متابعة")
LOGIN_BUTTON_TEXTS = ("login", "log in", "sign in", "submit", "دخول", "تسجيل")
COOKIE_ACCEPT_TEXTS = ("accept", "agree", "got it", "ok", "موافق")
LOGGED_IN_MARKERS = ("logout", "log out", "sign out", "my account", "تسجيل الخروج")

# CONFIRMED: a successful login lands on /Global/bls/visatypeverification.
# ("/account" does not exist on this portal at all - it 404s.)
LOGGED_IN_URL_HINTS = ("visatypeverification", "/global/bls/")

# CONFIRMED: the login form POSTs here. The hidden fields
# (__RequestVerificationToken, ResponseData, ReturnUrl, Id) ride along with the
# native form submit — the bot must never set them itself.
LOGIN_SUBMIT_PATH = "/Global/account/LoginSubmit"

BUTTON_TIMEOUT_MS = 5000

# --------------------------------------------------------------------------- #
# CAPTCHA (DOM-based — no image recognition needed)
#
# Confirmed structure from page source:
#   * Prompt text lives in  div.col-12.box-label  -> "Please select all boxes
#     with number NNN".
#   * Clickable tiles are  div.col-4  containing an <img> wired up with
#     onclick="Select('<id>', this)".
#   * A VISIBLE tile carries an inline  style="padding: 5px; display: block;"
#     A HIDDEN tile carries  style="padding:5px;"  with no display:block.
#     Several grids are stacked; only 9 tiles are visible at a time.
#   * Clicking a tile adds class "img-selected" to its <img>.
#   * The page's own Select() handler maintains the hidden input
#     "SelectedImages". The bot therefore only ever CLICKS - it must never write
#     SelectedImages itself, and must not touch the other hidden fields
#     (Id, ReturnUrl, ResponseData, Param, __RequestVerificationToken), which
#     ride along with the native form submit.
# --------------------------------------------------------------------------- #
CAPTCHA_BOX_LABEL_SELECTOR = "div.col-12.box-label"
CAPTCHA_TILE_SELECTOR = "div.col-4"
CAPTCHA_CONTAINER_SELECTOR = "div.main-div-container"
# Strongest, most specific signal that a challenge is on screen.
CAPTCHA_PROMPT_TEXT = "please select all boxes with number"
CAPTCHA_IMG_SELECTOR = "img.captcha-img"
CAPTCHA_SELECTED_CLASS = "img-selected"
CAPTCHA_SELECTED_INPUT_SELECTOR = 'input[name="SelectedImages"]'
CAPTCHA_NUMBER_REGEX = re.compile(r"number\s+(\d+)", re.I)
# CONFIRMED markup of the verification modal's footer. Each control is a DIV
# whose onclick calls a named function; the visible text sits in a child <p>,
# so matching the text finds an unclickable element:
#
#   <div class="col-4 text-center img-action-div" onclick="onUndo();">
#       <i id="undo"></i><p class="img-action-text">Clear Selection</p></div>
#   <div ... onclick="onReload();">  ... Reload Images
#   <div ... onclick="onSubmit();">  ... Submit Selection
#       <input id="SelectedImages" ...><input id="Id" ...>
#
# Calling the functions directly is far more reliable than hunting for an
# element to click, so that is tried first with selectors as a fallback.
CAPTCHA_SUBMIT_FN = "onSubmit"
CAPTCHA_CLEAR_FN = "onUndo"
CAPTCHA_RELOAD_FN = "onReload"
CAPTCHA_ACTION_DIV = "div.img-action-div"

CAPTCHA_SUBMIT_SELECTORS = (
    'div.img-action-div[onclick*="onSubmit"]',
    '[onclick*="onSubmit"]',
    'button:has-text("Submit")',
    'input[value="Submit"]',
)
CAPTCHA_SUBMIT_TEXTS = ("submit selection", "submit")
# Clearing between attempts matters because Select() TOGGLES: re-clicking a tile
# that is already chosen unselects it.
CAPTCHA_CLEAR_TEXTS = ("clear selection", "clear")
# Solving is cheap and the grid reloads on every rejection, so retry hard before
# bothering a human. Escalating after 3 made MANUAL_REQUIRED alerts far too
# frequent.
# Each attempt is a real POST to the portal. 10 was too aggressive and helped
# escalate rate limiting into an IP block; 4 still absorbs the occasional
# misread without hammering.
CAPTCHA_MAX_ATTEMPTS = 4
CAPTCHA_CLICK_DELAY = 0.8          # between tile clicks
CAPTCHA_SELECT_CONFIRM_MS = 2000   # wait for img-selected before moving on
CAPTCHA_PRE_SUBMIT_WAIT = 1.0      # settle time after the last click

# One pass over the DOM: resolve every visible tile to the number its label
# states, and report which labels are prompts rather than per-tile labels.
#
# Visibility is decided by the inline "display:block" marker (whitespace
# normalised, so both "display: block" and "display:block" match) AND by an
# ancestor walk - an inline display:block inside a hidden grid still computes to
# "block", so the inline marker alone is not sufficient.
_CAPTCHA_DETECT_JS = r"""
(args) => {
  const { containerSel, tileSel, imgSel, labelSel, promptText } = args;
  const needle = (promptText || '').toLowerCase();
  const bodyText = (document.body ? (document.body.innerText || '') : '').toLowerCase();

  const containers = Array.from(document.querySelectorAll(containerSel));
  // A container only counts as a captcha signal when it actually holds captcha
  // content. "main-div-container" may well be a generic page wrapper present on
  // every page - treating its mere existence as "captcha" would make every
  // normal login look like a challenge and stall the bot.
  let containerHasCaptcha = false;
  for (const c of containers) {
    if (c.querySelector(imgSel) || c.querySelector(tileSel) || c.querySelector(labelSel)) {
      containerHasCaptcha = true;
      break;
    }
    if ((c.innerText || '').toLowerCase().includes(needle)) {
      containerHasCaptcha = true;
      break;
    }
  }

  return {
    promptTextFound: needle ? bodyText.includes(needle) : false,
    containerCount: containers.length,
    containerHasCaptcha: containerHasCaptcha,
    captchaImgCount: document.querySelectorAll(imgSel).length,
    tileCount: document.querySelectorAll(tileSel).length,
    labelCount: document.querySelectorAll(labelSel).length,
  };
}
"""

_CAPTCHA_SCAN_JS = r"""
(args) => {
  const { tileSel, labelSel, imgSel, selectedClass } = args;

  const numFrom = (txt) => {
    const m = /number\s+(\d+)/i.exec(txt || '');
    return m ? m[1] : null;
  };

  const ancestorsVisible = (el) => {
    let n = el;
    while (n && n.nodeType === 1) {
      const s = getComputedStyle(n);
      if (s.display === 'none') return false;
      if (s.visibility === 'hidden' || s.visibility === 'collapse') return false;
      if (parseFloat(s.opacity || '1') < 0.1) return false;
      n = n.parentElement;
    }
    return true;
  };

  // --- colour contrast ------------------------------------------------
  // CONFIRMED: all 31 prompts are stacked at the same spot
  // (.box-label { position:absolute; top:20px }) so every one of them is
  // "visible" by display/opacity. The decoys are painted in the container's
  // own background colour (#F0FFF0) and only the live prompt keeps readable
  // dark text. Contrast against the backdrop is therefore the discriminator -
  // it is what actually decides whether a human can read the text.
  const parseRGB = (s) => {
    const m = /rgba?\(([^)]+)\)/.exec(s || '');
    if (!m) return null;
    const p = m[1].split(',').map((x) => parseFloat(x.trim()));
    if (p.length < 3 || p.some(isNaN)) return null;
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  };

  const relLum = (c) => {
    const f = (v) => {
      v = v / 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  };

  const backdropOf = (el) => {
    let n = el;
    while (n && n.nodeType === 1) {
      const c = parseRGB(getComputedStyle(n).backgroundColor);
      if (c && c.a > 0.1) return c;
      n = n.parentElement;
    }
    return { r: 255, g: 255, b: 255, a: 1 };
  };

  const contrastOf = (el) => {
    const st = getComputedStyle(el);
    const fg = parseRGB(st.color);
    if (!fg || fg.a < 0.1) return 0;
    const bg = backdropOf(el);
    const l1 = relLum(fg);
    const l2 = relLum(bg);
    const hi = Math.max(l1, l2);
    const lo = Math.min(l1, l2);
    return (hi + 0.05) / (lo + 0.05);
  };

  const inlineBlock = (el) => {
    const st = (el.getAttribute('style') || '').replace(/\s+/g, '');
    return st.includes('display:block');
  };

  const allTiles = Array.from(document.querySelectorAll(tileSel));
  const allLabels = Array.from(document.querySelectorAll(labelSel));
  const labelIndex = new Map(allLabels.map((el, i) => [el, i]));

  // Which label states this tile's number? Try, in order: a label inside the
  // tile, the nearest preceding label sibling, the sole label in the tile's row,
  // the tile's own text, then image attributes.
  const labelFor = (tile) => {
    const own = tile.querySelector(labelSel);
    if (own && numFrom(own.innerText)) {
      return { num: numFrom(own.innerText), src: 'descendant', idx: labelIndex.get(own) };
    }
    let sib = tile.previousElementSibling;
    while (sib) {
      if (sib.matches && sib.matches(labelSel) && numFrom(sib.innerText)) {
        return { num: numFrom(sib.innerText), src: 'prev-sibling', idx: labelIndex.get(sib) };
      }
      sib = sib.previousElementSibling;
    }
    const parent = tile.parentElement;
    if (parent) {
      const inRow = Array.from(parent.querySelectorAll(labelSel));
      if (inRow.length === 1 && numFrom(inRow[0].innerText)) {
        return { num: numFrom(inRow[0].innerText), src: 'row-single', idx: labelIndex.get(inRow[0]) };
      }
    }
    const selfNum = numFrom(tile.innerText);
    if (selfNum) return { num: selfNum, src: 'tile-text', idx: null };

    const img = tile.querySelector(imgSel) || tile.querySelector('img');
    if (img) {
      for (const attr of ['alt', 'title', 'data-number', 'data-value']) {
        const v = (img.getAttribute(attr) || '').trim();
        const n = numFrom(v) || (/^\d+$/.test(v) ? v : null);
        if (n) return { num: n, src: 'img-' + attr, idx: null };
      }
    }
    return { num: null, src: 'unresolved', idx: null };
  };

  const tiles = [];
  const claimed = new Set();
  allTiles.forEach((tile, domIndex) => {
    // Visibility is decided by computed style plus a real bounding box, not by
    // the inline display:block marker alone — a grid can be hidden by a class
    // instead, and relying on the inline string missed a tile on live runs.
    if (!ancestorsVisible(tile)) return;
    const r = tile.getBoundingClientRect();
    if (r.width < 10 || r.height < 10) return;

    // CRITICAL: several grids are stacked at the same coordinates. They all pass
    // display/opacity/size checks, so those alone reported 36 "visible" tiles
    // across 4 grids - and positions then mapped into grids sitting BEHIND the
    // front one, whose clicks the front grid intercepts.
    // elementFromPoint answers the only question that matters: at this tile's
    // centre, is this tile the thing a user would actually hit?
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    if (cx < 0 || cy < 0 || cx > window.innerWidth || cy > window.innerHeight) return;
    const topEl = document.elementFromPoint(cx, cy);
    if (!topEl) return;
    if (!(topEl === tile || tile.contains(topEl))) return;

    // A tile MUST hold a captcha image. The modal's footer controls
    // ("Clear Selection", "Reload Images", "Submit Selection") are col-4 divs
    // too, and counting them gave 12 tiles for a 3x3 grid — which broke the
    // per-tile read (9 crops vs 12 expected) and made the fallback positions
    // index into the wrong list.
    // CONFIRMED: the footer controls are
    //   <div class="col-4 text-center img-action-div" onclick="onUndo()|onReload()|onSubmit()">
    // so they match div.col-4 too. Exclude them explicitly, and require a real
    // captcha image — counting them gave 12 tiles for a 3x3 grid.
    if (tile.classList.contains('img-action-div')) return;
    if (tile.getAttribute('onclick')) return;
    const img = tile.querySelector(imgSel) || tile.querySelector('img');
    if (!img) return;
    const info = labelFor(tile);
    if (info.idx !== null && info.idx !== undefined) claimed.add(info.idx);
    tiles.push({
      domIndex: domIndex,
      id: tile.id || (img && img.id) || '',
      num: info.num,
      labelSource: info.src,
      hasImg: !!img,
      selected: img ? img.classList.contains(selectedClass) : false,
      inlineBlock: inlineBlock(tile),
      rect: { x: r.x + window.scrollX, y: r.y + window.scrollY, w: r.width, h: r.height },
    });
  });

  // Every stacked label is "visible"; the readable one is the one with contrast.
  const prompts = [];
  const visibleLabels = [];
  allLabels.forEach((lab, i) => {
    if (!ancestorsVisible(lab)) return;
    const n = numFrom(lab.innerText);
    if (!n) return;
    const st = getComputedStyle(lab);
    const entry = {
      idx: i,
      num: n,
      text: (lab.innerText || '').trim().slice(0, 120),
      contrast: Math.round(contrastOf(lab) * 100) / 100,
      zIndex: st.zIndex,
      color: st.color,
    };
    visibleLabels.push(entry);
    if (!claimed.has(i) && !lab.closest(tileSel)) prompts.push(entry);
  });

  return {
    tiles: tiles,
    prompts: prompts,
    visibleLabels: visibleLabels,
    totalTiles: allTiles.length,
    totalLabels: allLabels.length,
  };
}
"""

# Candidate containers for a captcha / challenge widget, dumped verbatim when one
# is detected so the markup can be analysed without re-triggering a login.
CAPTCHA_DOM_SELECTORS = (
    # BLS's own captcha, first — these are the confirmed containers.
    "div.col-12.box-label",
    "div.col-4",
    "img.captcha-img",
    'input[name="SelectedImages"]',
    # Generic third-party challenges, in case BLS ever swaps provider.
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "iframe[src*='turnstile']",
    "iframe[src*='captcha']",
    ".g-recaptcha",
    ".h-captcha",
    ".cf-turnstile",
    "[class*='captcha']",
    "[id*='captcha']",
    "[class*='Captcha']",
    "[id*='Captcha']",
    "[class*='puzzle']",
    "[class*='slider-verify']",
    "[class*='challenge']",
    "[id*='challenge']",
    "[class*='verify']",
)

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-infobars",
]


class LoginError(RuntimeError):
    """Raised when the bot cannot establish an authenticated session."""


class PortalUnreachableError(LoginError):
    """The portal served an HTTP error page instead of the app.

    Most commonly a 403 from the AWS load balancer, which BLS returns to IPs
    outside Egypt and to datacenter/hosting ASNs. Retrying the login is pointless
    in that state — there is no form on the page to fill — so this is raised
    immediately rather than being left to surface as a confusing
    "could not locate the real email field" further down the flow.
    """


class BLSAuth:
    """Owns the Playwright lifecycle and keeps an authenticated page available."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        state: Any = None,
        notifier: Any = None,
    ) -> None:
        self.config = config
        self.state = state
        self.notifier = notifier
        self.vision = vision.VisionSolver(config)

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._lock = asyncio.Lock()
        self._logged_in = False
        # HTTP status of the most recent captcha submit, set by the network
        # listener. None means no response was seen for this attempt.
        self._last_captcha_submit_status: int | None = None

        self.session_file = Path(SESSION_FILE)

    # ------------------------------------------------------------------ #
    # Browser lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Launch Chromium, build a stealthed context, restore saved cookies."""
        if self._browser is not None:
            return

        browser_cfg = self.config.get("browser", {}) or {}
        proxy_cfg = self.config.get("proxy", {}) or {}

        self._playwright = await async_playwright().start()

        args = list(LAUNCH_ARGS)
        if bool(browser_cfg.get("allow_insecure_content", True)):
            # The portal 302s its own captcha iframe to http://, which Chrome
            # blocks as mixed content *in the renderer* — before the request is
            # issued, so Playwright routing cannot intercept or upgrade it. The
            # only way to let that frame load is to permit insecure subresources.
            args += [
                "--allow-running-insecure-content",
                "--disable-features=BlockInsecurePrivateNetworkRequests",
            ]

        launch_kwargs: dict[str, Any] = {
            "headless": bool(self.config.get("headless", True)),
            "args": args,
        }
        if proxy_cfg.get("enabled") and proxy_cfg.get("server"):
            proxy: dict[str, Any] = {"server": proxy_cfg["server"]}
            if proxy_cfg.get("username"):
                proxy["username"] = proxy_cfg["username"]
                proxy["password"] = proxy_cfg.get("password", "")
            launch_kwargs["proxy"] = proxy
            logger.info(f"auth: using proxy {proxy_cfg['server']}")

        self._browser = await self._playwright.chromium.launch(**launch_kwargs)

        context_kwargs: dict[str, Any] = {
            "locale": browser_cfg.get("locale", "en-US"),
            "timezone_id": browser_cfg.get("timezone", "Africa/Cairo"),
            "viewport": {"width": 1366, "height": 768},
            "ignore_https_errors": True,
            # Renders at higher DPI so captcha crops carry real detail rather
            # than interpolated pixels. CSS coordinates are unaffected, so
            # elementFromPoint and clip rects keep working unchanged.
            "device_scale_factor": float(browser_cfg.get("device_scale_factor", 2)),
        }
        if browser_cfg.get("user_agent"):
            context_kwargs["user_agent"] = browser_cfg["user_agent"]

        if self._has_saved_session():
            context_kwargs["storage_state"] = str(self.session_file)
            logger.info(f"auth: restoring session from {self.session_file}")

        self._context = await self._browser.new_context(**context_kwargs)
        self._context.set_default_navigation_timeout(
            int(browser_cfg.get("navigation_timeout", 60000))
        )
        self._context.set_default_timeout(int(browser_cfg.get("action_timeout", 20000)))

        # Stealth is now optional. Its injected script throws
        # "Cannot redefine property: offsetHeight" on this portal, which leaves
        # the page half-patched and is a prime suspect for the captcha iframe
        # being served a login redirect. Set browser.stealth: false to rule it
        # out without touching code.
        # Stealth is known-broken on this portal: its script throws
        # "Cannot redefine property: offsetHeight" from inside FingerprintJS's
        # font probing, so the visitor id the server expects is never computed
        # properly — and /Global/NewCaptcha/GenerateCaptcha then refuses the
        # session and 302s to the login page.
        #
        # config.yaml is gitignored, so a stale `stealth: true` there cannot be
        # corrected by a pull. Treat the config value as a request, not an
        # order: ignore it unless browser.force_stealth is explicitly set.
        wants_stealth = bool(browser_cfg.get("stealth", False))
        force_stealth = bool(browser_cfg.get("force_stealth", False))
        self._stealth_active = force_stealth or (wants_stealth and False)

        if wants_stealth and not force_stealth:
            logger.error(
                "auth: browser.stealth is true in config.yaml but is being "
                "IGNORED — it breaks FingerprintJS on this portal and causes "
                "GenerateCaptcha to reject the session. Set browser.stealth: "
                "false to silence this, or browser.force_stealth: true to "
                "override."
            )
        if self._stealth_active:
            logger.warning("auth: stealth FORCED ON by browser.force_stealth")
            if await utils.apply_stealth(self._context):
                logger.debug("auth: stealth applied to context")
        else:
            logger.info("auth: stealth disabled (recommended for this portal)")

        await self._install_https_upgrade(self._context)

        self._page = await self._context.new_page()
        if getattr(self, "_stealth_active", False):
            await utils.apply_stealth(self._page)
        self._install_network_logging(self._page)
        self._log("browser launched", status="ok")

    async def _install_https_upgrade(self, context: BrowserContext) -> None:
        """Rewrite the portal's plain-HTTP redirects to HTTPS.

        The captcha iframe is 302'd to
        http://egypt.blsspainglobal.com/Global/Account/LogIn?ReturnUrl=...
        and Chrome blocks it as mixed content on an HTTPS page, so the frame
        never renders and the Kendo spinner hangs forever. The app is almost
        certainly building redirects from the ELB's forwarded http scheme.
        Upgrading the scheme lets the request proceed instead of being blocked.
        """

        async def _upgrade(route: Any, request: Any) -> None:
            url = request.url or ""
            try:
                if url.startswith("http://"):
                    secure = "https://" + url[len("http://"):]
                    logger.warning(f"auth: upgrading insecure request -> {secure[:120]}")
                    await route.continue_(url=secure)
                    return
                await route.continue_()
            except Exception as exc:
                logger.debug(f"auth: route continue failed: {exc}")
                try:
                    await route.continue_()
                except Exception:
                    pass

        try:
            await context.route("http://*/**", _upgrade)
            logger.info("auth: http->https upgrade route installed")
        except Exception as exc:
            logger.warning(f"auth: could not install the https upgrade route: {exc}")

    def _install_network_logging(self, page: Page) -> None:
        """Log the captcha endpoints' responses, including redirects.

        Without this the iframe failure is invisible from Python — the block
        happens in the browser and only shows in the devtools console.
        """
        interesting = ("newcaptcha", "generatecaptcha", "visatypeverification")

        def _on_response(response: Any) -> None:
            url = (response.url or "").lower()
            if any(hint in url for hint in interesting):
                logger.info(f"auth: [net] {response.status} {response.url[:150]}")
                # Remember the captcha POST result. "The captcha is gone" is NOT
                # proof of success: a 403 error page also has no captcha on it,
                # and was being reported as SOLVED.
                if "captchasubmit" in url:
                    self._last_captcha_submit_status = response.status

        def _on_failed(request: Any) -> None:
            url = (request.url or "").lower()
            if any(hint in url for hint in interesting):
                failure = getattr(request, "failure", None)
                logger.warning(f"auth: [net] FAILED {request.url[:130]} — {failure}")

        try:
            page.on("response", _on_response)
            page.on("requestfailed", _on_failed)
        except Exception as exc:
            logger.debug(f"auth: could not install network logging: {exc}")

    async def stop(self) -> None:
        """Close everything, saving the session first on a best-effort basis."""
        if self._context is not None and self._logged_in:
            await self._save_session()
        for closer, label in ((self._context, "context"), (self._browser, "browser")):
            if closer is None:
                continue
            try:
                await closer.close()
            except Exception as exc:
                logger.debug(f"auth: closing {label} failed: {exc}")
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as exc:
                logger.debug(f"auth: stopping playwright failed: {exc}")
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._logged_in = False
        logger.info("auth: browser closed")

    @property
    def page(self) -> Page:
        if self._page is None:
            raise LoginError("browser is not started — call start() first")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise LoginError("browser is not started — call start() first")
        return self._context

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #
    async def ensure_logged_in(self, *, force: bool = False) -> Page:
        """Return an authenticated page, logging in or re-logging in as needed.

        Restored cookies are tried first; a login only happens when the session
        is missing or expired. Safe to call before every monitoring cycle.
        """
        async with self._lock:
            await self.start()
            page = self.page

            try:
                if not force and await self._is_logged_in(page):
                    self._logged_in = True
                    logger.debug("auth: existing session is still valid")
                    return page
            except PortalUnreachableError:
                # Blocked at the edge — there is no form to fill. Do NOT set a
                # manual pause: that would wait for a human /resume forever. The
                # monitor waits out the cooldown and retries by itself.
                self._logged_in = False
                raise

            attempts = max(1, int((self.config.get("retry", {}) or {}).get("login_attempts", 2)))
            last_error: Exception | None = None
            for attempt in range(1, attempts + 1):
                logger.info(f"auth: logging in (attempt {attempt}/{attempts})")
                try:
                    if await self._perform_login(page):
                        self._logged_in = True
                        await self._save_session()
                        self._log("login successful", status="ok")
                        return page
                    last_error = LoginError("login did not reach an authenticated page")
                except PortalUnreachableError:
                    self._logged_in = False
                    raise
                except PlaywrightTimeout as exc:
                    last_error = exc
                    logger.warning(f"auth: timeout during login: {exc}")
                except Exception as exc:
                    last_error = exc
                    logger.warning(f"auth: login attempt failed: {exc}")

                await utils.screenshot(page, f"login-failed-attempt{attempt}")
                if attempt < attempts:
                    await asyncio.sleep(5 * attempt)

            self._logged_in = False
            message = f"Login failed after {attempts} attempt(s): {last_error}"
            self._log(message, status="error")
            # No manual pause: the monitor backs off and retries from scratch.
            # A human /resume must never be required for the bot to recover.
            await self._alert_error(
                message, retry_info="retrying automatically after a back-off"
            )
            raise LoginError(message)

    async def refresh_session(self) -> Page:
        """Force a fresh login, discarding any stored cookies."""
        self._clear_saved_session()
        return await self.ensure_logged_in(force=True)

    # ------------------------------------------------------------------ #
    # Login flow
    # ------------------------------------------------------------------ #
    async def _perform_login(self, page: Page) -> bool:
        """Two-step login, confirmed against the live portal:

        1. Email page -> Verify
        2. Password page, which also carries the CAPTCHA
        3. CAPTCHA solved + submitted -> redirect to /Global/bls/visatypeverification

        There is no /account or /dashboard page on this portal — probing for one
        returns 404. Success is therefore judged by the landing URL.
        """
        await self._goto(page, self._path("login"))
        await utils.human_delay(self.config)
        await self._dismiss_cookie_banner(page)

        # --- Step 1: email -------------------------------------------------
        email = (self.config.get("bls", {}) or {}).get("email", "")
        if not email:
            raise LoginError("bls.email is empty in config.yaml")

        if not await self._fill_email(page, email):
            await utils.screenshot(page, "login-no-email-field")
            raise LoginError("could not locate the real email field among the honeypots")
        self._log("email submitted", status="ok")

        await utils.human_delay(self.config)
        await self._click_verify(page)
        await self._settle(page)

        # --- Step 2: password ---------------------------------------------
        password = (self.config.get("bls", {}) or {}).get("password", "")
        if not password:
            raise LoginError("bls.password is empty in config.yaml")

        if not await self._fill_password(page, password):
            await utils.screenshot(page, "login-no-password-field")
            raise LoginError("password field did not appear after the email step")
        self._log("password filled", status="ok")

        # Evidence of the page as filled, before anything can navigate away.
        logger.info(f"auth: password-page URL is {page.url}")
        await utils.screenshot(page, "password-page-filled")
        await utils.dump_page_html(page, "password-page-filled")

        # --- CAPTCHA lives on the password page ----------------------------
        # Solve it BEFORE submitting. An unsolved captcha blocks the POST, which
        # is exactly what earlier runs showed: no /Global/account/LoginSubmit
        # response within 20s after clicking Login.
        logger.info("auth: checking for a CAPTCHA on the password page")
        if not await self.solve_captcha(page):
            await self._check_for_captcha(page)  # manual /resume fallback

        # Solving the captcha can complete the login by itself (the captcha form
        # posts the credentials). Check before submitting again — otherwise
        # _submit_login waits the full 20s for a LoginSubmit response that will
        # never come, which cost ~20s on every successful login.
        await utils.human_delay(self.config)
        if self._authenticated_url(page.url or ""):
            logger.info("auth: already authenticated after the captcha — skipping resubmit")
        else:
            await self._submit_login(page)
            await self._settle(page)
        self._log("password submitted", status="ok")

        # Snapshot the page state immediately, before any waiting, so the real
        # post-submit state is always on record.
        logger.info(f"auth: post-submit URL is {page.url}")
        await utils.screenshot(page, "after-password-submit")
        await utils.dump_page_html(page, "after-password-submit")

        # Wait for whichever lands first: the authenticated URL, or another
        # CAPTCHA. A rejected challenge re-renders rather than redirecting, and
        # it can take a moment to appear — checking the URL alone gives up early.
        for round_no in range(1, CAPTCHA_MAX_ATTEMPTS + 1):
            outcome = await self._await_login_outcome(page, timeout_s=30.0)

            if outcome == "success":
                logger.success(f"auth: authenticated URL reached — {page.url}")
                break

            if outcome == "captcha":
                logger.info("auth: CAPTCHA detected — solving")
                if not await self.solve_captcha(page):
                    # solve_captcha already exhausted its own retries.
                    await self._check_for_captcha(page)
                    break
                await utils.human_delay(self.config)
                await self._submit_login(page)
                await self._settle(page)
                logger.info(f"auth: URL after submit round {round_no} is {page.url}")
                await utils.screenshot(page, f"after-submit-round{round_no}")
                continue

            logger.warning(
                "auth: 30s timeout — no CAPTCHA and no success URL "
                f"(still at {page.url})"
            )
            await utils.screenshot(page, f"login-stalled-round{round_no}")
            await utils.dump_page_html(page, f"login-stalled-round{round_no}")
            break

        # TODO(unconfirmed): the portal may insert an email/SMS OTP step here.
        # If that is observed, call modules.otp.handle_otp(page, ...) before the
        # check below.
        return await self._is_logged_in(page, navigate=False)

    @staticmethod
    def _authenticated_url(url: str) -> bool:
        """True only when the URL *path* is an authenticated route.

        Matches the path alone, never the query string. The login page is served
        as ``/Global/Account/LogIn?ReturnUrl=%2FGlobal%2Fbls%2Fvisatypeverification``
        — searching the whole URL finds "visatypeverification" in that parameter
        and reports success while sitting on the login page.
        """
        try:
            path = (urlsplit(url or "").path or "").lower()
        except Exception:
            return False
        if not path or "/account/login" in path or "/newcaptcha/" in path:
            return False
        return any(hint in path for hint in LOGGED_IN_URL_HINTS)

    async def _await_login_outcome(self, page: Page, timeout_s: float = 30.0) -> str:
        """After submitting, wait for whichever comes first.

        Returns "success" (authenticated URL), "captcha" (a challenge rendered),
        or "timeout". Polling both beats waiting on the URL alone: the CAPTCHA
        can appear a second or two after the POST, and a URL-only check gives up
        while still sitting on the login page.
        """
        waited = 0.0
        step = 0.5
        while waited < timeout_s:
            if self._authenticated_url(page.url or ""):
                return "success"
            try:
                container = await page.query_selector(CAPTCHA_CONTAINER_SELECTOR)
            except Exception:
                container = None
            if container is not None and (await self._detect_captcha(page))["present"]:
                return "captcha"
            await asyncio.sleep(step)
            waited += step

        if self._authenticated_url(page.url or ""):
            return "success"
        return "timeout"

    async def _fill_email(self, page: Page, email: str) -> bool:
        """Fill the one real email field.

        Located by ``find_real_input`` — the real input is the one whose
        ``div.mb-3`` wrapper computes to ``display: block``. Position, id and name
        all randomise per load, so none of them may be used.
        """
        element = await utils.find_real_input(page, EMAIL_INPUT_TYPE)

        if element is None:
            # No wrapper reported display:block. Either the markup changed or the
            # form had not finished rendering — retry once after a short settle.
            logger.warning("auth: no real email field on the first pass — retrying")
            await utils.human_delay(self.config)
            element = await utils.find_real_input(page, EMAIL_INPUT_TYPE)

        if element is None:
            # Last resort: computed-visibility scan (display/opacity/size/offscreen).
            logger.warning(
                "auth: div.mb-3 discriminator found nothing — falling back to the "
                "visibility scan; the login markup may have changed"
            )
            element = await utils.find_visible_input(
                page,
                kinds=("text", "email"),
                keywords=("email", "mail", "user", "بريد"),
            )

        if element is None:
            return False

        await utils.type_like_human(element, email)
        logger.info("auth: email filled")
        return True

    async def _fill_password(self, page: Page, password: str) -> bool:
        """Fill the password field on the second login step.

        TODO(unconfirmed — needs live portal inspection): see
        ``PASSWORD_INPUT_TYPES`` above. This assumes the email step's honeypot
        pattern repeats, so the same ``div.mb-3`` / ``display:block`` check is
        used. Once the real markup is captured, narrow this to the single
        confirmed input type and drop the loop.
        """
        for input_type in PASSWORD_INPUT_TYPES:
            element = await utils.find_real_input(page, input_type)
            if element is None:
                continue
            if input_type == "text":
                # Safety net: a text input only receives the password if it is
                # also genuinely visible, so a mislabelled honeypot can never
                # capture the credential.
                if not await utils.is_really_visible(element):
                    logger.warning(
                        "auth: candidate text field on the password step is not "
                        "genuinely visible — skipping it (honeypot guard)"
                    )
                    continue
            await utils.type_like_human(element, password)
            logger.info(f"auth: password filled (real input[type={input_type!r}])")
            return True

        logger.warning(
            "auth: div.mb-3 discriminator found no password field — "
            "falling back to the visibility scan"
        )
        element = await utils.find_visible_input(
            page, kinds=("password", "text"), keywords=("password", "pass", "مرور")
        )
        if element is None:
            return False
        await utils.type_like_human(element, password)
        logger.info("auth: password filled via visibility fallback")
        return True

    async def _click_verify(self, page: Page) -> bool:
        """Click the step-1 Verify button.

        CONFIRMED: ``#btnVerify``. Button ids are stable on this portal — only the
        *input fields* randomise — so targeting it directly is correct. Falls back
        to a text match, then to Enter, if the markup ever changes.
        """
        try:
            button = page.locator(VERIFY_BUTTON_SELECTOR)
            await button.wait_for(state="visible", timeout=BUTTON_TIMEOUT_MS)
            await button.click(timeout=BUTTON_TIMEOUT_MS)
            logger.info(f"auth: clicked Verify via {VERIFY_BUTTON_SELECTOR}")
            return True
        except PlaywrightTimeout:
            logger.warning(
                f"auth: {VERIFY_BUTTON_SELECTOR} not clickable — falling back to text match"
            )
        except Exception as exc:
            logger.warning(f"auth: {VERIFY_BUTTON_SELECTOR} click failed ({exc}) — falling back")

        if await utils.click_by_text(page, VERIFY_BUTTON_TEXTS, config=self.config):
            return True

        logger.debug("auth: no Verify button found — pressing Enter")
        await page.keyboard.press("Enter")
        return False

    async def _submit_login(self, page: Page) -> bool:
        """Submit the password step and watch the confirmed LoginSubmit POST.

        Observing the POST status is what distinguishes rejected credentials from
        the page silently re-rendering.
        """

        async def _click() -> None:
            if not await utils.click_by_text(page, LOGIN_BUTTON_TEXTS, config=self.config):
                logger.debug("auth: no Login button found — pressing Enter")
                await page.keyboard.press("Enter")

        try:
            async with page.expect_response(
                lambda response: LOGIN_SUBMIT_PATH.lower() in response.url.lower(),
                timeout=20000,
            ) as info:
                await _click()
            response = await info.value
            logger.info(f"auth: {LOGIN_SUBMIT_PATH} responded {response.status}")
            if response.status >= 400:
                self._log(f"login POST returned HTTP {response.status}", status="error")
            return response.status < 400
        except PlaywrightTimeout:
            # Not fatal, but highly diagnostic: if the POST never fired, something
            # on the page blocked submission — a captcha being the prime suspect.
            # Logged at WARNING so it is visible in the run transcript.
            logger.warning(
                f"auth: no {LOGIN_SUBMIT_PATH} response within 20s — the login "
                "POST does not appear to have fired (a captcha or validation "
                "error may be blocking the form)"
            )
            return False
        except Exception as exc:
            logger.debug(f"auth: login submit observation failed: {exc}")
            return False

    # ------------------------------------------------------------------ #
    # CAPTCHA
    # ------------------------------------------------------------------ #
    async def solve_captcha(
        self,
        page: Page,
        *,
        ctx: Any = None,
        offset: tuple[float, float] = (0.0, 0.0),
    ) -> bool:
        """Solve the DOM-based BLS captcha.

        Returns True when there is no captcha, or when one was solved. Returns
        False when a captcha is present but could not be solved — the caller then
        falls back to pausing for a human.

        Per round: scan the DOM once, take the target number from the prompt,
        click the <img> inside every visible ``div.col-4`` whose label states that
        number, confirm each click registered (class ``img-selected``), then
        submit. A rejected selection reloads the grid with a new number, so every
        round re-scans from scratch. Up to ``CAPTCHA_MAX_ATTEMPTS`` rounds.
        """
        # ctx is where the DOM lives (a Page, or the GenerateCaptcha iframe used
        # by the visa-type verification gate). page is always a real Page,
        # because only a Page can take a screenshot; offset converts the frame's
        # coordinates into page coordinates for the grid clip.
        ctx = ctx or page
        signals = await self._detect_captcha(ctx)
        logger.info(f"auth: captcha signals -> {self._signal_summary(signals)}")

        if not signals["present"]:
            logger.info("auth: no CAPTCHA detected")
            return True

        logger.info("auth: CAPTCHA detected, attempting to solve")
        self._log("CAPTCHA detected — solving", status="warn")
        await utils.screenshot(page, "captcha-before")
        # Always keep the markup of a real challenge: if the tile selectors turn
        # out not to match, this dump is what identifies the correct ones.
        await utils.dump_page_html(ctx, "captcha-detected")

        for attempt in range(1, CAPTCHA_MAX_ATTEMPTS + 1):
            # elementFromPoint only resolves inside the viewport, so make sure
            # the grid is on screen before deciding which tiles are visible.
            # Clear the previous attempt's status so a stale 403 cannot be read
            # as this attempt's result.
            self._last_captcha_submit_status = None
            await self._scroll_captcha_into_view(ctx)
            scan = await self._captcha_scan(ctx)
            if scan is None:
                await utils.screenshot(page, f"captcha-scan-failed-attempt{attempt}")
                await utils.dump_page_html(ctx, f"captcha-scan-failed-attempt{attempt}")
                return False

            tiles = scan.get("tiles") or []
            target = self._captcha_target(scan)

            if not tiles:
                # Usually transient: mid-reload, or the grid is scrolled out of
                # the viewport so elementFromPoint cannot see it. Scroll it into
                # view and rescan before treating this as a real failure.
                logger.warning(
                    f"auth: no visible tile on attempt {attempt} "
                    f"(DOM has {scan.get('totalTiles')} col-4 divs, "
                    f"{scan.get('totalLabels')} box-labels) — rescanning"
                )
                await self._scroll_captcha_into_view(ctx)
                await asyncio.sleep(1.0)
                scan = await self._captcha_scan(ctx) or {}
                tiles = scan.get("tiles") or []
                target = self._captcha_target(scan) if scan else None

            if not tiles:
                logger.error(
                    "auth: CAPTCHA is on screen but no tile is visible after a "
                    "rescan — the tile selectors may need updating"
                )
                await utils.screenshot(page, f"captcha-no-tiles-attempt{attempt}")
                await utils.dump_page_html(ctx, f"captcha-no-tiles-attempt{attempt}")
                if attempt < CAPTCHA_MAX_ATTEMPTS:
                    await self._reload_captcha(ctx)
                    continue
                self._log("CAPTCHA tiles not found", status="error")
                return False

            # --- attempt header: everything needed to debug from the log alone --
            logger.info("-" * 60)
            logger.info(f"CAPTCHA attempt {attempt} of {CAPTCHA_MAX_ATTEMPTS}")
            logger.info(f"  target number   : {target}")
            logger.info(
                f"  visible tiles   : {len(tiles)} "
                f"(of {scan.get('totalTiles')} col-4 divs in the DOM)"
            )
            logger.info(
                f"  visible labels  : {len(scan.get('visibleLabels') or [])} "
                f"(of {scan.get('totalLabels')} box-labels in the DOM)"
            )
            self._log(
                f"captcha attempt {attempt}: target={target}, {len(tiles)} visible tiles",
                status="warn",
            )

            if target is None:
                logger.warning("auth: captcha present but no target number found")
                await utils.screenshot(page, f"captcha-no-target-attempt{attempt}")
                await utils.dump_page_html(ctx, f"captcha-no-target-attempt{attempt}")
                if attempt < CAPTCHA_MAX_ATTEMPTS:
                    await self._reload_captcha(ctx)
                    continue
                return False

            # --- Tile recognition -------------------------------------------
            # The tile digits exist ONLY as pixels: each tile holds a single
            # base64 <img> with no text, alt or data attribute, and the images
            # are unique on every load (so a hash lookup table cannot work).
            # Reading the grid with vision is the only way to solve this.
            ordered = self._order_tiles_visually(tiles)
            logger.info(
                "  grid order      : "
                + ", ".join(f"{i}:{t.get('id') or '?'}" for i, t in enumerate(ordered, 1))
            )
            if len(ordered) != 9:
                # The challenge is a 3x3 grid. Anything else means the visibility
                # filter is picking up stacked grids again, which silently
                # corrupts the position -> tile mapping.
                logger.warning(
                    f"  grid size       : {len(ordered)} tiles — expected 9. "
                    "The visible-tile filter may be matching stacked grids."
                )

            grid_png = await self._screenshot_grid(ctx, ordered, attempt, shot_page=page, offset=offset)
            # Save exactly what the model is sent (upscaled), not the raw crop —
            # otherwise the audit image and the model's input differ.
            sample = vision.save_sample(
                vision.upscale_png(grid_png),
                f"{_sample_stamp()}_target{target}_attempt{attempt}",
            )
            logger.info(f"  grid screenshot : {sample or 'FAILED'}")

            if not grid_png:
                logger.warning(
                    f"  outcome         : RETRY - could not capture the grid "
                    f"(attempt {attempt}/{CAPTCHA_MAX_ATTEMPTS})"
                )
                if attempt < CAPTCHA_MAX_ATTEMPTS:
                    await self._reload_captcha(ctx)
                    continue
                self._log("captcha failed — grid could not be captured", status="error")
                return False

            # Preferred: have the model READ all nine tiles and match in Python.
            # Pure OCR is an easier ask than "OCR + compare + report positions",
            # and it puts every digit it read into the log so a misread is
            # visible instead of hidden behind a position list.
            positions: list[int] | None = None
            method = "per-tile"
            # Per-tile first: human labels showed the whole-grid read fails by
            # losing track of which tile is which, not by misreading digits.
            digits = await self.vision.read_tiles_individually(
                grid_png, count=len(ordered)
            )
            if digits is None:
                method = "whole-grid"
                digits = await self.vision.read_grid_numbers(
                    grid_png, count=len(ordered)
                )
            if digits is not None:
                logger.info(
                    "  tiles read      : "
                    + ", ".join(f"{i}:{d}" for i, d in enumerate(digits, 1))
                )
                positions = [i for i, d in enumerate(digits, 1) if d == target]
                logger.info(f"  matching {target}      : positions {positions}")
            else:
                logger.warning(
                    "  tiles read      : unusable — falling back to asking for "
                    "positions directly"
                )
                method = "positions"
                positions = await self.vision.find_matching_positions(
                    grid_png, target, count=len(ordered)
                )

            # Record what was read so tools/grade_captcha.py can score it against
            # the human labels in ground_truth.json.
            vision.record_reading(sample or "", target, digits, positions, method)

            if positions is None:
                # Transient API errors are common; only give up once the whole
                # retry budget is gone, so a human is not pestered for a blip.
                logger.warning(
                    f"  outcome         : RETRY - vision lookup unavailable "
                    f"(attempt {attempt}/{CAPTCHA_MAX_ATTEMPTS})"
                )
                if attempt < CAPTCHA_MAX_ATTEMPTS:
                    await utils.human_delay(self.config)
                    continue
                self._log("captcha: vision lookup failed", status="error")
                return False

            logger.info(f"  vision positions: {positions}")

            if not positions:
                logger.warning(
                    f"  vision reported no tile shows {target} — treating the "
                    "reading as wrong and retrying with a fresh grid"
                )
                if attempt < CAPTCHA_MAX_ATTEMPTS:
                    await self._reload_captcha(ctx)
                    continue
                return False

            matches = [ordered[p - 1] for p in positions]
            matched_ids = [t.get("id") or "?" for t in matches]
            logger.info(f"  matched tiles   : {len(matches)} -> {matched_ids}")

            # Select() toggles — start every attempt from a clean slate.
            await self._clear_captcha_selection(ctx)
            clicked_ok, confirmations = await self._captcha_click_tiles(ctx, matches)

            logger.info("  click results   :")
            for entry in confirmations:
                logger.info(
                    f"      id={entry['id']:<14} number={entry['number']:<6} "
                    f"clicked={entry['clicked']}  "
                    f"img-selected={entry['selected_confirmed']}"
                )

            # Let the page finish updating its hidden field before reading it.
            await asyncio.sleep(CAPTCHA_PRE_SUBMIT_WAIT)

            selected = await self._captcha_selected_images(ctx)
            logger.info(f"  SelectedImages  : {selected!r}")

            # Verify the hidden field really holds every tile we chose — this is
            # what the server reads, so a mismatch means submitting a wrong answer.
            expected = {t.get("id") for t in matches if t.get("id")}
            present = {p.strip() for p in (selected or "").replace(";", ",").split(",") if p.strip()}
            missing = expected - present
            extra = present - expected
            logger.info(f"  expected ids    : {sorted(expected)}")

            # ADVISORY ONLY. Some builds populate SelectedImages on submit rather
            # than on each Select() call, so an empty field here does not mean the
            # selection failed. The .img-selected class is the authoritative
            # signal that the page registered a tile — treating a mismatch as
            # fatal aborted perfectly good selections.
            if not present:
                logger.info(
                    "  SelectedImages  : empty — the page likely fills it on "
                    "submit; relying on img-selected confirmations instead"
                )
            elif missing or extra:
                logger.warning(
                    f"  SelectedImages differs — missing={sorted(missing)} "
                    f"unexpected={sorted(extra)} (proceeding; img-selected is "
                    "the authoritative signal)"
                )
            else:
                logger.info("  SelectedImages  : matches the intended selection")

            # Visual record of the selection actually made, before submitting.
            await utils.screenshot(page, f"captcha-attempt{attempt}-before-submit")

            if not clicked_ok:
                logger.warning(
                    f"  outcome         : RETRY - not every tile registered "
                    f"(attempt {attempt}/{CAPTCHA_MAX_ATTEMPTS})"
                )
                await utils.screenshot(page, f"captcha-click-failed-attempt{attempt}")
                if attempt < CAPTCHA_MAX_ATTEMPTS:
                    await self._reload_captcha(ctx)
                    continue
                return False

            # CRITICAL: a rejected captcha reloads the page with a fresh grid and
            # an EMPTY password box. Without refilling, every retry submits a
            # blank password and the portal answers "Please enter your account
            # password" — which looks like a captcha rejection but is not.
            await self._ensure_password_filled(ctx)

            submitted = await self._click_captcha_submit(ctx)
            logger.info(f"  submit clicked  : {submitted}")
            if not submitted:
                # Do not abort: a failed submit usually means the modal moved on
                # (re-rendered, detached, or the control is not wired yet).
                # Pull a fresh grid via onReload() and try the whole attempt
                # again, the same way a rejected answer is handled.
                logger.warning(
                    f"  outcome         : RETRY - submit did not fire "
                    f"(attempt {attempt}/{CAPTCHA_MAX_ATTEMPTS})"
                )
                await utils.screenshot(page, f"captcha-no-submit-attempt{attempt}")
                await utils.dump_page_html(ctx, f"captcha-no-submit-attempt{attempt}")
                if attempt < CAPTCHA_MAX_ATTEMPTS:
                    await self._clear_captcha_selection(ctx)
                    await self._reload_captcha(ctx)
                    continue
                self._log("captcha failed — submit never fired", status="error")
                return False

            await self._settle(page)

            # A rejected POST produces an error page, which contains no captcha
            # and therefore used to be mistaken for success. Check the status.
            status = self._last_captcha_submit_status
            if status is not None and status >= 400:
                logger.error(
                    f"  outcome         : SERVER REJECTED - captcha submit "
                    f"returned HTTP {status}"
                )
                if status == 403:
                    logger.error(
                        "  HTTP 403 on the captcha POST is the same awselb "
                        "signature as the geo-block. The IP may now be rate "
                        "limited after repeated automated attempts — back off "
                        "before retrying."
                    )
                    self._log("captcha POST blocked (403) — backing off", status="error")
                    await utils.screenshot(page, f"captcha-403-attempt{attempt}")
                    await utils.dump_page_html(ctx, f"captcha-403-attempt{attempt}")
                    return False
                await utils.screenshot(page, f"captcha-rejected-{status}-attempt{attempt}")
                if attempt < CAPTCHA_MAX_ATTEMPTS:
                    await self._reload_captcha(ctx)
                    continue
                return False

            if not await self._captcha_present(ctx):
                logger.success(f"  outcome         : SOLVED on attempt {attempt}")
                logger.info("-" * 60)
                self._log(f"captcha solved (attempt {attempt})", status="ok")
                await utils.screenshot(page, "captcha-solved")
                return True

            # The portal's own message says WHY it was rejected — a wrong grid
            # and a blank password look identical without it.
            banner = await self._page_error_banner(ctx)
            if banner:
                logger.warning(f"  portal message  : {banner!r}")

            logger.warning(
                f"  outcome         : REJECTED - captcha still present after "
                f"attempt {attempt}, grid reloaded"
            )
            logger.info("-" * 60)
            self._log(f"captcha attempt {attempt} rejected", status="warn")
            await utils.screenshot(page, f"captcha-attempt{attempt}-rejected")
            await utils.human_delay(self.config)

        logger.error(f"auth: captcha not solved after {CAPTCHA_MAX_ATTEMPTS} attempts")
        self._log(f"captcha unsolved after {CAPTCHA_MAX_ATTEMPTS} attempts", status="error")
        await utils.screenshot(page, "captcha-failed")
        await utils.dump_page_html(ctx, "captcha-failed")
        return False

    @staticmethod
    def _order_tiles_visually(tiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Sort tiles left-to-right, top-to-bottom — the order the model sees.

        DOM order is not reliable here, and grid positions returned by the vision
        model are defined visually. Rows are grouped with a tolerance of half a
        tile height so minor sub-pixel differences do not split a row.
        """
        placed = [t for t in tiles if t.get("rect")]
        if not placed:
            return list(tiles)

        heights = [t["rect"]["h"] for t in placed if t["rect"]["h"] > 0]
        tolerance = (sum(heights) / len(heights) / 2) if heights else 20

        rows: list[list[dict[str, Any]]] = []
        for tile in sorted(placed, key=lambda t: t["rect"]["y"]):
            for row in rows:
                if abs(row[0]["rect"]["y"] - tile["rect"]["y"]) <= tolerance:
                    row.append(tile)
                    break
            else:
                rows.append([tile])

        ordered: list[dict[str, Any]] = []
        for row in rows:
            ordered.extend(sorted(row, key=lambda t: t["rect"]["x"]))
        return ordered

    async def _screenshot_grid(
        self,
        ctx: Any,
        ordered: list[dict[str, Any]],
        attempt: int,
        *,
        shot_page: Page | None = None,
        offset: tuple[float, float] = (0.0, 0.0),
    ) -> bytes | None:
        """Capture just the tile grid, as PNG bytes.

        Clipped to the union of the tile rects rather than the whole container,
        so the image holds only the puzzle — no email address or other account
        detail leaves the machine, and nothing identifying lands in the samples
        folder that gets committed to git.
        """
        tile_ids = [t.get("id") for t in ordered if t.get("id")]
        if tile_ids:
            # Rects must be re-read in VIEWPORT space here. The scan stores
            # document coordinates (rect + scrollX/scrollY), but a non-fullPage
            # screenshot clips against the viewport — on a scrolled page those
            # differ and the clip lands outside the image. Scroll the grid into
            # view first, then measure fresh and clamp to the viewport.
            try:
                box = await ctx.evaluate(
                    """
                    (args) => {
                      const els = args.ids
                        .map((id) => document.getElementById(id))
                        .filter(Boolean);
                      if (!els.length) return null;
                      els[0].scrollIntoView({ block: 'center', inline: 'center' });
                      let l = Infinity, t = Infinity, r = -Infinity, b = -Infinity;
                      for (const el of els) {
                        const q = el.getBoundingClientRect();
                        l = Math.min(l, q.left);
                        t = Math.min(t, q.top);
                        r = Math.max(r, q.right);
                        b = Math.max(b, q.bottom);
                      }
                      const pad = args.pad;
                      l = Math.max(0, l - pad);
                      t = Math.max(0, t - pad);
                      r = Math.min(window.innerWidth, r + pad);
                      b = Math.min(window.innerHeight, b + pad);
                      return { x: l, y: t, width: r - l, height: b - t,
                               vw: window.innerWidth, vh: window.innerHeight,
                               tiles: els.length };
                    }
                    """,
                    {"ids": tile_ids, "pad": 10},
                )
            except Exception as exc:
                logger.warning(f"auth: could not measure the grid: {exc}")
                box = None

            if box and box["width"] > 10 and box["height"] > 10:
                # Frame coordinates are relative to the frame; the page
                # screenshot needs page coordinates, so shift by the
                # iframe's own position.
                clip = {
                    "x": round(box["x"] + offset[0], 2),
                    "y": round(box["y"] + offset[1], 2),
                    "width": round(box["width"], 2),
                    "height": round(box["height"], 2),
                }
                logger.info(
                    f"  grid clip       : x={clip['x']} y={clip['y']} "
                    f"w={clip['width']} h={clip['height']} "
                    f"(viewport {box['vw']}x{box['vh']}, {box['tiles']} tiles)"
                )
                try:
                    await asyncio.sleep(0.2)  # let the scroll settle
                    return await (shot_page or ctx).screenshot(clip=clip)
                except Exception as exc:
                    logger.warning(f"auth: clipped grid screenshot failed: {exc}")
            else:
                logger.warning(f"auth: grid measurement unusable: {box}")

        # Fall back to the captcha container element.
        try:
            container = await ctx.query_selector(CAPTCHA_CONTAINER_SELECTOR)
            if container is not None:
                return await container.screenshot()
        except Exception as exc:
            logger.warning(f"auth: container screenshot failed: {exc}")

        logger.error(f"auth: no way to capture the captcha grid (attempt {attempt})")
        return None

    async def _scroll_captcha_into_view(self, page: Page) -> None:
        """Bring the grid on screen so elementFromPoint can resolve the tiles.

        Tile visibility is decided with elementFromPoint, which only answers for
        points inside the viewport. A grid pushed below the fold (an error banner
        above it is enough) therefore reads as zero visible tiles.
        """
        try:
            await page.evaluate(
                """
                (sel) => {
                  const el = document.querySelector(sel)
                         || document.querySelector('img.captcha-img');
                  if (el) el.scrollIntoView({ block: 'center', inline: 'center' });
                }
                """,
                CAPTCHA_CONTAINER_SELECTOR,
            )
            await asyncio.sleep(0.3)
        except Exception as exc:
            logger.debug(f"auth: could not scroll the captcha into view: {exc}")

    async def _reload_captcha_action(self, ctx: Any) -> bool:
        """Ask the modal for a fresh grid via its own onReload() handler."""
        return await self._call_captcha_action(ctx, CAPTCHA_RELOAD_FN)

    async def _reload_captcha(self, ctx: Any) -> None:
        """Ask for a fresh grid after an unusable reading or a failed submit.

        ``ctx`` may be a Page or the GenerateCaptcha frame, so nothing here may
        assume Page-only methods.
        """
        # CONFIRMED: the verification modal exposes onReload().
        if await self._reload_captcha_action(ctx):
            await asyncio.sleep(1.0)
            return
        if await utils.click_by_text(
            ctx,
            ("reload images", "reload", "refresh"),
            roles=("button", "a", "div", "span", "label"),
            config=self.config,
        ):
            logger.info("auth: requested a fresh captcha grid")
            await asyncio.sleep(1.0)
        await utils.human_delay(self.config)

    async def _ensure_password_filled(self, page: Page) -> bool:
        """Refill the password box if the page cleared it.

        The captcha and the password share one form. Submitting a wrong captcha
        re-renders the page: new grid, blank password. Returns True when the
        field ends up populated (or when there is no password field here).
        """
        password = (self.config.get("bls", {}) or {}).get("password", "")
        if not password:
            return False

        element = await utils.find_real_input(page, "password")
        if element is None:
            logger.debug("auth: no password field on this page — nothing to refill")
            return True

        try:
            current = await element.evaluate("(el) => el.value || ''")
        except Exception:
            current = ""

        if current:
            logger.debug("auth: password field still populated")
            return True

        logger.info("auth: password box was cleared by the reload — refilling")
        try:
            await utils.type_like_human(element, password)
        except Exception as exc:
            logger.warning(f"auth: could not refill the password: {exc}")
            return False

        try:
            refilled = await element.evaluate("(el) => (el.value || '').length")
        except Exception:
            refilled = 0
        logger.info(f"auth: password refilled ({refilled} chars)")
        return bool(refilled)

    async def _page_error_banner(self, page: Page) -> str | None:
        """Any validation message the portal is showing, for the log."""
        try:
            return await page.evaluate(
                """
                () => {
                  const sel = '.alert, .validation-summary-errors, [class*="error"],'
                            + ' [class*="danger"], [role="alert"]';
                  for (const el of document.querySelectorAll(sel)) {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    if (r.width < 5 || r.height < 5) continue;
                    if (s.display === 'none' || s.visibility === 'hidden') continue;
                    const t = (el.innerText || '').trim();
                    if (t) return t.slice(0, 200);
                  }
                  return null;
                }
                """
            )
        except Exception:
            return None

    async def _captcha_present(self, page: Page) -> bool:
        """True while a captcha challenge is on screen."""
        return (await self._detect_captcha(page))["present"]

    async def _detect_captcha(self, page: Page) -> dict[str, Any]:
        """Decide whether a captcha is on screen, and report every signal.

        Deliberately broader than the tile scan: the page can be a challenge even
        when ``div.col-4`` tiles do not match, and in that case the bot must say
        so loudly rather than report "no captcha" and let the login check fail
        with a misleading error.

        Signals, strongest first:
          * prompt text "Please select all boxes with number" anywhere on the page
          * at least one visible tile found by the full scan
          * ``img.captcha-img`` elements present
          * ``div.main-div-container`` that *contains* captcha content

        The container is only counted when it holds captcha content, because a
        name like "main-div-container" may be a generic page wrapper — treating
        its bare presence as a captcha would misfire on every normal page.
        """
        signals: dict[str, Any] = {
            "promptTextFound": False,
            "containerCount": 0,
            "containerHasCaptcha": False,
            "captchaImgCount": 0,
            "tileCount": 0,
            "labelCount": 0,
            "visibleTiles": 0,
            "present": False,
        }

        try:
            found = await page.evaluate(
                _CAPTCHA_DETECT_JS,
                {
                    "containerSel": CAPTCHA_CONTAINER_SELECTOR,
                    "tileSel": CAPTCHA_TILE_SELECTOR,
                    "imgSel": CAPTCHA_IMG_SELECTOR,
                    "labelSel": CAPTCHA_BOX_LABEL_SELECTOR,
                    "promptText": CAPTCHA_PROMPT_TEXT,
                },
            )
            signals.update(found or {})
        except Exception as exc:
            logger.warning(f"auth: captcha detection failed: {exc}")
            return signals

        scan = await self._captcha_scan(page)
        if scan is not None:
            signals["visibleTiles"] = len(scan.get("tiles") or [])

        signals["present"] = bool(
            signals["promptTextFound"]
            or signals["visibleTiles"] > 0
            or signals["captchaImgCount"] > 0
            or signals["containerHasCaptcha"]
        )
        return signals

    @staticmethod
    def _signal_summary(signals: dict[str, Any]) -> str:
        return (
            f"prompt_text={signals['promptTextFound']} "
            f"visible_tiles={signals['visibleTiles']} "
            f"captcha_imgs={signals['captchaImgCount']} "
            f"container={signals['containerCount']}"
            f"(has_captcha={signals['containerHasCaptcha']}) "
            f"col4_total={signals['tileCount']} "
            f"box_labels={signals['labelCount']}"
        )

    async def _captcha_scan(self, page: Page) -> dict[str, Any] | None:
        """Run the single-pass DOM scan. None when it could not be evaluated."""
        try:
            return await page.evaluate(
                _CAPTCHA_SCAN_JS,
                {
                    "tileSel": CAPTCHA_TILE_SELECTOR,
                    "labelSel": CAPTCHA_BOX_LABEL_SELECTOR,
                    "imgSel": CAPTCHA_IMG_SELECTOR,
                    "selectedClass": CAPTCHA_SELECTED_CLASS,
                },
            )
        except Exception as exc:
            logger.warning(f"auth: captcha DOM scan failed: {exc}")
            return None

    def _captcha_target(self, scan: dict[str, Any]) -> str | None:
        """Pick the target number out of a scan result, by colour contrast.

        CONFIRMED: the page stacks ~31 prompts at one position and paints all but
        one in the container's own background colour (#F0FFF0), leaving a single
        readable label. Document order is meaningless here — a live run saw the
        real prompt (381) sitting at index 18 while index 0 held a decoy (631),
        which is exactly the wrong answer picking "the first" produced.

        So the label with the highest text/background contrast wins: that is, by
        definition, the one a human can actually read.
        """
        candidates = scan.get("prompts") or scan.get("visibleLabels") or []
        if not candidates:
            return None

        ranked = sorted(
            candidates, key=lambda e: float(e.get("contrast") or 0), reverse=True
        )
        best = ranked[0]
        best_contrast = float(best.get("contrast") or 0)

        readable = [e for e in ranked if float(e.get("contrast") or 0) >= 2.0]
        logger.info(
            f"auth: {len(candidates)} stacked prompt(s); "
            f"{len(readable)} readable (contrast >= 2.0)"
        )

        if best_contrast < 2.0:
            # Nothing stands out — the decoy scheme may have changed.
            logger.warning(
                f"auth: no prompt has readable contrast (best={best_contrast} "
                f"num={best['num']}); the colour discriminator may need revisiting"
            )
            return None

        if len(readable) > 1:
            distinct = {e["num"] for e in readable}
            if len(distinct) > 1:
                logger.warning(
                    f"auth: {len(readable)} readable prompts disagree "
                    f"({sorted(distinct)}) — taking the highest contrast"
                )

        logger.info(
            f"auth: prompt {best['num']} chosen "
            f"(contrast={best_contrast}, z-index={best.get('zIndex')}, "
            f"color={best.get('color')})"
        )
        logger.debug(f"auth: prompt text {best['text']!r}")
        return best["num"]

    async def _captcha_click_tiles(
        self, page: Page, matches: list[dict[str, Any]]
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Click the <img> inside each matching tile, verifying each registered.

        Returns ``(all_clicked, confirmations)`` where each confirmation records
        the tile id, its number, whether the click was dispatched, and whether the
        ``img-selected`` class appeared afterwards.

        Tiles are addressed by their position in ``document.querySelectorAll`` so
        the random per-load ids are never used as selectors. Clicking lets the
        page's own ``Select()`` handler populate the hidden ``SelectedImages``
        field — the bot never writes that field itself.
        """
        confirmations: list[dict[str, Any]] = []

        def record(info: dict[str, Any], clicked: bool, confirmed: Any) -> None:
            confirmations.append(
                {
                    "id": str(info.get("id") or "?"),
                    "number": str(info.get("num")),
                    "clicked": clicked,
                    "selected_confirmed": confirmed,
                }
            )

        all_ok = True
        for position, tile_info in enumerate(matches, start=1):
            tile_id = tile_info.get("id") or ""
            if not tile_id:
                logger.warning(f"auth: captcha tile {position} has no id — cannot select")
                record(tile_info, False, "no-id")
                all_ok = False
                continue

            # Call the page's own Select() handler rather than dispatching a
            # synthetic click. Select() is what actually records the choice, and
            # calling it directly sidesteps pointer-interception from the other
            # grids stacked at the same coordinates.
            try:
                result = await page.evaluate(
                    """
                    (id) => {
                      const tile = document.getElementById(id);
                      if (!tile) return 'no-tile';
                      const img = tile.querySelector('img');
                      if (!img) return 'no-img';
                      if (typeof Select !== 'function') return 'no-select-fn';
                      Select(id, img);
                      return 'ok';
                    }
                    """,
                    tile_id,
                )
            except Exception as exc:
                logger.warning(f"auth: Select('{tile_id}') threw: {exc}")
                record(tile_info, False, "select-error")
                all_ok = False
                continue

            if result != "ok":
                logger.warning(f"auth: Select('{tile_id}') could not run: {result}")
                # Fall back to a real click if the page has no Select function.
                if result == "no-select-fn":
                    clicked = await self._click_tile_element(page, tile_id)
                    if not clicked:
                        record(tile_info, False, result)
                        all_ok = False
                        continue
                else:
                    record(tile_info, False, result)
                    all_ok = False
                    continue

            # Wait for the page to mark the tile before touching the next one.
            confirmed = await self._await_tile_selected(page, tile_id)
            logger.debug(
                f"auth: tile {position}/{len(matches)} id={tile_id} "
                f"selected={confirmed}"
            )
            if confirmed is False:
                logger.warning(
                    f"auth: tile {tile_id} never gained .{CAPTCHA_SELECTED_CLASS}"
                )
                all_ok = False

            record(tile_info, True, confirmed)
            await asyncio.sleep(CAPTCHA_CLICK_DELAY)

        return all_ok, confirmations

    async def _await_tile_selected(self, page: Page, tile_id: str) -> Any:
        """Poll until the tile's <img> carries the selected class, or time out."""
        deadline = CAPTCHA_SELECT_CONFIRM_MS / 1000
        waited = 0.0
        step = 0.1
        while waited < deadline:
            try:
                marked = await page.evaluate(
                    """
                    (args) => {
                      const tile = document.getElementById(args.id);
                      if (!tile) return null;
                      const img = tile.querySelector('img');
                      if (!img) return null;
                      return img.classList.contains(args.cls);
                    }
                    """,
                    {"id": tile_id, "cls": CAPTCHA_SELECTED_CLASS},
                )
            except Exception as exc:
                logger.debug(f"auth: selection probe failed for {tile_id}: {exc}")
                return "unknown"
            if marked:
                return True
            if marked is None:
                return "unknown"
            await asyncio.sleep(step)
            waited += step
        return False

    async def _click_tile_element(self, page: Page, tile_id: str) -> bool:
        """Last-resort real click, used only when the page exposes no Select()."""
        try:
            element = await page.query_selector(f"#{tile_id} img")
            if element is None:
                return False
            await element.click(timeout=5000)
            return True
        except Exception as exc:
            logger.warning(f"auth: fallback click on {tile_id} failed: {exc}")
            return False

    async def _captcha_selected_images(self, page: Page) -> str | None:
        """Live value of the hidden SelectedImages field.

        Must read the DOM *property*, not the attribute: Select() assigns
        ``input.value`` in JavaScript, which never updates the static ``value="">``
        attribute. get_attribute() therefore always returned "" and made a
        correct selection look empty.
        """
        try:
            return await page.evaluate(
                """
                (sel) => {
                  const el = document.querySelector(sel);
                  if (!el) return null;
                  return el.value == null ? '' : String(el.value);
                }
                """,
                CAPTCHA_SELECTED_INPUT_SELECTOR,
            )
        except Exception as exc:
            logger.debug(f"auth: could not read SelectedImages: {exc}")
            return None

    async def _call_captcha_action(self, ctx: Any, fn: str) -> bool:
        """Invoke one of the modal's own handlers (onSubmit/onUndo/onReload).

        The control is a div whose onclick calls the function; the readable text
        lives in a child <p>. Calling the function is both more reliable than
        locating the right element and exactly what a real click does.
        """
        try:
            result = await ctx.evaluate(
                """
                (name) => {
                  if (typeof window[name] === 'function') { window[name](); return 'called'; }
                  const el = document.querySelector('[onclick*="' + name + '"]');
                  if (el) { el.click(); return 'clicked'; }
                  return 'missing';
                }
                """,
                fn,
            )
        except Exception as exc:
            logger.debug(f"auth: {fn}() failed: {exc}")
            return False
        if result in ("called", "clicked"):
            logger.info(f"auth: captcha action {fn}() -> {result}")
            return True
        logger.debug(f"auth: {fn}() not present on this page")
        return False

    async def _click_captcha_submit(self, ctx: Any) -> bool:
        # The portal's own handler first — it is what the real click invokes.
        if await self._call_captcha_action(ctx, CAPTCHA_SUBMIT_FN):
            return True

        for selector in CAPTCHA_SUBMIT_SELECTORS:
            try:
                button = ctx.locator(selector).first
                await button.wait_for(state="visible", timeout=BUTTON_TIMEOUT_MS)
                await button.click(timeout=BUTTON_TIMEOUT_MS)
                logger.info(f"auth: captcha submitted via {selector}")
                return True
            except PlaywrightTimeout:
                continue
            except Exception as exc:
                logger.debug(f"auth: captcha submit via {selector} failed: {exc}")

        # The control may be a div/span with an onclick rather than a button, so
        # widen the roles beyond click_by_text's defaults.
        if await utils.click_by_text(
            ctx,
            CAPTCHA_SUBMIT_TEXTS,
            roles=("button", "a", 'input[type="submit"]', 'input[type="button"]',
                   "div", "span", "label"),
            config=self.config,
        ):
            return True

        # Last resort: find it by text in JS and click it directly.
        try:
            clicked = await ctx.evaluate(
                """
                (texts) => {
                  const wanted = texts.map((t) => t.toLowerCase());
                  for (const el of document.querySelectorAll('*')) {
                    if (el.children.length > 2) continue;
                    const t = (el.innerText || '').trim().toLowerCase();
                    if (!t) continue;
                    if (wanted.some((w) => t === w || t.startsWith(w))) {
                      el.click();
                      return t;
                    }
                  }
                  return null;
                }
                """,
                list(CAPTCHA_SUBMIT_TEXTS),
            )
            if clicked:
                logger.info(f"auth: captcha submitted via JS click on {clicked!r}")
                return True
        except Exception as exc:
            logger.debug(f"auth: JS submit click failed: {exc}")
        return False

    async def _clear_captcha_selection(self, ctx: Any) -> None:
        """Deselect everything before a fresh attempt.

        Select() toggles, so re-clicking a tile that is still selected from the
        previous attempt turns it OFF. Without clearing, retries flip tiles in
        and out and img-selected appears to flap at random.
        """
        try:
            selected = await ctx.evaluate(
                "(cls) => document.querySelectorAll('img.' + cls).length",
                CAPTCHA_SELECTED_CLASS,
            )
        except Exception:
            selected = 0
        if not selected:
            return

        logger.info(f"auth: clearing {selected} tile(s) left selected from the last try")
        # CONFIRMED: the Clear Selection control calls onUndo().
        if await self._call_captcha_action(ctx, CAPTCHA_CLEAR_FN):
            await asyncio.sleep(0.6)
            return
        if await utils.click_by_text(
            ctx,
            CAPTCHA_CLEAR_TEXTS,
            roles=("button", "a", "div", "span", "label"),
            config=self.config,
        ):
            await asyncio.sleep(0.6)
            return
        # No Clear control — toggle them off individually.
        try:
            await ctx.evaluate(
                """
                (cls) => {
                  document.querySelectorAll('img.' + cls).forEach((img) => {
                    const tile = img.closest('div.col-4');
                    if (tile && tile.id && typeof Select === 'function') {
                      Select(tile.id, img);
                    }
                  });
                }
                """,
                CAPTCHA_SELECTED_CLASS,
            )
            await asyncio.sleep(0.6)
        except Exception as exc:
            logger.debug(f"auth: could not clear the selection: {exc}")

    # ------------------------------------------------------------------ #
    # Session detection + persistence
    # ------------------------------------------------------------------ #
    async def _is_logged_in(self, page: Page, *, navigate: bool = True) -> bool:
        """Decide whether the current session is authenticated.

        CONFIRMED: a logged-in session lives under ``/Global/bls/`` and lands on
        ``/Global/bls/visatypeverification`` (the login form's hidden ``ReturnUrl``
        points there). There is **no** ``/account`` or ``/dashboard`` page on this
        portal — probing for one returns 404, which earlier caused a successful
        login to be reported as a failure.

        ``navigate=False`` judges the page as it stands. Use it straight after a
        login so the post-submit page (which may still hold a captcha) is not
        navigated away from before it has been inspected.
        """
        if navigate:
            try:
                await self._goto(page, self._path("dashboard"))
            except PortalUnreachableError:
                raise  # blocked at the edge — do not fall through to a login
            except Exception as exc:
                logger.debug(f"auth: session probe navigation failed: {exc}")
                return False
            await utils.human_delay(self.config, scale=0.5)

        url = (page.url or "").lower()

        # --- hard negatives -------------------------------------------------
        if url.startswith("chrome-error://") or "chromewebdata" in url:
            logger.info(f"auth: not logged in — browser error page ({url})")
            return False

        if any(hint in url for hint in ("/account/login", "signin", "/login")):
            logger.info(f"auth: not logged in — still on the login page ({url})")
            return False

        if await self._captcha_present(page):
            logger.info("auth: not logged in — a CAPTCHA is still on screen")
            return False

        # --- confirmed positive --------------------------------------------
        # Path only: the login URL carries the destination in its ReturnUrl query
        # parameter, so a whole-URL match reports success on the login page.
        if self._authenticated_url(page.url or ""):
            logger.success(f"auth: logged in — landed on {page.url}")
            return True

        # --- secondary signals ----------------------------------------------
        text = await utils.page_text(page)
        if utils.contains_any(text, LOGGED_IN_MARKERS):
            logger.info(f"auth: logged in — session marker found at {page.url}")
            return True

        if await utils.first_visible(page, 'input[type="password"]') is not None:
            logger.info(f"auth: not logged in — a password field is visible ({url})")
            return False
        if utils.contains_any(text, ("sign in", "log in", "تسجيل الدخول")):
            logger.info(f"auth: not logged in — sign-in text present ({url})")
            return False

        # Unexpected shape — capture it rather than guessing.
        logger.warning(f"auth: session check inconclusive at {page.url}")
        await utils.screenshot(page, "session-check-inconclusive")
        await utils.dump_page_html(page, "session-check-inconclusive")
        return False

    def _has_saved_session(self) -> bool:
        if not self.session_file.exists():
            return False
        try:
            data = json.loads(self.session_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"auth: unreadable session file ({exc}) — ignoring it")
            return False
        return bool(data.get("cookies") or data.get("origins"))

    async def _save_session(self) -> None:
        if self._context is None:
            return
        try:
            self.session_file.parent.mkdir(parents=True, exist_ok=True)
            await self._context.storage_state(path=str(self.session_file))
            logger.info(f"auth: session saved to {self.session_file}")
        except Exception as exc:
            logger.warning(f"auth: could not save session: {exc}")

    def _clear_saved_session(self) -> None:
        try:
            if self.session_file.exists():
                self.session_file.unlink()
                logger.info("auth: stored session discarded")
        except OSError as exc:
            logger.warning(f"auth: could not delete session file: {exc}")

    # ------------------------------------------------------------------ #
    # Page helpers
    # ------------------------------------------------------------------ #
    def path(self, key: str) -> str:
        """Public accessor for a configured portal URL (used by monitor.py)."""
        return self._path(key)

    def _path(self, key: str) -> str:
        """Absolute URL for a configured portal path.

        TODO(unconfirmed): only the login path is verified. ``dashboard`` and
        ``appointment`` in ``bls.paths`` are placeholders — confirm both after a
        successful login and update config.example.yaml accordingly.
        """
        bls = self.config.get("bls", {}) or {}
        base = (bls.get("url") or "").rstrip("/")
        rel = ((bls.get("paths") or {}).get(key) or "/").lstrip("/")
        return f"{base}/{rel}" if rel else f"{base}/"

    async def _goto(self, page: Page, url: str) -> None:
        """Navigate, and fail loudly if the portal served an error page.

        Without this check an HTTP 403 just renders as a bare error document with
        no inputs, and the failure surfaces much later as a misleading
        "could not locate the real email field among the honeypots".
        """
        logger.debug(f"auth: navigating to {url}")

        # The portal fires a client-side redirect on first load, which aborts the
        # initial navigation with "interrupted by another navigation to
        # chrome-error://chromewebdata". That is transient - retry once rather
        # than burning a whole login attempt on it.
        response = None
        for nav_attempt in (1, 2):
            try:
                response = await page.goto(url, wait_until="domcontentloaded")
                break
            except Exception as exc:
                message = str(exc)
                transient = (
                    "interrupted by another navigation" in message
                    or "chromewebdata" in message
                    or "ERR_ABORTED" in message
                )
                if not transient or nav_attempt == 2:
                    raise
                logger.warning(
                    f"auth: navigation to {url} was interrupted — retrying once"
                )
                await utils.human_delay(self.config)

        if response is not None and response.status >= 400:
            server = ""
            try:
                headers = await response.all_headers()
                server = headers.get("server", "")
            except Exception:
                pass

            shot = await utils.screenshot(page, f"http-{response.status}")
            message = f"portal returned HTTP {response.status} for {url}"
            if server:
                message += f" (server: {server})"
            if response.status == 403:
                message += (
                    " — awselb returns 403 either for non-Egyptian / datacenter "
                    "IPs, OR after repeated automated attempts from an IP that "
                    "was previously fine. If earlier requests in this same run "
                    "succeeded, it is rate limiting, not geo-blocking: stop, "
                    "wait, and reduce the attempt rate."
                )

            logger.error(f"auth: {message}")
            self._log(message, status="error")
            if response.status == 403:
                # Persist it so the next run refuses to start during cooldown.
                utils.record_block(message)
            await self._alert_error(message, retry_info="no retry — the IP must change first")
            if shot and self.notifier is not None:
                try:
                    await self.notifier.send_photo(shot, f"HTTP {response.status} from the portal")
                except Exception:
                    pass
            raise PortalUnreachableError(message)

        await self._settle(page)

    async def _settle(self, page: Page) -> None:
        """Wait for the network to go quiet, then add a human-ish pause."""
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeout:
            logger.debug("auth: networkidle timed out — continuing")
        await utils.human_delay(self.config, scale=0.5)

    async def _dismiss_cookie_banner(self, page: Page) -> None:
        if await utils.click_by_text(page, COOKIE_ACCEPT_TEXTS, config=self.config):
            logger.debug("auth: cookie banner dismissed")
            await utils.human_delay(self.config, scale=0.5)

    async def _check_for_captcha(self, page: Page) -> None:
        """Pause for a human when BLS throws a captcha at the login flow."""
        phrases = (self.config.get("detection", {}) or {}).get("captcha_phrases", [])
        hit = utils.contains_any(await utils.page_text(page), phrases)
        if not hit:
            return

        # Capture everything about the challenge before anything else touches the
        # page: screenshot, full DOM, and the widget markup in isolation.
        shot = await utils.screenshot(page, "login-captcha")
        full_dom = await utils.dump_page_html(page, "login-captcha")
        widget = await utils.dump_widget_dom(page, CAPTCHA_DOM_SELECTORS, "login-captcha")

        logger.warning(f"auth: captcha detected ({hit!r}) — manual step required")
        logger.warning(f"auth: captcha url={page.url}")
        logger.warning(
            f"auth: captcha artefacts — screenshot={shot} full_dom={full_dom} "
            f"widget_dom={widget.get('file')} matches={len(widget.get('matches', []))} "
            f"iframes={widget.get('iframes')}"
        )
        self._log(f"captcha detected: {hit}", status="warn")
        # Informational only. The bot must never block on a human here: this
        # login attempt simply fails and the monitor backs off and retries from
        # scratch, which is what an unattended bot needs.
        if self.notifier is not None:
            await self.notifier.error(
                f"Could not solve the login captcha automatically (matched {hit!r}).",
                retry_info="retrying automatically after a back-off",
            )
            if shot:
                try:
                    await self.notifier.send_photo(shot, "Unsolved login captcha")
                except Exception:
                    pass

    # ------------------------------------------------------------------ #
    # Logging / alert plumbing
    # ------------------------------------------------------------------ #
    def _log(self, message: str, *, status: str = "info") -> None:
        logger.info(f"auth: {message}")
        if self.state is not None:
            self.state.log_event("auth", message, status=status)

    async def _alert_error(self, message: str, *, retry_info: str = "") -> None:
        if self.notifier is None:
            return
        try:
            await self.notifier.error(message, retry_info=retry_info)
        except Exception as exc:
            logger.debug(f"auth: could not send error alert: {exc}")
