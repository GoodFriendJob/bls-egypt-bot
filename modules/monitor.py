"""Availability polling loop for Cairo and Alexandria."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import re
from typing import Any

from loguru import logger
from playwright.async_api import TimeoutError as PlaywrightTimeout

from modules import otp, utils
from modules.auth import LoginError, PortalUnreachableError
from modules.booking import Booking
from modules.state import (
    STATUS_CHECKING,
    STATUS_ERROR,
    STATUS_NO_SLOTS,
    STATUS_PAUSED,
    STATUS_SLOT_FOUND,
)

# --------------------------------------------------------------------------- #
# Visa-type verification gate (CONFIRMED)
#
# After login the portal lands on /Global/bls/visatypeverification, which holds
# a SECOND captcha behind a "Verify Selection" button. Unlike the login captcha,
# this one renders its numbers as ordinary text, so it is solvable straight from
# the DOM - no vision call needed.
#
# The grid markup is injected when the button is clicked, so it is absent from a
# page dump taken on arrival. Selectors below are therefore best-effort and the
# modal is dumped on every encounter so they can be tightened.
# --------------------------------------------------------------------------- #
# CONFIRMED: the challenge is a Kendo UI window wrapping an IFRAME:
#   <iframe class="k-content-frame" src="/Global/NewCaptcha/GenerateCaptcha">
# So the grid is neither in the parent document nor in a window.open() popup —
# it is a separate frame, and every query and click must target that frame.
# Named in error messages. config.yaml is gitignored, so every machine keeps
# its own copy and they drift apart; saying WHICH file is short of a setting is
# the difference between a one-line fix and a hunt.
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

CAPTCHA_FRAME_HINT = "generatecaptcha"
CAPTCHA_FRAME_TIMEOUT = 30.0

VERIFY_SELECTION_TEXTS = ("verify selection", "verify")
SUBMIT_SELECTION_TEXTS = ("submit selection",)
RELOAD_IMAGES_TEXTS = ("reload images", "reload", "refresh")
CONSENT_ACCEPT_TEXTS = (
    "i agree to provide my consent",
    "i have read and understood",
    "i agree",
    "accept",
)
# A rejected selection surfaces as a native JS alert, not as page text.
INVALID_ALERT_HINTS = ("invalid", "wrong", "not correct", "failed")
# These mean the CHALLENGE itself is dead, not that the answer was wrong.
# Retrying only produces more 403s against an expired token — the journey has
# to restart so a fresh challenge is issued.
EXPIRED_ALERT_HINTS = ("session has been expired", "session expired", "expired")
VERIFY_MAX_ATTEMPTS = 5

# One pass over the modal: the prompt number plus every clickable tile's number,
# both read as text. Mirrors the login solver's contrast trick for the prompt,
# since the same decoy-stacking pattern may be reused here.
_VERIFY_SCAN_JS = r"""
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) return false;
    let n = el;
    while (n && n.nodeType === 1) {
      const s = getComputedStyle(n);
      if (s.display === 'none') return false;
      if (s.visibility === 'hidden' || s.visibility === 'collapse') return false;
      if (parseFloat(s.opacity || '1') < 0.1) return false;
      n = n.parentElement;
    }
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    if (cx < 0 || cy < 0 || cx > window.innerWidth || cy > window.innerHeight) return false;
    const top = document.elementFromPoint(cx, cy);
    return !!top && (top === el || el.contains(top));
  };

  const parseRGB = (s) => {
    const m = /rgba?\(([^)]+)\)/.exec(s || '');
    if (!m) return null;
    const p = m[1].split(',').map((x) => parseFloat(x.trim()));
    return p.length >= 3 ? { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 } : null;
  };
  const lum = (c) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  };
  const bgOf = (el) => {
    let n = el;
    while (n && n.nodeType === 1) {
      const c = parseRGB(getComputedStyle(n).backgroundColor);
      if (c && c.a > 0.1) return c;
      n = n.parentElement;
    }
    return { r: 255, g: 255, b: 255, a: 1 };
  };
  const contrast = (el) => {
    const fg = parseRGB(getComputedStyle(el).color);
    if (!fg || fg.a < 0.1) return 0;
    const a = lum(fg), b = lum(bgOf(el));
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  };

  const all = Array.from(document.querySelectorAll('body *'));

  // Prompt: visible text containing "number NNN", highest contrast wins.
  const prompts = [];
  for (const el of all) {
    if (el.children.length > 2) continue;
    const t = (el.innerText || '').trim();
    const m = /number\s+(\d+)/i.exec(t);
    if (!m) continue;
    if (!visible(el)) continue;
    prompts.push({ num: m[1], text: t.slice(0, 120), contrast: Math.round(contrast(el) * 100) / 100 });
  }
  prompts.sort((a, b) => b.contrast - a.contrast);

  // Tiles: leaf-ish visible elements whose whole text is just a number.
  const tiles = [];
  for (const el of all) {
    const t = (el.innerText || '').trim();
    if (!/^\d{2,5}$/.test(t)) continue;
    if (el.querySelector('*') && el.children.length > 1) continue;
    if (!visible(el)) continue;
    const r = el.getBoundingClientRect();
    tiles.push({
      el: el,
      num: t,
      id: el.id || '',
      tag: el.tagName.toLowerCase(),
      cls: (el.className || '').toString().slice(0, 80),
      hasOnclick: !!el.getAttribute('onclick') || !!el.onclick,
      selected: /select|active|checked/i.test((el.className || '').toString()),
      rect: { x: r.left, y: r.top, w: r.width, h: r.height },
    });
  }
  tiles.sort((a, b) => (Math.abs(a.rect.y - b.rect.y) > 15 ? a.rect.y - b.rect.y : a.rect.x - b.rect.x));

  // Tag each tile so Python can grab the exact element by selector and click it
  // directly. Clicking by page coordinates is wrong inside an iframe: frame
  // coordinates and page coordinates are different spaces.
  document.querySelectorAll('[data-bot-tile]').forEach(
    (el) => el.removeAttribute('data-bot-tile'));
  tiles.forEach((t, i) => { t.el.setAttribute('data-bot-tile', String(i + 1)); });

  return { prompts, tiles: tiles.map((t) => ({
    num: t.num, id: t.id, tag: t.tag, cls: t.cls,
    hasOnclick: t.hasOnclick, selected: t.selected, rect: t.rect,
  })) };
}
"""

# Where in the journey a URL puts us. Ordered: the first match wins, so the more
# specific verification path is tested before the generic /bls/ one.
PAGE_STAGES = (
    ("/account/login", "login"),
    ("/newcaptcha/logincaptcha", "login-captcha"),
    ("/newcaptcha/generatecaptcha", "verify-captcha"),
    ("/bls/visatypeverification", "verification"),
    ("/bls/visatype", "appointment-form"),
    ("/blsappointment/", "appointment"),
)


def classify_page(url: str) -> str:
    """Name the stage a URL belongs to. See Monitor.detect_stage for the DOM
    side, which is needed wherever several steps share one path."""
    low = (url or "").lower()
    if low.startswith("chrome-error") or "chromewebdata" in low:
        return "browser-error"
    for fragment, stage in PAGE_STAGES:
        if fragment in low:
            return stage
    return "other"


def _frame_gone(frame: Any) -> bool:
    """True once the captcha frame has been detached (the success signal).

    A Frame has is_detached(); only a Page has is_closed(). Mixing them up
    raises AttributeError mid-flow, so the check lives here.
    """
    if frame is None:
        return True
    try:
        if hasattr(frame, "is_detached"):
            return bool(frame.is_detached())
        if hasattr(frame, "is_closed"):
            return bool(frame.is_closed())
    except Exception:
        return True
    return False


# --------------------------------------------------------------------------- #
# "Book New Appointment" form  (/Global/bls/visatype)  — CONFIRMED markup
#
# Two traps here:
#
# 1. The controls are Kendo UI DropDownLists, NOT <select> elements. The real
#    field is a hidden <input data-role="dropdownlist"> wrapped in a
#    <span class="k-widget k-dropdown">, so querying "select" finds nothing and
#    setting .value on the input does nothing either. Values must go through the
#    widget: $(el).data('kendoDropDownList').value(id) then trigger('change').
#
# 2. Every field is duplicated as decoys — AppointmentCategoryId1/2/3/5,
#    Location2/3/5, VisaType2/5, VisaSubType2/5, AppointmentFor1/2/3 — hidden by
#    the same random-class obfuscation as the login honeypots. Only one of each
#    is genuinely visible, so each must be picked by visibility, never by id.
#
# The dropdowns cascade: onLocationChangeN() refilters the Visa Type source and
# onVisaTypeChangeN() refilters Visa Sub Type, so 'change' must actually fire
# and the next field must be given time to repopulate.
# --------------------------------------------------------------------------- #
APPOINTMENT_FIELD_ORDER = (
    ("Appointment Category", "appointment_category"),
    ("Location", "location"),
    ("Visa Type", "visa_type"),
    ("Visa Sub Type", "visa_sub_type"),
)

# The real picker among the AppointmentDate1..5 decoys: initialised as a Kendo
# widget, not disabled, and not wrapped in a k-state-disabled container. The
# counters come back on every poll so a failure says WHY, not just "none".
_DATEPICKER_OPEN_JS = r"""
() => {
  const inputs = Array.from(
    document.querySelectorAll('input[data-role="datepicker"], input[id^="AppointmentDate"]'));
  const ids = [];
  let ready = 0, enabled = 0;
  for (const inp of inputs) {
    const dp = window.jQuery && jQuery(inp).data('kendoDatePicker');
    if (dp) ready++;
    if (!inp.disabled) enabled++;
    ids.push(inp.id + (inp.disabled ? ':off' : ':on') + (dp ? ':kendo' : ':raw'));
  }
  for (const inp of inputs) {
    if (inp.disabled) continue;
    const wrap = inp.closest('.k-picker-wrap');
    if (wrap && wrap.classList.contains('k-state-disabled')) continue;
    const dp = window.jQuery && jQuery(inp).data('kendoDatePicker');
    if (!dp) continue;
    dp.open();
    return { opened: inp.id, total: inputs.length, ready, enabled, ids };
  }
  return { opened: null, total: inputs.length, ready, enabled, ids };
}
"""

# Open the real Time Slot dropdown and report its rendered options.
#
# The dataSource route read Count/Code off the model and set the value
# programmatically. It kept coming back empty ("not-loaded after 20s") even
# though the control was populated on screen, because the list is only built
# when the widget is OPENED. So drive it the way a person does: open the box,
# read the rendered <li>s, and click the green one. Availability is a
# BACKGROUND COLOUR on the item, exactly like the calendar.
_SLOT_OPEN_JS = r"""
// Kendo's dd.ul is a jQuery OBJECT, not a DOM element, so dd.ul.querySelectorAll
// does not exist -- it threw "querySelectorAll is not a function" against the
// live portal and the slot step gave up instantly (runs 20261014_042703 and
// _043702). A hand-written mock that returned a raw <ul> hid this in testing.
// dd.items() is Kendo's own accessor and returns the real <li> elements;
// unwrapping dd.ul[0] is the fallback.
function kendoItems(dd) {
  try {
    if (dd && typeof dd.items === 'function') {
      const got = dd.items();
      if (got && got.length) return Array.prototype.slice.call(got);
    }
  } catch (e) { /* fall through to the ul */ }
  const ul = dd && dd.ul ? (dd.ul[0] || dd.ul) : null;
  if (!ul || typeof ul.querySelectorAll !== 'function') return [];
  return Array.prototype.slice.call(ul.querySelectorAll('li'));
}

(dateId) => {
  // The slot widget is decoyed like everything else (AppointmentSlot1..5) and
  // the real one carries the same numeric suffix as the real date picker.
  const suffix = (String(dateId || '').match(/([0-9]+)$/) || [])[1];
  const all = Array.from(document.querySelectorAll('input'))
    .filter((i) => /AppointmentSlot/i.test(i.id || ''));
  const widget = (i) => window.jQuery && jQuery(i).data('kendoDropDownList');

  let dd = null, input = null;
  const paired = suffix ? all.find((i) => i.id === 'AppointmentSlot' + suffix) : null;
  if (paired && widget(paired) && !paired.disabled) { dd = widget(paired); input = paired; }
  if (!dd) {
    for (const i of all) {
      if (i.disabled || !widget(i)) continue;
      dd = widget(i); input = i; break;
    }
  }
  if (!dd) {
    return { ok: false, reason: 'no enabled slot dropdown',
             seen: all.map((i) => i.id + (i.disabled ? ':off' : ':on') +
                                 (widget(i) ? ':kendo' : ':raw')) };
  }

  // Re-opened on EVERY poll, not once. The option list is fetched by AJAX
  // after the date is chosen, and Kendo renders its <li>s from whatever the
  // dataSource holds AT OPEN TIME -- so a dropdown opened before the response
  // lands is built empty and stays empty however long it is then watched.
  // Reopening rebuilds it from the current data.
  const rows = (dd.dataSource && dd.dataSource.data()) || [];
  dd.open();
  const rendered = kendoItems(dd).length;
  return { ok: true, id: input.id, opened: true,
           dataCount: rows.length, rendered };
}
"""

# Read the options the open dropdown is showing, and click one by index.
_SLOT_PICK_JS = r"""
// Kendo's dd.ul is a jQuery OBJECT, not a DOM element, so dd.ul.querySelectorAll
// does not exist -- it threw "querySelectorAll is not a function" against the
// live portal and the slot step gave up instantly (runs 20261014_042703 and
// _043702). A hand-written mock that returned a raw <ul> hid this in testing.
// dd.items() is Kendo's own accessor and returns the real <li> elements;
// unwrapping dd.ul[0] is the fallback.
function kendoItems(dd) {
  try {
    if (dd && typeof dd.items === 'function') {
      const got = dd.items();
      if (got && got.length) return Array.prototype.slice.call(got);
    }
  } catch (e) { /* fall through to the ul */ }
  const ul = dd && dd.ul ? (dd.ul[0] || dd.ul) : null;
  if (!ul || typeof ul.querySelectorAll !== 'function') return [];
  return Array.prototype.slice.call(ul.querySelectorAll('li'));
}

({ inputId, wantIndex }) => {
  const input = document.getElementById(inputId);
  const dd = input && window.jQuery && jQuery(input).data('kendoDropDownList');
  if (!dd) return { ok: false, reason: 'widget vanished' };

  const items = kendoItems(dd);
  if (!items.length) return { ok: false, reason: 'not-loaded' };

  const colourOf = (el) => {
    // Walk up a little: the colour may sit on the li or on an inner span.
    for (const node of [el, ...el.querySelectorAll('*')]) {
      const bg = getComputedStyle(node).backgroundColor || '';
      const m = /rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([0-9.]+))?/.exec(bg);
      if (!m) continue;
      if (m[4] !== undefined && Number(m[4]) === 0) continue;   // transparent
      const r = +m[1], g = +m[2], b = +m[3];
      if (r === 255 && g === 255 && b === 255) continue;        // plain white
      return { r, g, b };
    }
    return null;
  };

  const options = items.map((li, index) => {
    const text = (li.innerText || '').trim();
    const c = colourOf(li);
    // Judge by WHICH channel leads, not by how far it leads. The portal
    // uses Bootstrap's pale tints as well as saturated fills: bg-success is
    // rgb(212,237,218), where green tops red by exactly 25 -- a "g > r + 25"
    // rule read that as not-green and skipped a free 09:00 slot in testing.
    const lead = (c) => {
      if (!c) return '';
      const lo = Math.min(c.r, c.g, c.b);
      const spread = Math.max(c.r, c.g, c.b) - lo;
      if (spread < 10) return '';                 // grey / white: no signal
      if (c.g >= c.r && c.g >= c.b) return 'free';
      if (c.r >= c.g && c.r >= c.b) return 'full';
      return '';
    };
    const tone = lead(c);
    const free = tone === 'free';
    const full = tone === 'full';
    return { index, text, free, full,
             colour: c ? `rgb(${c.r},${c.g},${c.b})` : 'none' };
  }).filter((o) => o.text && !/^-+\s*select/i.test(o.text));

  if (wantIndex === null || wantIndex === undefined) {
    return { ok: true, options };
  }
  const target = items[wantIndex];
  if (!target) return { ok: false, reason: 'index gone', options };
  // Click the actual <li> so the portal's own change handlers run.
  target.click();
  return { ok: true, options, clicked: (target.innerText || '').trim() };
}
"""

# --------------------------------------------------------------------------- #
# Applicant details form (/Global/BLSAppointment/ManageAppointment, #visaForm)
#
# (element id WITHOUT the _<n> applicant suffix, kind, config key, required).
# The suffix is the applicant index, so the same map drives every applicant on
# a family booking. Email/Mobile/Mission/Location/VisaType are prefilled and
# readonly on this page, so they are deliberately absent.
# --------------------------------------------------------------------------- #
APPLICANT_FIELDS: tuple[tuple[str, str, str, bool], ...] = (
    # 1. Personal details
    ("FirstName",                "text",     "first_name",               True),
    ("SurName",                  "text",     "surname",                  True),
    ("SurnameAtBirth",           "text",     "surname_at_birth",         True),
    ("LastName",                 "text",     "last_name",                False),
    ("DateOfBirth",              "date",     "dob",                      True),
    ("PlaceOfBirth",             "text",     "place_of_birth",           True),
    ("CountryOfBirthId",         "dropdown", "country_of_birth",         True),
    ("NationalityId",            "dropdown", "nationality",              True),
    ("NationalityAtBirthId",     "dropdown", "nationality_at_birth",     False),
    ("GenderId",                 "dropdown", "gender",                   True),
    ("MaritalStatusId",          "dropdown", "marital_status",           True),
    # 3. Passport details
    ("NationalIdentityNumber",   "text",     "national_identity_number", False),
    ("PassportType",             "dropdown", "passport_type",            True),
    ("PassportNo",               "text",     "passport_number",          True),
    ("IssueDate",                "date",     "passport_issue_date",      True),
    ("ExpiryDate",               "date",     "passport_expiry_date",     True),
    ("IssuePlace",               "text",     "passport_issue_place",     True),
    ("IssueCountryId",           "dropdown", "passport_issue_country",   True),
    ("TravelDate",               "date",     "travel_date",              True),
    # 4. Address & contact
    ("HomeAddressLine1",         "text",     "address",                  True),
    ("HomeAddressLine2",         "text",     "address_line2",            False),
    ("HomeAddressCountryId",     "dropdown", "country",                  True),
    ("HomeAddressCity",          "text",     "city",                     True),
    ("HomeAddressPostalCode",    "text",     "postal_code",              True),
    ("HomeAddressContactNumber", "text",     "contact_number",           True),
    # 6. Employer
    ("EmployerName",             "text",     "employer_name",            True),
    ("EmployerPhone",            "text",     "employer_phone",           True),
    ("EmployerAddress",          "text",     "employer_address",         True),
    ("CurrentOccupationId",      "dropdown", "occupation",               True),
    # 7. Travel information
    ("PurposeOfJourneyId",       "dropdown", "purpose_of_journey",       True),
    ("MemberStateDestinationId", "dropdown", "destination_state",        True),
    ("MemberStateSecondDestinationId", "dropdown", "second_destination_state", False),
    ("MemberStateFirstEntryId",   "dropdown", "first_entry_state",        True),
    ("NumberOfEntriesRequested",  "dropdown", "number_of_entries",        True),
    # CONFIRMED: this one is numeric. "13 Nights 14 days" went in and came
    # back as "" because the control only accepts a number of days.
    ("IntendedStayDuration",      "text",     "intended_stay_days",       True),
    ("IntendedDateOfArrival",     "date",     "arrival_date",             True),
    ("IntendedDateOfDeparture",   "date",     "departure_date",           True),
    # 8. The inviting party -- for a tourist booking, the hotel.
    ("InvitingAuthorityName",     "text",     "hotel_name",               True),
    ("InvitingCountryId",         "dropdown", "hotel_country",            True),
    ("InvitingCity",              "text",     "hotel_city",               True),
    ("InvitingZipCode",           "text",     "hotel_zip",                True),
    ("InvitingAddress",           "text",     "hotel_address",            True),
    ("InvitingEmail",             "text",     "hotel_email",              True),
    ("InvitingContactNo",         "text",     "hotel_phone",              True),
    # 9. A contact person at that address.
    ("InvitingContactName",       "text",     "contact_first_name",       True),
    ("InvitingContactSurname",    "text",     "contact_surname",          True),
    ("InvitingContactCountryId",  "dropdown", "contact_country",          True),
    ("InvitingContactCity",       "text",     "contact_city",             True),
    ("InvitingContactZipCode",    "text",     "contact_zip",              True),
    ("InvitingContactAddress",    "text",     "contact_address",          True),
    ("InvitingContactContactNo",  "text",     "contact_phone",            True),
    ("InvitingContactEmail",      "text",     "contact_email",            True),
    # CONFIRMED ids; both are Kendo dropdowns fed by costCoveredData /
    # meansOfSupportData.
    ("CostCoveredById",          "dropdown", "cost_covered_by",          True),
    ("MeansOfSupportId",         "dropdown", "means_of_support",         True),
)

# Fill one field. Kendo owns the date pickers and dropdowns, so their widget
# API is used and 'change' fired: writing .value directly leaves the widget's
# own model stale and the form posts the old value.
_FILL_FIELD_JS = r"""
(field) => {
  const el = document.getElementById(field.id);
  if (!el) return { id: field.id, ok: false, reason: 'missing' };
  const $ = window.jQuery;

  if (field.kind === 'date') {
    const dp = $ && $(el).data('kendoDatePicker');
    if (!dp) return { id: field.id, ok: false, reason: 'no datepicker widget' };
    // yyyy-mm-dd, parsed as LOCAL time: new Date("2026-10-15") is UTC and can
    // land on the previous day once the timezone is applied.
    const p = String(field.value).split('-').map(Number);
    if (p.length !== 3) return { id: field.id, ok: false, reason: 'bad date' };
    dp.value(new Date(p[0], p[1] - 1, p[2]));
    dp.trigger('change');
    return { id: field.id, ok: true, set: el.value };
  }

  if (field.kind === 'dropdown') {
    const dd = $ && $(el).data('kendoDropDownList');
    if (!dd) return { id: field.id, ok: false, reason: 'no dropdown widget' };
    const data = dd.dataSource.data() || [];
    const textField = (dd.options && dd.options.dataTextField) || 'Name';
    const valueField = (dd.options && dd.options.dataValueField) || 'Id';
    // Punctuation is collapsed to a space as well as whitespace: the portal
    // writes "SELF EMPLOYED" where the config says "Self-employed", and the
    // hyphen alone was enough to make that an unmatched option.
    const norm = (t) => String(t || '').replace(/[-_/.,()'"‐-―]+/g, ' ')
                          .replace(/\s+/g, ' ').trim().toLowerCase();
    const want = norm(field.value);
    const texts = data.map((d) => String(d[textField] || ''));
    let i = texts.findIndex((t) => norm(t) === want);
    if (i < 0) i = texts.findIndex((t) => norm(t).includes(want));
    // And the other way round: config says nationality "Egyptian" while the
    // dropdown offers the country "Egypt", so neither exact nor
    // option-contains-want matches. Require 4+ chars so this cannot latch
    // onto something trivial.
    if (i < 0 && want.length >= 4) {
      i = texts.findIndex((t) => norm(t).length >= 4 && want.includes(norm(t)));
    }
    if (i < 0) {
      // Every option, not a dozen: the one time this fired, the value the
      // config asked for ("Self-employed") simply is not on this portal's
      // list, and the truncated dump did not show what to choose instead.
      return { id: field.id, ok: false, reason: 'no such option',
               options: texts.slice(0, 60), count: texts.length };
    }
    dd.value(data[i][valueField]);
    dd.trigger('change');
    return { id: field.id, ok: true, set: texts[i] };
  }

  // CONFIRMED from the form markup: every phone control is
  // <input type="number"> (EmployerPhone_0, InvitingContactNo_0,
  // InvitingContactContactNo_0, HomeAddressContactNumber_0). A number input
  // REFUSES anything that is not a valid floating-point number, and "+" is
  // not one -- assigning "+201055565536" leaves the field EMPTY with no error
  // anywhere. So the digits are kept and the rest dropped, and what was
  // dropped is reported rather than hidden.
  let want = String(field.value);
  let cleanedFrom = '';
  if (el.type === 'number') {
    const digits = want.replace(/[^\d.]/g, '');
    if (digits !== want) { cleanedFrom = want; want = digits; }
  }
  el.value = want;
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  // Trust the control, not the assignment: this is the check that would have
  // caught a "+" being swallowed.
  if (want && !el.value) {
    return { id: field.id, ok: false, reason: `the control rejected ${want!==String(field.value) ? want : 'this value'}`,
             type: el.type };
  }
  return { id: field.id, ok: true, set: el.value, cleanedFrom };
}
"""

# After filling, find every control the page marks required that is still
# empty. The applicant form is longer than the markup captured so far -- the
# Travel Information section continues past MemberStateSecondDestinationId with
# entries/stay/previous-visa/expenses controls whose ids were never seen. This
# reports them by id and label so they can be added to APPLICANT_FIELDS without
# another round of guessing.
# Fill a control located by its visible LABEL rather than its element id.
#
# APPLICANT_FIELDS keys off ids, which only works for markup that has been
# captured. The form carries more required controls than that -- Number of
# Entries, Intended Stay, Cost Covered By, Means Of Support -- and their ids
# were never seen. Labels ARE visible on the page, so config can name them
# directly and no id has to be invented. The kind is detected from whatever
# widget the control turns out to be.
_FILL_BY_LABEL_JS = r"""
({ label, value, index }) => {
  const form = document.getElementById('visaForm') || document;
  const norm = (t) => String(t || '').replace(/[*\-_/.,()'"‐-―]+/g, ' ')
                        .replace(/\s+/g, ' ').trim().toLowerCase();
  const want = norm(label);
  const suffix = '_' + (index || 0);

  const seen = [];
  let exact = null, partial = null;
  for (const el of form.querySelectorAll('label')) {
    const id = el.getAttribute('for');
    if (!id) continue;
    // Only this applicant's controls, so a family booking cannot cross-fill.
    if (/_\d+$/.test(id) && !id.endsWith(suffix)) continue;
    const text = norm(el.innerText);
    if (!text) continue;
    seen.push(text);
    const node = document.getElementById(id);
    if (!node) continue;
    if (text === want) { exact = node; break; }
    // The LABEL may carry extra words ("contact number" for "number"), but
    // never the other way round: "Hotel Name" previously matched the plain
    // label "name" and overwrote EmployerName_0 with the hotel, silently
    // replacing a value that had already been filled correctly.
    if (!partial && text.includes(want)) partial = node;
  }
  const target = exact || partial;
  if (!target) return { ok: false, reason: 'no label match', seen: seen.slice(0, 80) };

  const $ = window.jQuery;
  const dd = $ && $(target).data('kendoDropDownList');
  const dp = $ && $(target).data('kendoDatePicker');

  if (dd) {
    const data = dd.dataSource.data() || [];
    const textField = (dd.options && dd.options.dataTextField) || 'Name';
    const valueField = (dd.options && dd.options.dataValueField) || 'Id';
    const texts = data.map((d) => String(d[textField] || ''));
    const w = norm(value);
    let i = texts.findIndex((t) => norm(t) === w);
    if (i < 0) i = texts.findIndex((t) => norm(t).includes(w));
    if (i < 0 && w.length >= 4) {
      i = texts.findIndex((t) => norm(t).length >= 4 && w.includes(norm(t)));
    }
    if (i < 0) {
      return { ok: false, id: target.id, reason: 'no such option',
               options: texts.slice(0, 15), count: texts.length };
    }
    dd.value(data[i][valueField]);
    dd.trigger('change');
    return { ok: true, id: target.id, kind: 'dropdown', set: texts[i] };
  }

  if (dp) {
    const parts = String(value).split('-').map(Number);
    if (parts.length !== 3) return { ok: false, id: target.id, reason: 'bad date' };
    dp.value(new Date(parts[0], parts[1] - 1, parts[2]));
    dp.trigger('change');
    return { ok: true, id: target.id, kind: 'date', set: target.value };
  }

  // Same number-input trap as _FILL_FIELD_JS: "+" is not a valid
  // floating-point number, so assigning it empties the field silently.
  let want = String(value);
  if (target.type === 'number') want = want.replace(/[^\d.]/g, '');
  target.value = want;
  target.dispatchEvent(new Event('input', { bubbles: true }));
  target.dispatchEvent(new Event('change', { bubbles: true }));
  if (want && !target.value) {
    return { ok: false, id: target.id, reason: 'the control rejected this value' };
  }
  return { ok: true, id: target.id, kind: 'text', set: target.value };
}
"""

# Section 11, "Details of the inviting person(s) ... If not applicable,
# details of hotel(s) or temporary accommodation(s)". CONFIRMED markup: a radio
# group whose own change handler decides which halves of the section apply.
#
#   value 0  Inviting company/organization   -> shows BOTH halves
#   value 1  Hotel(s) / temporary accommodation -> hotel half only
#   value 2  Inviting person(s)              -> contact-person half only
#
# It defaults to 0, so a hotel booking was declaring Hostal Nova as an
# "inviting company/organization" AND being asked for a named contact person
# there — eight fields that only exist because of a default nobody chose.
INVITING_PARTY_VALUES = {"company": "0", "hotel": "1", "person": "2"}

_PICK_INVITING_PARTY_JS = r"""
({ index, value }) => {
  const name = 'BlsInvitingAuthority_' + (index || 0);
  const radios = Array.from(
    document.querySelectorAll(`input[type=radio][name="${name}"]`));
  if (!radios.length) return { ok: false, reason: 'no such radio group' };
  const want = radios.find((r) => r.value === String(value));
  if (!want) {
    return { ok: false, reason: 'no such option',
             options: radios.map((r) => r.value) };
  }
  const already = want.checked;
  // A real click so the page's own change handler runs and shows/hides the
  // halves; setting .checked would leave both halves as they were.
  want.click();
  return { ok: true, value: want.value, already,
           label: (want.parentElement && want.parentElement.innerText || '')
                    .replace(/\s+/g, ' ').trim().slice(0, 70) };
}
"""

# Is this control actually in play? Same walk as the required scanner: a field
# inside a collapsed section is neither required nor worth filling.
_CONTROL_VISIBLE_JS = r"""
(ids) => {
  const out = {};
  for (const id of ids) {
    const el = document.getElementById(id);
    if (!el) { out[id] = false; continue; }
    // Kendo hides the original <input> by design, so start at its wrapper.
    const kendo = el.getAttribute('data-role') || el.closest('span.k-widget');
    const start = el.closest('span.k-widget') || (kendo ? el.parentElement : el);
    let shown = true;
    for (let n = start; n && n.nodeType === 1; n = n.parentElement) {
      const st = getComputedStyle(n);
      if (st.display === 'none' || st.visibility === 'hidden') { shown = false; break; }
    }
    out[id] = shown;
  }
  return out;
}
"""

_UNFILLED_REQUIRED_JS = r"""
() => {
  const form = document.getElementById('visaForm');
  if (!form) return { form: false, fields: [] };
  const $ = window.jQuery;
  const out = [];

  for (const label of form.querySelectorAll('label')) {
    if (!label.querySelector('.required')) continue;          // not required
    const id = label.getAttribute('for');
    if (!id) continue;
    const el = document.getElementById(id);
    if (!el) continue;

    // Anything inside a collapsed section is not in play. The page gates
    // several blocks behind a checkbox the bot never ticks -- minor details,
    // other residence, and previous_visa_div_0 behind IsVisaIssuedBefore_0 --
    // and naming them individually missed the last one, which reported five
    // phantom "required" fields for a visa the applicant has never held.
    // Start from the Kendo wrapper, never the input: Kendo hides the original
    // <input> by design, so walking from it marks EVERY dropdown as hidden and
    // the scan stops reporting the very fields it exists to find.
    let hidden = false;
    const kendo = el.getAttribute('data-role') || el.closest('span.k-widget');
    const start = el.closest('span.k-widget') || (kendo ? el.parentElement : el);
    for (let n = start; n && n.nodeType === 1; n = n.parentElement) {
      const st = getComputedStyle(n);
      if (st.display === 'none' || st.visibility === 'hidden') { hidden = true; break; }
    }
    if (hidden) continue;

    let value = (el.value || '').trim();
    let kind = 'text';
    if ($) {
      const dd = $(el).data('kendoDropDownList');
      const dp = $(el).data('kendoDatePicker');
      if (dd) {
        kind = 'dropdown';
        const text = (dd.text && dd.text()) || '';
        value = /^-+\s*select/i.test(text) ? '' : text;
      } else if (dp) {
        kind = 'date';
        value = dp.value() ? value : '';
      }
    }
    if (value) continue;

    out.push({
      id,
      kind,
      label: (label.innerText || '').replace(/\*/g, '').trim().slice(0, 40),
    });
  }
  return { form: true, fields: out };
}
"""

# "You have already initiated an appointment which is not completed. You can
# create another appointment only after 27 minute(s)" -- the portal holds a
# half-finished booking open and refuses a new one until it lapses. Retrying
# before then cannot succeed, and each attempt costs a login and two captchas.
# What page is this, judged from the DOM rather than the URL alone.
#
# The URL is not enough any more: /blsAppointment/ManageAppointment serves the
# date/slot step, the applicant form and the consent page under the same path,
# and a refresh or a server redirect can land on any of them. Reading the
# markers tells the bot where it actually is, so it can resume at the right
# step instead of running the one it expected.
_PAGE_STAGE_JS = r"""
() => {
  const vis = (el) => {
    if (!el) return false;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 2 && r.height > 2;
  };
  const anyVis = (sel) =>
    Array.prototype.some.call(document.querySelectorAll(sel), vis);
  const text = ((document.body && document.body.innerText) || '')
    .replace(/\s+/g, ' ').trim();

  // Hard stops first.
  if (/already initiated an appointment/i.test(text)) {
    return { stage: 'appointment-cooldown' };
  }
  if (anyVis('[onclick*="onAgree"]')) return { stage: 'terms' };
  if (document.getElementById('visaForm')) return { stage: 'applicant-form' };

  // Payment is a human's job; naming it keeps the bot from wandering in.
  // Deliberately narrow: a word like "payment" on its own appears in footers
  // and help links all over this portal, so only phrasing that belongs to an
  // actual card form counts.
  if (/card number|\bcvv\b|amount payable|pay now|proceed to pay|payment gateway|card holder/i
        .test(text)) {
    return { stage: 'payment' };
  }

  // The captcha grid, wherever it is rendered.
  if (anyVis('div.col-4 img.captcha-img') ||
      /please select all boxes with number/i.test(text)) {
    return { stage: 'captcha' };
  }

  if (document.querySelector('input[id^="AppointmentDate"]') ||
      anyVis('#btnVerifyAppointment, #btnVerifiedAppointment')) {
    return { stage: 'appointment-details' };
  }
  if (document.querySelector('input[id^="VisaSubType"]')) {
    return { stage: 'appointment-form' };
  }
  if (anyVis('#btnSubmit') && /verify selection/i.test(text)) {
    return { stage: 'verification' };
  }
  return { stage: '' };
}
"""

_APPOINTMENT_COOLDOWN_JS = r"""
() => {
  const vis = (el) => {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 2 && r.height > 2;
  };
  const pool = document.querySelectorAll(
    '.alert, .alert-danger, .validation-summary, [role="alert"]');
  for (const el of pool) {
    if (!vis(el)) continue;
    const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (!/already initiated an appointment/i.test(text)) continue;
    const m = /after\s+(\d+)\s*minute/i.exec(text);
    return { found: true, minutes: m ? Number(m[1]) : null, text: text.slice(0, 200) };
  }
  return { found: false };
}
"""

_APPLICANT_READY_JS = r"""
() => {
  const form = document.getElementById('visaForm');
  if (!form) return { form: false };
  const $ = window.jQuery;
  const first = document.getElementById('FirstName_0');
  const gender = document.getElementById('GenderId_0');
  const dob = document.getElementById('DateOfBirth_0');
  return {
    form: true,
    text: !!first,
    dropdown: !!(gender && $ && $(gender).data('kendoDropDownList')),
    date: !!(dob && $ && $(dob).data('kendoDatePicker')),
  };
}
"""

_APPLICANT_SUBMIT_JS = r"""
() => {
  const form = document.getElementById('visaForm');
  if (!form) return { ok: false, reason: 'no form' };
  const vis = (el) => {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 2 && r.height > 2;
  };
  // Never the escape hatches, whatever the markup order happens to be.
  const bad = /disagree|cancel|back|reset|clear|logout/i;
  const pool = Array.from(form.querySelectorAll('button, input[type=submit]'))
    .concat(Array.from(document.querySelectorAll('button, input[type=submit]')));
  const seen = [];
  for (const el of pool) {
    if (!vis(el)) continue;
    const text = ((el.innerText || el.value || '') + '').trim();
    if (seen.length < 12) seen.push(text.slice(0, 30));
    if (bad.test(text)) continue;
    const isSubmit = el.type === 'submit' || /submit|continue|next|save/i.test(text);
    if (!isSubmit) continue;
    el.click();
    return { ok: true, text };
  }
  return { ok: false, reason: 'no submit control', seen };
}
"""

_KENDO_SET_JS = r"""
(args) => {
  const { label, wanted } = args;
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const vis = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 5 || r.height < 5) return false;
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

  const target = norm(label);
  const inputs = Array.from(document.querySelectorAll('input[data-role="dropdownlist"]'));
  const visible = [];
  for (const inp of inputs) {
    const lab = inp.id ? document.querySelector('label[for="' + inp.id + '"]') : null;
    if (!lab) continue;
    if (!norm(lab.innerText).startsWith(target)) continue;
    // The <input> itself is display:none by design, so judge the Kendo wrapper.
    // The decoys are hidden on their enclosing div.mb-3 (that is where the
    // random-class CSS applies display:none), so check that container too.
    const widget = inp.closest('span.k-widget') || inp.parentElement;
    if (!vis(widget)) continue;
    const box = inp.closest('div.mb-3');
    if (box && !vis(box)) continue;
    visible.push(inp);
  }
  if (!visible.length) {
    return { ok: false, reason: 'no visible widget for this label',
             candidates: inputs.length };
  }

  const inp = visible[0];
  if (!window.jQuery) return { ok: false, reason: 'jQuery missing', id: inp.id };
  const dd = window.jQuery('#' + inp.id).data('kendoDropDownList');
  if (!dd) return { ok: false, reason: 'kendo widget not initialised', id: inp.id };

  const data = dd.dataSource.data() || [];
  const textField = (dd.options && dd.options.dataTextField) || 'Name';
  const valueField = (dd.options && dd.options.dataValueField) || 'Id';
  const texts = [];
  for (let i = 0; i < data.length; i++) texts.push(String(data[i][textField] || ''));

  const isPlaceholder = (t) => !t || /^-+\s*select/i.test(t);
  let idx = -1;
  if (wanted) {
    idx = texts.findIndex((t) => norm(t) === norm(wanted));
    if (idx < 0) idx = texts.findIndex((t) => norm(t).includes(norm(wanted)));
  }
  if (idx < 0) idx = texts.findIndex((t) => !isPlaceholder(t));
  if (idx < 0) {
    return { ok: false, reason: 'no selectable option', id: inp.id,
             options: texts, count: texts.length };
  }

  dd.value(data[idx][valueField]);
  // value() alone does not fire change, and the cascade depends on it.
  dd.trigger('change');
  return { ok: true, id: inp.id, chosen: texts[idx], options: texts,
           count: texts.length, visibleWidgets: visible.length };
}
"""

# Set a Kendo dropdown the way a person does: OPEN it, then CLICK the option.
#
# CONFIRMED from the page's own script (dump 20261014-135231_visatype-form):
#
#   function OnSubmitVisaType() { $("#ResponseData").val(JSON.stringify(rspData)); return true; }
#   function onDrpOpen(e)   { ... rspData.push({Id, Start: new Date($.now()), ...}) }
#   function onDrpSelect(e) { ... records the selection against that Id ... }
#   function onAjaxSuccess(res) { if (res.bot === true) location.href = "/Global/account/bot"; ... }
#
# rspData is INTERACTION TELEMETRY -- which dropdown was opened, when, and what
# was then chosen -- and it is posted in ResponseData for the server to judge.
# Assigning through dd.value() + trigger('change') fires neither handler, so
# the bot posted an empty array every time. The submit itself never blocks
# (OnSubmitVisaType always returns true), which is why the failure looked like
# "nothing happened" rather than a validation error.
#
# open() and the item click go through Kendo's own event path, so both
# handlers run and the array is populated as the server expects.
# Two halves of one human interaction, so a real pause can sit between them.
#
# The portal records WHEN each dropdown was opened and when it was chosen
# (onDrpOpen / onDrpSelect -> rspData -> ResponseData) and judges the result:
# onAjaxSuccess redirects to /Global/account/bot when it decides the answer
# came from a machine. Run 20261014_162958 got all four fields in "via click"
# and the POST through, and was still sent to that page -- because opening and
# clicking inside a single evaluate puts Start and End in the same
# millisecond, which no hand can do.
#
# So: tag the control, let PLAYWRIGHT click it (a real input event, not a
# synthetic node.click()), wait a human beat, then tag and click the option.
_KENDO_PREPARE_JS = r"""
(args) => {
  const { label, wanted } = args;
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const vis = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 5 || r.height < 5) return false;
    let n = el;
    while (n && n.nodeType === 1) {
      const st = getComputedStyle(n);
      if (st.display === 'none') return false;
      if (st.visibility === 'hidden' || st.visibility === 'collapse') return false;
      if (parseFloat(st.opacity || '1') < 0.1) return false;
      n = n.parentElement;
    }
    return true;
  };

  document.querySelectorAll('[data-bot-dd]').forEach(
    (el) => el.removeAttribute('data-bot-dd'));

  const target = norm(label);
  let input = null;
  for (const inp of document.querySelectorAll('input[data-role="dropdownlist"]')) {
    const lab = inp.id ? document.querySelector('label[for="' + inp.id + '"]') : null;
    if (!lab) continue;
    if (!norm(lab.innerText).startsWith(target)) continue;
    const widget = inp.closest('span.k-widget') || inp.parentElement;
    if (!vis(widget)) continue;
    const box = inp.closest('div.mb-3');
    if (box && !vis(box)) continue;
    input = inp;
    break;
  }
  if (!input) return { ok: false, reason: 'no visible widget for this label' };

  const dd = window.jQuery && jQuery('#' + input.id).data('kendoDropDownList');
  if (!dd) return { ok: false, reason: 'kendo widget not initialised', id: input.id };

  const data = dd.dataSource.data() || [];
  const textField = (dd.options && dd.options.dataTextField) || 'Name';
  const texts = data.map((d) => String(d[textField] || ''));
  const isPlaceholder = (t) => !t || /^-+\s*select/i.test(t);

  let idx = -1;
  if (wanted) {
    idx = texts.findIndex((t) => norm(t) === norm(wanted));
    if (idx < 0) idx = texts.findIndex((t) => norm(t).includes(norm(wanted)));
  }
  if (idx < 0) idx = texts.findIndex((t) => !isPlaceholder(t));
  if (idx < 0) {
    return { ok: false, reason: 'no selectable option', id: input.id,
             options: texts, count: texts.length };
  }

  // The clickable face of the widget, which is what a person presses.
  const widget = input.closest('span.k-widget') || input.parentElement;
  const face = widget.querySelector('.k-dropdown-wrap') || widget;
  face.setAttribute('data-bot-dd', '1');

  return { ok: true, id: input.id, wantedText: texts[idx], index: idx,
           options: texts, count: texts.length };
}
"""

# Tag the rendered option so Playwright can click it for real.
_KENDO_TAG_OPTION_JS = r"""
({ inputId, wantedText, index }) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  document.querySelectorAll('[data-bot-opt]').forEach(
    (el) => el.removeAttribute('data-bot-opt'));

  const input = document.getElementById(inputId);
  const dd = input && window.jQuery && jQuery('#' + inputId).data('kendoDropDownList');
  if (!dd) return { ok: false, reason: 'widget vanished' };

  let items = [];
  try {
    if (typeof dd.items === 'function') {
      items = Array.prototype.slice.call(dd.items() || []);
    }
  } catch (e) { /* fall through */ }
  if (!items.length) {
    const ul = dd.ul ? (dd.ul[0] || dd.ul) : null;
    if (ul && typeof ul.querySelectorAll === 'function') {
      items = Array.prototype.slice.call(ul.querySelectorAll('li'));
    }
  }
  if (!items.length) return { ok: false, reason: 'list not rendered' };

  // The rendered list carries the --Select-- placeholder that dataSource does
  // not, so match on text first and only fall back to position.
  let node = items.find((li) => norm(li.textContent) === norm(wantedText));
  if (!node && items.length === index + 1 + 1) node = items[index + 1];
  if (!node && items[index]) node = items[index];
  if (!node) return { ok: false, reason: 'option not in the list',
                      rendered: items.length };

  node.setAttribute('data-bot-opt', '1');
  return { ok: true, text: (node.textContent || '').trim() };
}
"""

_KENDO_CLICK_SET_JS = r"""
(args) => {
  const { label, wanted } = args;
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const vis = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 5 || r.height < 5) return false;
    let n = el;
    while (n && n.nodeType === 1) {
      const st = getComputedStyle(n);
      if (st.display === 'none') return false;
      if (st.visibility === 'hidden' || st.visibility === 'collapse') return false;
      if (parseFloat(st.opacity || '1') < 0.1) return false;
      n = n.parentElement;
    }
    return true;
  };

  // The real field for this label, exactly as before: every field hides its
  // own decoys independently, so each one is found on its own merits.
  const target = norm(label);
  let input = null;
  for (const inp of document.querySelectorAll('input[data-role="dropdownlist"]')) {
    const lab = inp.id ? document.querySelector('label[for="' + inp.id + '"]') : null;
    if (!lab) continue;
    if (!norm(lab.innerText).startsWith(target)) continue;
    const widget = inp.closest('span.k-widget') || inp.parentElement;
    if (!vis(widget)) continue;
    const box = inp.closest('div.mb-3');
    if (box && !vis(box)) continue;
    input = inp;
    break;
  }
  if (!input) return { ok: false, reason: 'no visible widget for this label' };

  const dd = window.jQuery && jQuery('#' + input.id).data('kendoDropDownList');
  if (!dd) return { ok: false, reason: 'kendo widget not initialised', id: input.id };

  const data = dd.dataSource.data() || [];
  const textField = (dd.options && dd.options.dataTextField) || 'Name';
  const texts = data.map((d) => String(d[textField] || ''));
  const isPlaceholder = (t) => !t || /^-+\s*select/i.test(t);

  let idx = -1;
  if (wanted) {
    idx = texts.findIndex((t) => norm(t) === norm(wanted));
    if (idx < 0) idx = texts.findIndex((t) => norm(t).includes(norm(wanted)));
  }
  if (idx < 0) idx = texts.findIndex((t) => !isPlaceholder(t));
  if (idx < 0) {
    return { ok: false, reason: 'no selectable option', id: input.id,
             options: texts, count: texts.length };
  }

  // OPEN first: this is what records the Start timestamp for this field.
  dd.open();

  // Kendo renders the items on open; dd.ul is a jQuery object, so unwrap it.
  let items = [];
  try {
    if (typeof dd.items === 'function') {
      items = Array.prototype.slice.call(dd.items() || []);
    }
  } catch (e) { /* fall through */ }
  if (!items.length) {
    const ul = dd.ul ? (dd.ul[0] || dd.ul) : null;
    if (ul && typeof ul.querySelectorAll === 'function') {
      items = Array.prototype.slice.call(ul.querySelectorAll('li'));
    }
  }

  // The rendered list includes the optionLabel placeholder as item 0 when the
  // widget has one, while dataSource.data() does not -- line them up by text.
  let node = items.find((li) => norm(li.textContent) === norm(texts[idx]));
  if (!node && items.length === texts.length) node = items[idx];
  if (!node && items.length === texts.length + 1) node = items[idx + 1];

  if (node) {
    node.click();
    return { ok: true, id: input.id, chosen: texts[idx], via: 'click',
             options: texts, count: texts.length, items: items.length };
  }

  // Last resort: Kendo's select() still raises its select event, which is the
  // half that matters most; value() alone raises nothing.
  try {
    if (typeof dd.select === 'function') {
      dd.select(idx);
      dd.trigger('change');
      dd.close();
      return { ok: true, id: input.id, chosen: texts[idx], via: 'select',
               options: texts, count: texts.length, items: items.length };
    }
  } catch (e) { /* fall through */ }

  return { ok: false, reason: 'could not click the option', id: input.id,
           options: texts, items: items.length };
}
"""

_RADIO_SET_JS = r"""
(wanted) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const vis = (el) => {
    let n = el;
    while (n && n.nodeType === 1) {
      const s = getComputedStyle(n);
      if (s.display === 'none') return false;
      if (s.visibility === 'hidden') return false;
      n = n.parentElement;
    }
    return true;
  };
  const target = norm(wanted);
  for (const r of document.querySelectorAll('input[type="radio"]')) {
    if (!vis(r)) continue;
    const own = norm(r.value);
    const lab = r.id ? document.querySelector('label[for="' + r.id + '"]') : null;
    const text = norm((lab && lab.innerText) || '');
    if (own === target || text === target || text.startsWith(target)) {
      // Tagged, not clicked: the caller presses it through Playwright so the
      // event is trusted, like every other control on this form.
      document.querySelectorAll('[data-bot-radio]').forEach(
        (el) => el.removeAttribute('data-bot-radio'));
      r.setAttribute('data-bot-radio', '1');
      return { ok: true, name: r.name, id: r.id, value: r.value };
    }
  }
  return { ok: false };
}
"""

# Slot cells on the calendar, used as a structural signal alongside page text.
SLOT_PROBES = (
    "td.available",
    "td[class*='available']",
    "button[class*='available']",
    "a[class*='available']",
    ".slot:not(.disabled)",
    "[data-slot]:not([disabled])",
)


class Monitor:
    """Polls each configured location and hands detected slots to Booking."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        state: Any,
        auth: Any,
        notifier: Any,
    ) -> None:
        self.config = config
        self.state = state
        self.auth = auth
        self.notifier = notifier
        self.booking = Booking(config, state=state, notifier=notifier)

        self.locations = [str(loc).lower() for loc in (config.get("bls", {}) or {}).get("locations", [])]
        self.poll_interval = float(config.get("poll_interval", 90))
        # After the portal explicitly says there are no slots, polling every 90s
        # is pointless and is exactly the behaviour that got the IP throttled.
        # Slots are released in batches, so wait much longer before retrying.
        self.no_slots_wait_minutes = float(config.get("no_slots_wait_minutes", 5))
        # Each combination costs a verification captcha; cap the per-cycle fan-out.
        self.max_combinations = int(config.get("max_combinations_per_cycle", 12))
        # Seconds between the sessions of two combinations. Each combination
        # needs its own browser session (one verification per session), so this
        # is the main throttle on how hard a sweep hits the portal.
        self.session_recycle_wait = int(config.get("session_recycle_wait", 45))
        # How many combinations to try per cycle. MEASURED: each one needs its
        # own browser session (one verification per session), and therefore its
        # own login. Two logins inside ~2.5 minutes was enough for the portal
        # to answer 403 to LoginCaptchaSubmit and then to the login page itself
        # (run 20261013_184753). So the sweep is spread ACROSS cycles instead
        # of crammed into one: each cycle takes the next slice and the cursor
        # carries over, giving full coverage over time without the burst.
        self.combos_per_cycle = max(1, int(config.get("combinations_per_cycle", 1)))
        self._combo_cursor = 0
        # The portal will not take a booking that is too close to today, so a
        # day inside this window is skipped even when the calendar paints it
        # green. Confirmed by run 20261013_155949: tomorrow was offered and
        # accepted, and the time-slot list then stayed empty forever.
        self.min_days_ahead = int(config.get("min_days_ahead", 2))
        # How long to wait for the AJAX-loaded time-slot list after a date is
        # chosen. The box is re-opened every second until the options render.
        self.slot_wait_seconds = float(config.get("slot_wait_seconds", 30))
        self._date_field_id: str = ""
        # Minutes the portal asked us to wait before starting another booking.
        self._appointment_cooldown_minutes: int = 0
        self._current_combo_text: str = ""
        self._last_no_date_signature: str = ""
        retry = config.get("retry", {}) or {}
        self.max_attempts = int(retry.get("max_attempts", 3))
        self.page_error_wait = float(retry.get("page_error_wait", 30))
        self.network_error_wait = float(retry.get("network_error_wait", 60))

        self._consecutive_errors = 0
        self._last_pause_reason: str | None = None

        # Unattended recovery state.
        self.block_cooldown_minutes = float(retry.get("block_cooldown_minutes", 60))
        # Consecutive login failures before this stops being treated as
        # transient and becomes a critical, manager-facing pause.
        self.critical_login_failures = int(retry.get("critical_login_failures", 8))
        self._login_failures = 0
        self._block_alerted = False
        self._last_form_result: str | None = None
        self._option_unavailable: str | None = None
        self._last_visatype_status: int | None = None
        self._form_hooked: set[int] = set()
        # Live page-stage tracking, fed by the navigation watcher.
        self._nav_hooked: set[int] = set()
        self._page_stage: str | None = None
        self._needs_restart = False
        self._needs_reverify = False
        # Rotates through bls.locations so consecutive rounds ask about
        # different centres rather than hammering the same one.
        self._location_index = 0

        # Native JS dialogs raised by the verification captcha.
        self._dialogs: list[str] = []
        self._dialog_hooked: set[int] = set()

        # The appointment URL is still a guess; the live portal answers it with
        # ERR_HTTP_RESPONSE_CODE_FAILURE. Flip this to True (or set
        # bls.paths.appointment_confirmed) once the real route is known.
        bls_cfg = config.get("bls", {}) or {}
        self.appointment_path_confirmed = bool(
            (bls_cfg.get("paths") or {}).get("appointment_confirmed", False)
        )

    # ------------------------------------------------------------------ #
    # Loop
    # ------------------------------------------------------------------ #
    def _cycle_slice(self, combos: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The combinations this cycle will try, continuing where the last left off."""
        if not combos:
            return []
        size = min(self.combos_per_cycle, len(combos))
        start = self._combo_cursor % len(combos)
        picked = [combos[(start + i) % len(combos)] for i in range(size)]
        self._combo_cursor = (start + size) % len(combos)
        return picked

    def reload_config(self, config: dict[str, Any] | None = None) -> None:
        """Re-read every value __init__ cached, so dashboard edits take effect.

        Nested lookups (detection phrases, bls options) already read through
        self.config and go live the moment the dict is mutated. The scalars
        below were copied into attributes at construction, so without this they
        would keep their startup values for the lifetime of the process.

        Deliberately NOT reloaded: anything the browser session was built from
        (proxy, stealth, user agent). Those only take effect on a new context,
        and the sweep recycles sessions between combinations anyway.
        """
        if config is not None:
            self.config = config
        config = self.config
        bls_cfg = config.get("bls", {}) or {}

        self.locations = [str(loc).lower() for loc in bls_cfg.get("locations", [])]
        self.poll_interval = float(config.get("poll_interval", 90))
        self.no_slots_wait_minutes = float(config.get("no_slots_wait_minutes", 5))
        self.max_combinations = int(config.get("max_combinations_per_cycle", 12))
        self.session_recycle_wait = int(config.get("session_recycle_wait", 45))
        self.combos_per_cycle = max(1, int(config.get("combinations_per_cycle", 1)))
        self.min_days_ahead = int(config.get("min_days_ahead", 2))
        self.slot_wait_seconds = float(config.get("slot_wait_seconds", 30))

        retry = config.get("retry", {}) or {}
        self.max_attempts = int(retry.get("max_attempts", 3))
        self.page_error_wait = float(retry.get("page_error_wait", 30))
        self.network_error_wait = float(retry.get("network_error_wait", 60))
        self.block_cooldown_minutes = float(retry.get("block_cooldown_minutes", 60))
        self.critical_login_failures = int(retry.get("critical_login_failures", 8))

        # Collaborators hold their own copies.
        for holder in (self.booking, self.auth, self.notifier):
            if holder is not None and hasattr(holder, "config"):
                holder.config = config

        # A changed calendar rule invalidates the "already reported" memo,
        # otherwise widening min_days_ahead would stay silent.
        self._last_no_date_signature = ""

        logger.info(
            f"monitor: config reloaded — locations={self.locations} "
            f"poll={self.poll_interval}s lead={self.min_days_ahead}d "
            f"combos<={self.max_combinations}"
        )

    async def run(self) -> None:
        """Poll until a stop is requested."""
        logger.info(f"monitor: watching {self.locations} every {self.poll_interval}s")
        while not self.state.stop_requested:
            if not self.state.running:
                await self._idle()
                continue
            if self.state.paused:
                await self._await_manual_resume()
                continue

            # Honour a recorded block before touching the portal at all.
            remaining = utils.block_cooldown_remaining(self.block_cooldown_minutes)
            if remaining > 0:
                logger.warning(
                    f"monitor: portal blocked — waiting {remaining:.0f} more "
                    "minutes before the next login attempt"
                )
                for location in self.locations:
                    self.state.set_location_status(
                        location, STATUS_PAUSED, f"blocked — {remaining:.0f} min left"
                    )
                await utils.interruptible_sleep(remaining * 60, self.state)
                continue

            try:
                await self.run_once()
                self._consecutive_errors = 0
                self._login_failures = 0
            except PortalUnreachableError as exc:
                await self._handle_portal_block(exc)
                continue
            except LoginError as exc:
                await self._handle_login_failure(exc)
                continue
            except Exception as exc:
                await self._handle_cycle_error(exc)
                continue

            if self.state.stop_requested:
                break

            # Pace the next cycle by what the portal actually told us.
            if self._last_form_result == "no_slots":
                wait = self.no_slots_wait_minutes * 60
                logger.info(
                    f"monitor: no slots — next full check in "
                    f"{self.no_slots_wait_minutes:.0f} min"
                )
                for location in self.locations:
                    self.state.set_location_status(
                        location, STATUS_NO_SLOTS,
                        f"no slots — rechecking in {self.no_slots_wait_minutes:.0f} min",
                        checked=True,
                    )
            else:
                wait = self.poll_interval
            if self._appointment_cooldown_minutes:
                wait = max(wait, self._appointment_cooldown_minutes * 60)
                logger.info(
                    f"monitor: honouring the portal's "
                    f"{self._appointment_cooldown_minutes}-minute hold"
                )
                self._appointment_cooldown_minutes = 0

            self._last_form_result = None
            # Keep the artifact directories from growing without bound on a
            # 24/7 run; retention is logging.retention_days (default 2).
            utils.prune_logs(self.config)
            await utils.interruptible_sleep(wait, self.state)

        logger.info("monitor: loop ended")

    async def run_once(self) -> None:
        """One full pass: log in, clear the verification gate, check each location."""
        page = await self.auth.ensure_logged_in()
        self.state.mark_cycle()

        self.install_navigation_watch(page)

        if not await self.ensure_visa_type_verified(page):
            logger.error("monitor: visa-type verification not cleared — skipping cycle")
            self.state.log_event("bot", "visa-type verification failed", status="error")
            return

        # Verification lands on /Global/bls/visatype?data=... — the appointment
        # form. Its URL carries a one-time token, so it can only be reached by
        # clicking through; navigating to a fixed URL will never work.
        # CONFIRMED: dismissing "No Appointments Available" makes the server
        # redirect to /Global/bls/VisaTypeVerification. So after a no-slots
        # answer the portal has already put us back at the gate — re-solve it
        # and ask about the NEXT location, rather than ending the cycle and
        # waiting. Only once every location has answered "no slots" is the long
        # wait justified.
        everything = self.build_combinations()
        combos = self._cycle_slice(everything)
        logger.info(
            f"monitor: {len(everything)} combination(s) configured; trying "
            f"{len(combos)} this cycle, then continuing from there next cycle"
        )
        for position, combo in enumerate(combos, 1):
            logger.info(f"monitor:   {position}. {self.describe_combo(combo)}")
        if len(combos) > 1:
            # Say the real cost out loud: every combination after the first
            # rebuilds the session, which means a login captcha AND a
            # verification captcha.
            logger.info(
                f"monitor: that is {len(combos)} login(s) this cycle, about "
                f"{len(combos) * 2} captcha solves — the portal starts "
                "refusing requests at roughly two logins per few minutes"
            )
        checked: list[str] = []
        for index, combo in enumerate(combos):
            if self.state.stop_requested or not self.state.running:
                return
            location = combo["location_key"]
            logger.info(
                f"monitor: [{index + 1}/{len(combos)}] {self.describe_combo(combo)}"
            )

            if index > 0:
                # MEASURED: the portal allows exactly ONE visa-type verification
                # per browser session. The second SubmitCaptcha always answers
                # 403 + "Your session has been expired", regardless of how fresh
                # the grid is -- run 20261013_154625 failed at a grid age of
                # 16.8s, well inside the ~45s token life, after only two
                # GenerateCaptcha hits. Reloading the page or the tiles does not
                # help because neither replaces the session that was consumed.
                #
                # So recycle the whole browser: drop the cookies, close the
                # context, log in again. That is the only thing that restores a
                # usable gate, and it is what a human opening a fresh private
                # window would get.
                logger.info(
                    "monitor: recycling the browser session — the portal allows "
                    "one verification per session"
                )
                self.state.log_event("bot", "new session for next combination")
                await self._reset_session()
                # Space the sessions out. Each one is a fresh login plus a fresh
                # verification, i.e. TWO captchas and a burst of requests;
                # running them back to back is what the portal rate-limits.
                pause = self.session_recycle_wait
                logger.info(f"monitor: pausing {pause}s before the next session")
                await utils.interruptible_sleep(pause, self.state)
                if self.state.stop_requested or not self.state.running:
                    return
                try:
                    page = await self.auth.ensure_logged_in(force=True)
                except PortalUnreachableError as exc:
                    # The portal rate-limits logins. Ending the cycle quietly
                    # is right: the caller already waits, and raising here
                    # killed the whole process under --once with a traceback.
                    logger.error(
                        f"monitor: portal refused the re-login ({exc}) — ending "
                        "the cycle; the next one starts from this combination"
                    )
                    self.state.log_event(
                        "bot", "portal refused re-login (403)", status="error"
                    )
                    # Re-try this same combination first next time.
                    self._combo_cursor = (self._combo_cursor - 1) % max(
                        1, len(everything)
                    )
                    await self._handle_portal_block(exc)
                    return
                except Exception as exc:
                    logger.error(f"monitor: could not log in again: {exc}")
                    return
                self.install_navigation_watch(page)

                self._needs_reverify = False
                if not await self.ensure_visa_type_verified(page):
                    logger.error("monitor: could not re-clear the gate — ending cycle")
                    return

            # Act on what the page ACTUALLY is, not on what the previous step
            # intended. An expired session is answered with a redirect to the
            # login page, and that can land at any moment -- mid-sweep, after a
            # refresh, or on the way back from the booking page. Carrying on
            # regardless is how the bot ends up filling a form that is not
            # there.
            stage = classify_page(page.url or "")
            if stage in ("login", "login-captcha") or self._restart_requested():
                logger.warning(
                    f"monitor: page is at '{stage}' ({page.url}) — the session "
                    "expired, logging in again before continuing the sweep"
                )
                self.state.log_event("bot", "session expired mid-sweep", status="warn")
                page = await self._relogin_and_clear_gate()
                if page is None:
                    return

            self._last_form_result = None
            if not await self.complete_visatype_form(page, combo):
                logger.error(f"monitor: appointment form failed for '{location}'")
                self.state.log_event(
                    "bot", f"appointment form failed ({location})", status="error"
                )
                await self._survey_page(page, f"visatype-incomplete-{location}")
                return

            if self._last_form_result == "appointment_in_progress":
                logger.warning(
                    "monitor: the portal is holding an unfinished appointment — "
                    f"waiting {self._appointment_cooldown_minutes} min before "
                    "anything else"
                )
                return

            if self._last_form_result == "option_unavailable":
                logger.info("monitor: combination not offered — moving to the next")
                continue

            checked.append(self.describe_combo(combo))
            if self._last_form_result != "no_slots":
                # Slots, a validation problem or an unknown modal — stop the
                # sweep so the outcome is handled rather than overwritten.
                logger.info(
                    f"monitor: {self.describe_combo(combo)} returned "
                    f"{self._last_form_result!r} — not continuing the sweep"
                )
                return

            logger.info(f"monitor: no slots for {self.describe_combo(combo)}")
            self.state.set_location_status(
                location, STATUS_NO_SLOTS, "no slots", checked=True
            )

        logger.info(
            f"monitor: no slots for any of the {len(checked)} combination(s) "
            "— cycle complete"
        )
        self._last_form_result = "no_slots"
        return
        # NOTE: the old _check_location() sweep used to run here. It navigated
        # to the guessed bls.paths.appointment URL, which does not exist — the
        # appointment form is only reachable by clicking through with a
        # one-time token. Availability is now answered by the form submit above.

    # ------------------------------------------------------------------ #
    # Visa-type verification gate
    # ------------------------------------------------------------------ #
    async def ensure_visa_type_verified(
        self, page: Any, *, hard_refresh: bool = False
    ) -> bool:
        """Clear the post-login verification page.

        Flow: open /Global/bls/visatypeverification -> dismiss consent modals ->
        click "Verify Selection" -> solve the text-based captcha in the modal ->
        "Submit Selection" -> submit the form.

        ``hard_refresh`` reloads the page before starting. Use it for the second
        and later combinations in a sweep: the document sitting there has
        already spent its captcha, and a reload mints a new grid as part of the
        SAME request. Asking the iframe for one via onReload() instead adds a
        second GenerateCaptcha hit, and the portal only allows a few per
        session -- the 4th in two minutes answered 403 and took the whole page
        down with it (run 20261013_044241, 04:45:19).
        """
        # MUST be installed before anything can trigger an alert: with no handler
        # Playwright auto-dismisses dialogs, so an "Invalid selection" alert
        # would vanish silently and every retry would look identical.
        self._install_dialog_handler(page)

        # DO NOT re-navigate when already here. The login flow lands on this page
        # as the result of a POST; re-requesting it with a plain GET is rejected
        # and the portal bounces to /Global/Account/LogIn, destroying the session
        # that was just established.
        url = self.auth.path("dashboard")
        current = (page.url or "").lower()
        if "visatypeverification" in current and hard_refresh:
            # Safe here, unlike straight after login: the portal itself just
            # served this page as a GET redirect when the no-slots modal was
            # dismissed, so re-issuing that GET is what a human pressing F5
            # does. Gives a brand-new document AND a brand-new captcha token.
            logger.info("monitor: hard-refreshing the verification page")
            try:
                await page.reload(wait_until="domcontentloaded")
                self.auth.note_captcha_issued()
                await self._settle(page)
            except Exception as exc:
                logger.error(f"monitor: could not refresh the verification page: {exc}")
                return False
            if "/account/login" in (page.url or "").lower():
                logger.error(
                    f"monitor: the refresh bounced to login — session lost "
                    f"({page.url})"
                )
                return False
        elif "visatypeverification" in current:
            logger.info(f"monitor: already on the verification page — {page.url}")
        else:
            logger.info(f"monitor: navigating to verification at {url}")
            try:
                await page.goto(url, wait_until="domcontentloaded")
                await self._settle(page)
            except Exception as exc:
                logger.error(f"monitor: could not open the verification page: {exc}")
                return False

            if "/account/login" in (page.url or "").lower():
                logger.error(
                    "monitor: navigating to the verification page bounced to login "
                    f"— session lost ({page.url})"
                )
                return False

        await self._accept_consents(page)

        state = await self._gate_state(page)
        if state.get("submit") or state.get("verified"):
            logger.info("monitor: verification already satisfied")
            return await self._submit_verified_form(page)

        # STEP 1 — open the captcha popup. It may be a real window.open() popup
        # (it has its own title bar), in which case the grid is not in this
        # page's DOM at all and every later step must target the popup instead.
        main_page = page
        opened = await self._open_verification_popup(page)
        if opened is None:
            logger.warning("monitor: could not open the verification challenge")
            await utils.screenshot(page, "verify-no-button")
            await utils.dump_page_html(page, "verify-no-button")
            return False

        # CONFIRMED: the grid is served inside the GenerateCaptcha iframe, so
        # the solving context is that frame — not the parent page, and not a
        # popup (iframes are not pages, which is why expect_page found nothing).
        work = await self._wait_for_captcha_frame(opened)
        if work is None:
            logger.error(
                "monitor: the verification iframe never appeared — the challenge "
                "cannot be read"
            )
            await utils.screenshot(opened, "verify-no-frame")
            await utils.dump_page_html(opened, "verify-no-frame")
            await self._probe_captcha_endpoint(opened)
            return False
        await utils.human_delay(self.config)

        for attempt in range(1, VERIFY_MAX_ATTEMPTS + 1):
            logger.info(f"monitor: verification attempt {attempt}/{VERIFY_MAX_ATTEMPTS}")
            if self._restart_requested():
                logger.warning("monitor: dropped to login mid-verification — aborting")
                return False

            if self.auth.captcha_generation_refused():
                logger.error(
                    "monitor: the portal refused to issue a new captcha grid "
                    "(HTTP 403) — it rations them per session. Ending the cycle "
                    "so the wait can do its job; nothing downstream can pass."
                )
                self.state.log_event(
                    "bot", "captcha generation refused (403)", status="error"
                )
                await utils.screenshot(main_page, "verify-generate-refused")
                return False

            if _frame_gone(work):
                # The frame is torn down on success; the outcome is on the parent.
                logger.info("monitor: verification frame closed")
                state = await self._gate_state(main_page)
                if state.get("submit") or state.get("verified"):
                    logger.success("monitor: verification captcha passed")
                    self.state.log_event(
                        "bot", "visa-type verification cleared", status="ok"
                    )
                    return await self._submit_verified_form(main_page)
                logger.warning("monitor: popup closed but the gate is still shut")
                return False

            # The grid is injected on click, so capture it every attempt — these
            # artifacts are what the selectors get tightened against.
            # Screenshot from the page (a Frame cannot screenshot), but dump the
            # FRAME's html — that is where the grid markup actually lives.
            await utils.screenshot(main_page, f"verify-modal-attempt{attempt}")
            await utils.dump_page_html(work, f"verify-frame-attempt{attempt}")

            # STEP 2 — solve and submit the selection, inside the frame.
            #
            # CONFIRMED from the frame dump: this challenge is structurally
            # IDENTICAL to the login captcha — 54 div.col-4 tiles, img.captcha-img
            # holding base64 data: URIs, Select() onclick, a SelectedImages
            # hidden input. The digits are IMAGES, not DOM text, which is why the
            # text-based scan reported zero tiles. Reuse the vision solver,
            # pointed at the frame.
            self._dialogs.clear()
            # Deliberately NOT refresh_first. The grid on screen was minted by
            # this page's own load, which is recent because the caller either
            # just logged in or just hard-refreshed. Asking for another one
            # spends a GenerateCaptcha request the portal may refuse: doing so
            # returned 403 and detached the frame (run 20261013_044241).
            # auth's age guard still refuses to submit against a dead grid.
            submitted = await self.auth.solve_captcha(
                main_page, ctx=work, offset=await self._frame_offset(work)
            )

            # STEP 3 — a rejected answer arrives as a JS alert, so give the
            # dialog handler a moment to fire before judging the outcome.
            await asyncio.sleep(1.0)
            alerts = list(self._dialogs)
            if alerts:
                logger.info(f"  dialog(s)       : {alerts}")

            expired = any(
                any(h in msg.lower() for h in EXPIRED_ALERT_HINTS) for msg in alerts
            )
            if expired:
                # Not a wrong answer — the token died before the submit landed.
                # A fresh grid is two seconds away, so pull one and try again
                # rather than abandoning the whole cycle. Retrying against the
                # SAME dead token is what produced a run of 403s previously;
                # _reload_captcha mints a new one and resets the age clock.
                logger.warning(
                    f"monitor: the challenge expired before submission ({alerts})"
                )
                await utils.screenshot(main_page, f"verify-expired-attempt{attempt}")
                if attempt < VERIFY_MAX_ATTEMPTS and not _frame_gone(work):
                    self.state.log_event(
                        "bot", "verification token expired — retrying", status="warn"
                    )
                    await self.auth.refresh_captcha(work)
                    continue
                self.state.log_event(
                    "bot", "verification challenge expired — restarting", status="warn"
                )
                return False

            rejected = any(
                any(h in msg.lower() for h in INVALID_ALERT_HINTS) for msg in alerts
            )

            if submitted and not rejected:
                if not _frame_gone(work):
                    await asyncio.sleep(1.0)
                # Verified / Submit appear on the PARENT page, never the popup.
                state = await self._gate_state(main_page)
                if state.get("submit") or state.get("verified"):
                    logger.success("monitor: verification captcha passed")
                    self.state.log_event(
                        "bot", "visa-type verification cleared", status="ok"
                    )
                    return await self._submit_verified_form(main_page)
                logger.warning("monitor: submitted but the gate is still closed")
            elif rejected:
                logger.warning(f"  outcome         : REJECTED — {alerts}")

            if attempt < VERIFY_MAX_ATTEMPTS and not _frame_gone(work):
                # Through auth so the token age clock is reset with the grid.
                if not await self.auth.refresh_captcha(work):
                    await self._reload_captcha_images(work)
                await utils.human_delay(self.config)

        logger.error("monitor: verification not cleared after all attempts")
        await utils.screenshot(main_page, "verify-failed")
        await utils.dump_page_html(main_page, "verify-failed")
        # Report, do not block. Waiting for a human /resume here would freeze an
        # unattended bot indefinitely; the caller retries on the next cycle.
        await self.notifier.error(
            "Could not clear the visa-type verification page automatically.",
            retry_info="retrying automatically on the next cycle",
        )

        state = await self._gate_state(main_page)
        if state.get("submit") or state.get("verified"):
            return await self._submit_verified_form(main_page)
        return False

    async def _solve_text_captcha(self, ctx: Any, attempt: int) -> bool:
        """Solve the captcha by reading tile numbers from the DOM.

        ``ctx`` is a Page or a Frame — the verification grid lives inside the
        /Global/NewCaptcha/GenerateCaptcha iframe, so this normally runs against
        that frame.
        """
        try:
            scan = await ctx.evaluate(_VERIFY_SCAN_JS)
        except Exception as exc:
            logger.warning(f"monitor: verification scan failed: {exc}")
            return False

        prompts = scan.get("prompts") or []
        tiles = scan.get("tiles") or []
        if not prompts:
            logger.warning("monitor: no 'number NNN' prompt visible in the modal")
            return False

        target = prompts[0]["num"]
        logger.info(
            f"  verify target   : {target} (contrast={prompts[0]['contrast']}, "
            f"{len(prompts)} prompt candidate(s))"
        )
        logger.info(
            "  verify tiles    : "
            + ", ".join(f"{i}:{t['num']}" for i, t in enumerate(tiles, 1))
        )

        wanted = [i for i, t in enumerate(tiles, 1) if t["num"] == target]
        logger.info(f"  verify matches  : {len(wanted)} tile(s) showing {target} -> {wanted}")
        if not wanted:
            logger.warning(f"monitor: no tile shows {target}")
            return False

        # Click the tagged elements directly. Coordinate clicks would be wrong
        # here: inside an iframe, frame coordinates are a different space from
        # page coordinates, so page.mouse would land somewhere else entirely.
        for position in wanted:
            try:
                element = await ctx.query_selector(f'[data-bot-tile="{position}"]')
                if element is None:
                    logger.warning(f"monitor: tile {position} vanished before the click")
                    return False
                await element.click()
                logger.debug(f"  clicked verify tile at position {position}")
            except Exception as exc:
                logger.warning(f"monitor: could not click verify tile {position}: {exc}")
                return False
            await asyncio.sleep(0.5)

        if not await utils.click_by_text(ctx, SUBMIT_SELECTION_TEXTS, config=self.config):
            logger.warning("monitor: no 'Submit Selection' button found in the frame")
            return False
        logger.info("  verify submit   : clicked")
        await asyncio.sleep(1.5)
        return True

    async def _accept_consents(self, page: Any) -> None:
        """Dismiss the biometric / data-protection consent modals if shown."""
        for text in CONSENT_ACCEPT_TEXTS:
            if await utils.click_by_text(page, (text,), config=self.config):
                logger.info(f"monitor: accepted consent — {text!r}")
                await self._settle(page)

    async def _wait_for_captcha_frame(
        self, page: Any, timeout: float = CAPTCHA_FRAME_TIMEOUT
    ) -> Any | None:
        """Wait for the GenerateCaptcha iframe and for its grid to render.

        The Kendo loading mask sits over the window until the frame's content
        arrives, which is the spinner that appeared to hang: the frame existed
        but was still empty when the parent document was scanned.
        """
        waited = 0.0
        step = 0.5
        frame = None
        while waited < timeout:
            for candidate in page.frames:
                if CAPTCHA_FRAME_HINT in (candidate.url or "").lower():
                    frame = candidate
                    break
            if frame is not None:
                try:
                    ready = await frame.evaluate(
                        """
                        () => {
                          const txt = (document.body && document.body.innerText) || '';
                          const hasPrompt = /number\\s+\\d+/i.test(txt);
                          // The tiles are IMAGES, not text, so count the images.
                          // Counting numeric text here always returned 0 and made
                          // the frame look like it never finished rendering.
                          const imgs = document.querySelectorAll('img.captcha-img');
                          const cells = document.querySelectorAll('div.col-4');
                          return { hasPrompt, tiles: imgs.length || cells.length,
                                   imgs: imgs.length, cells: cells.length,
                                   chars: txt.trim().length };
                        }
                        """
                    )
                except Exception:
                    ready = None
                if ready and ready.get("hasPrompt") and ready.get("tiles", 0) >= 4:
                    logger.info(
                        f"monitor: captcha frame ready — {ready['tiles']} number "
                        f"element(s), url={frame.url}"
                    )
                    return frame
                if waited and int(waited) % 5 == 0:
                    logger.debug(f"monitor: captcha frame still loading — {ready}")
            await asyncio.sleep(step)
            waited += step

        if frame is None:
            logger.warning(
                f"monitor: no frame matching {CAPTCHA_FRAME_HINT!r} after "
                f"{timeout:.0f}s; frames seen: {[f.url for f in page.frames]}"
            )
        else:
            logger.warning(
                f"monitor: captcha frame never finished rendering ({frame.url})"
            )
        return frame

    async def complete_visatype_form(self, page: Any, combo: dict[str, Any]) -> bool:
        """Fill and submit /Global/bls/visatype.

        Order matters and is fixed: Appointment Category -> Appointment For ->
        Location -> Visa Type -> Visa Sub Type. Each dropdown's options are
        filtered from the previous choice, so setting them out of order writes
        into a list that is about to be replaced.
        """
        if "/bls/visatype" not in (page.url or "").lower():
            logger.warning(f"monitor: not on the appointment form ({page.url})")
            return False

        self._install_form_listener(page)
        self.install_navigation_watch(page)

        bls = self.config.get("bls", {}) or {}
        # Values come from the combination being swept, not straight from
        # config, because each field may be a list of options to try.
        wanted = {
            "appointment_category": combo.get("appointment_category", ""),
            "location": combo.get("location", ""),
            "visa_type": combo.get("visa_type", ""),
            "visa_sub_type": combo.get("visa_sub_type", ""),
        }

        self._option_unavailable = None
        self._current_combo_text = self.describe_combo(combo)
        # Fields configured with more than one value; only those are strict.
        swept: set[str] = combo.get("_swept") or set()
        logger.info("=" * 60)
        logger.info("APPOINTMENT FORM")

        # 1. Appointment Category
        if not await self._set_kendo(
            page,
            "Appointment Category",
            wanted["appointment_category"],
            strict="appointment_category" in swept,
        ):
            return await self._form_failed(page, "appointment-category")

        # 2. Appointment For (radio) — before the cascading dropdowns, because
        #    choosing Family reveals a "Number Of Members" field.
        appointment_for = str(combo.get("appointment_for", "Individual"))
        try:
            result = await page.evaluate(_RADIO_SET_JS, appointment_for)
        except Exception as exc:
            logger.warning(f"monitor: radio set failed: {exc}")
            result = {"ok": False}
        if result.get("ok"):
            try:
                await page.click("[data-bot-radio]", timeout=5000)
            except Exception as exc:
                logger.warning(f"  Appointment For       : click failed — {exc}")
                result = {"ok": False}
        if result.get("ok"):
            logger.info(
                f"  Appointment For       : {result.get('value')!r} "
                f"(name={result.get('name')}, real click)"
            )
        else:
            logger.warning(f"  Appointment For       : NOT SET ({appointment_for!r})")
        await asyncio.sleep(1.0)

        # Choosing Family raises a confirmation modal and reveals an extra
        # "Number Of Members" dropdown. Both must be handled or the form cannot
        # be submitted.
        await self._handle_form_modals(page)
        if appointment_for.strip().lower().startswith("family"):
            members = str(bls.get("family_members", "") or "")
            if not await self._set_kendo(page, "Number Of Members", members):
                return await self._form_failed(page, "number-of-members")
        await self._settle(page)

        # 3-5. The cascading dropdowns, in order.
        for label, key in (
            ("Location", "location"),
            ("Visa Type", "visa_type"),
            ("Visa Sub Type", "visa_sub_type"),
        ):
            if not await self._set_kendo(
                page, label, wanted[key], strict=key in swept
            ):
                return await self._form_failed(page, label.lower().replace(" ", "-"))

        if self._option_unavailable:
            logger.info(
                f"monitor: skipping this combination — {self._option_unavailable} "
                "is not offered by the portal"
            )
            self._last_form_result = "option_unavailable"
            return True

        await utils.screenshot(page, "visatype-form-filled")

        if self._restart_requested():
            logger.warning("monitor: dropped to login before submit — aborting")
            return False

        # Re-check every field right before submitting. The cascades can clear a
        # field that was set earlier — a live run showed Location, Visa Type and
        # Visa Sub Type populated while Appointment Category had fallen back to
        # --Select--, which the portal rejects with "Select Appointment Category".
        unset = await self._unfilled_fields(page)
        if unset:
            logger.warning(f"  re-filling cleared fields: {unset}")
            for label in unset:
                key = {
                    "Appointment Category": "appointment_category",
                    "Location": "location",
                    "Visa Type": "visa_type",
                    "Visa Sub Type": "visa_sub_type",
                }.get(label)
                value = wanted.get(key, "") if key else ""
                await self._set_kendo(page, label, value)
            still = await self._unfilled_fields(page)
            if still:
                logger.error(f"  fields still empty after re-fill: {still}")
                return await self._form_failed(page, "fields-empty")

        self._last_visatype_status = None
        if not await utils.click_by_text(page, ("submit",), config=self.config):
            logger.warning("monitor: no Submit on the appointment form")
            return await self._form_failed(page, "no-submit")

        logger.info("monitor: appointment form submitted")
        await self._settle(page)
        logger.info(f"monitor: URL after submit — {page.url}")

        # The POST can fail outright — an expired session or a stale token comes
        # back as 4xx, or the portal simply bounces to the login page. Neither
        # is recoverable here: the whole journey has to start again.
        status = self._last_visatype_status
        url_now = (page.url or "").lower()
        bounced = "/account/login" in url_now
        if (status is not None and status >= 400) or bounced:
            logger.error(
                f"monitor: appointment submit failed "
                f"(HTTP {status}, url={page.url}) — session looks dead"
            )
            await utils.screenshot(page, "visatype-submit-failed")
            await utils.dump_page_html(page, "visatype-submit-failed")
            await self._restart_journey(page)
            return False
        logger.info("=" * 60)

        await utils.screenshot(page, "after-visatype-submit")
        await utils.dump_page_html(page, "after-visatype-submit")

        # The portal keeps an unfinished booking open and refuses a new one
        # until it lapses. Retrying before then cannot work and costs a login
        # plus two captchas each time.
        held = await self._appointment_cooldown(page)
        if held:
            self._appointment_cooldown_minutes = held
            self._last_form_result = "appointment_in_progress"
            await self._hold_for_debug(page, "an appointment is already in progress")
            return True

        # CONFIRMED: when nothing is free the portal answers with a modal —
        # "No Appointments Available / Currently, no slots are available for the
        # selected category." That is the no-slots signal for this whole flow,
        # not an error, so report it and dismiss the dialog cleanly.
        # SUCCESS: the portal moves on to the appointment manager.
        if "manageappointment" in (page.url or "").lower():
            logger.success(f"monitor: appointment page reached — {page.url}")
            combo_text = self.describe_combo(combo)
            self.state.log_event(
                "bot", f"SLOTS AVAILABLE — {combo_text}", status="ok"
            )
            self._last_form_result = "appointment_page"
            # Name the combination, not just the city: with a sweep running,
            # "Cairo" alone does not say which visa type opened up.
            await self.notifier.slot_found(
                combo.get("location_key", combo.get("location", "")),
                "booking page reached",
                selection=combo_text,
            )
            await self._survey_page(page, "manage-appointment")

            # Step 1 of the booking: date -> time slot -> photo, in that order.
            if not await self.complete_appointment_details(page):
                logger.warning("monitor: could not complete the appointment details")
                self._last_form_result = "no_slots"
            return True

        # The form rejects an incomplete submission inline, e.g.
        # "Select Appointment Category" — the fields need setting again.
        problem = await self._form_validation_error(page)
        if problem:
            logger.warning(f"monitor: form validation error — {problem!r}")
            await utils.screenshot(page, "visatype-validation-error")
            self._last_form_result = "validation_error"
            return False

        # CONFIRMED: the portal answers through a shared modal with stable ids —
        #   <span id="commonModalHeader">No Appointments Available</span>
        #   <span id="commonModalBody">Currently, no slots are available ...</span>
        # Reading those beats scraping page text, and the same modal carries
        # other messages, so the header is what decides the meaning.
        modal = await self._read_common_modal(page)
        if modal:
            header = (modal.get("header") or "").strip()
            body = (modal.get("body") or "").strip()
            logger.info(f"monitor: portal modal — {header!r}")
            logger.info(f"           {body[:160]}")
            await self._dismiss_common_modal(page)

            detection = self.config.get("detection", {}) or {}
            combined = f"{header} {body}".lower()
            if utils.contains_any(combined, detection.get("unavailable_phrases", [])):
                logger.info("monitor: no slots available for this selection")
                self._last_form_result = "no_slots"
                return True

            logger.warning(f"monitor: unrecognised portal message — {header!r}")
            self._last_form_result = "modal_unknown"
            await utils.screenshot(page, "visatype-modal-unknown")
            return True

        detection = self.config.get("detection", {}) or {}
        text = await utils.page_text(page)
        hit = utils.contains_any(text, detection.get("unavailable_phrases", []))
        if hit:
            logger.info(f"monitor: no slots — portal said {hit!r}")
            await self._dismiss_common_modal(page)
            self._last_form_result = "no_slots"
            return True

        # Still on /bls/visatype with no modal means the submit did not take.
        # Reporting that as "advanced" stopped the sweep on a success that had
        # not happened (run 20261014_135132).
        if "/bls/visatype" in (page.url or "").lower():
            logger.error(
                "monitor: the appointment form did not submit — still on "
                f"{(page.url or '').split('?')[0]} with no portal message"
            )
            problem = await self._form_validation_error(page)
            if problem:
                logger.error(f"monitor: validation says {problem!r}")
            await utils.screenshot(page, "visatype-submit-stalled")
            await utils.dump_page_html(page, "visatype-submit-stalled")
            self._last_form_result = "submit_stalled"
            await self._hold_for_debug(page, "the booking form did not submit")
            return False

        self._last_form_result = "slots_possible"
        logger.success("monitor: no 'no slots' message — the form advanced")
        await self._survey_page(page, "after-visatype-submit")
        return True

    async def _set_kendo_by_hand(
        self, page: Any, label: str, wanted: str
    ) -> dict[str, Any]:
        """Open a dropdown and choose from it with real, paced clicks.

        Playwright's click dispatches a genuine input event; node.click() from
        inside the page does not, and the portal is watching. Between the two
        clicks sits a human-length pause, because ResponseData carries the
        open and select timestamps and a zero-millisecond gap is the clearest
        possible tell.
        """
        try:
            prepared = await page.evaluate(
                _KENDO_PREPARE_JS, {"label": label, "wanted": wanted}
            )
        except Exception as exc:
            return {"ok": False, "reason": f"prepare failed: {exc}"}
        if not prepared.get("ok"):
            return prepared

        # 1. Press the face of the control -> onDrpOpen records Start.
        try:
            await page.click("[data-bot-dd]", timeout=5000)
        except Exception as exc:
            logger.debug(f"monitor: could not click the {label} control: {exc}")
            return {"ok": False, "reason": f"open click failed: {exc}",
                    "id": prepared.get("id")}

        # 2. Read the options, the way a person does before choosing.
        await utils.human_delay(self.config)

        try:
            tagged = await page.evaluate(
                _KENDO_TAG_OPTION_JS,
                {
                    "inputId": prepared["id"],
                    "wantedText": prepared["wantedText"],
                    "index": prepared["index"],
                },
            )
        except Exception as exc:
            return {"ok": False, "reason": f"tag failed: {exc}",
                    "id": prepared.get("id")}
        if not tagged.get("ok"):
            return {"ok": False, "reason": tagged.get("reason"),
                    "id": prepared.get("id"),
                    "options": prepared.get("options")}

        # 3. Press the option -> onDrpSelect records End, and the gap is real.
        try:
            await page.click("[data-bot-opt]", timeout=5000)
        except Exception as exc:
            return {"ok": False, "reason": f"option click failed: {exc}",
                    "id": prepared.get("id")}

        return {
            "ok": True,
            "id": prepared["id"],
            "chosen": prepared["wantedText"],
            "via": "hand",
            "options": prepared.get("options", []),
            "count": prepared.get("count", 0),
        }

    async def _set_kendo(
        self, page: Any, label: str, wanted: str, *, strict: bool = False
    ) -> bool:
        """Set one Kendo DropDownList, chosen by visible label.

        Waits for the widget's data source to populate first: each list is
        refilled by the previous field's change handler, so it is briefly empty.

        ``strict`` marks a field that is being SWEPT across several configured
        values. There, an option the portal does not offer means this
        combination is invalid (a Schengen sub-type under National Visa) and
        should be skipped. For a single configured value it stays False, so an
        unavailable option falls back to the portal's first real choice and
        only warns — skipping would leave the cycle checking nothing at all.
        """
        deadline = 15.0
        waited = 0.0
        result: dict[str, Any] = {}
        while waited < deadline:
            try:
                # Real clicks, paced like a hand: the portal times the gap
                # between opening a dropdown and choosing from it.
                result = await self._set_kendo_by_hand(page, label, wanted)
                if not result.get("ok") and result.get("reason") in (
                    "no visible widget for this label",
                    "kendo widget not initialised",
                ):
                    result = await page.evaluate(
                        _KENDO_SET_JS, {"label": label, "wanted": wanted}
                    )
            except Exception as exc:
                logger.warning(f"monitor: {label!r} set threw: {exc}")
                result = {"ok": False, "reason": str(exc)}
            if result.get("ok"):
                opts = result.get("options") or []
                logger.info(
                    f"  {label:<22}: {result.get('chosen')!r} "
                    f"(of {len(opts)} options, widget {result.get('id')}, "
                    f"via {result.get('via', 'value')})"
                )
                mismatch = (
                    wanted
                    and result.get("chosen")
                    and wanted.lower() not in str(result["chosen"]).lower()
                )
                if mismatch:
                    logger.warning(
                        f"      {wanted!r} is not available here (took "
                        f"{result['chosen']!r}); options: {opts}"
                    )
                    if strict:
                        # Swept field: this pairing is invalid, so skip it
                        # rather than re-test a combination already covered.
                        self._option_unavailable = f"{label}={wanted!r}"
                    else:
                        logger.warning(
                            f"      {label} is a single configured value, so "
                            "continuing with the portal's choice rather than "
                            "skipping the only combination there is"
                        )
                # Give the dependent list time to be refilled by 'change'.
                await asyncio.sleep(1.2)
                return True
            await asyncio.sleep(0.5)
            waited += 0.5

        logger.error(
            f"  {label:<22}: FAILED — {result.get('reason')} "
            f"(options seen: {result.get('options')})"
        )
        return False

    @staticmethod
    def _as_list(value: Any) -> list[str]:
        """Accept either a scalar or a list in config, always return a list."""
        if value is None:
            return [""]
        if isinstance(value, (list, tuple)):
            items = [str(v).strip() for v in value if str(v).strip()]
            return items or [""]
        text = str(value).strip()
        return [text] if text else [""]

    def build_combinations(self) -> list[dict[str, Any]]:
        """Every booking option to try, in order.

        location, visa_type, visa_sub_type, appointment_category and
        appointment_for may each be a single value or a list. The cross-product
        of the lists is swept in one cycle.

        Ordered with location outermost so the two centres are covered early:
        if only one option per field is configured this is just the single
        combination, and nothing changes.
        """
        bls = self.config.get("bls", {}) or {}
        labels = bls.get("location_labels", {}) or {}

        # Which fields are genuinely being swept. An unavailable option only
        # justifies SKIPPING the combination when the field is a list: that
        # means invalid pairings are expected (a Schengen sub-type under
        # National Visa). For a single configured value, skipping would leave
        # the cycle checking nothing at all, so fall back and warn instead.
        swept = {
            name
            for name in (
                "visa_type",
                "visa_sub_type",
                "appointment_category",
                "appointment_for",
            )
            if len(self._as_list(bls.get(name))) > 1
        }
        if len(self.locations) > 1:
            swept.add("location")

        combos: list[dict[str, Any]] = []
        for location in self.locations:
            for visa_type in self._as_list(bls.get("visa_type")):
                for sub_type in self._as_list(bls.get("visa_sub_type")):
                    for category in self._as_list(bls.get("appointment_category")):
                        for who in self._as_list(bls.get("appointment_for")):
                            combos.append(
                                {
                                    "location_key": location,
                                    "location": labels.get(location, location.title()),
                                    "visa_type": visa_type,
                                    "visa_sub_type": sub_type,
                                    "appointment_category": category,
                                    "appointment_for": who or "Individual",
                                    "_swept": swept,
                                }
                            )

        if len(combos) > self.max_combinations:
            logger.warning(
                f"monitor: {len(combos)} option combinations configured — capping "
                f"at {self.max_combinations} per cycle. Each one needs its own "
                "verification captcha, and too many requests per cycle is what "
                "triggers the portal's rate limiting."
            )
            combos = combos[: self.max_combinations]
        return combos

    @staticmethod
    def describe_combo(combo: dict[str, Any]) -> str:
        return (
            f"{combo['location']} / {combo['visa_type']} / "
            f"{combo['visa_sub_type']} / {combo['appointment_category']} / "
            f"{combo['appointment_for']}"
        )

    def next_location(self) -> str:
        """The centre to ask about this round, rotating through bls.locations.

        Each centre has its own slot pool, so asking about the same one every
        time both misses openings elsewhere and repeats an identical request.
        Round 1 takes the first configured location, round 2 the next, and so on.
        """
        if not self.locations:
            return "cairo"
        location = self.locations[self._location_index % len(self.locations)]
        self._location_index += 1
        if len(self.locations) > 1:
            logger.info(
                f"monitor: this round checks {location!r} "
                f"(round {self._location_index}, rotating {self.locations})"
            )
        return location

    async def complete_appointment_details(self, page: Any) -> bool:
        """Fill the booking page's first step: date, time slot, photo.

        Order is fixed by the page itself — the Time Slot dropdown stays
        disabled until a date is chosen, and the slot list is fetched for that
        date. Availability is colour-coded: green is free, red is booked, grey
        is unselectable, so only green is ever clicked.
        """
        logger.info("=" * 60)
        logger.info("APPOINTMENT DETAILS (date / slot / photo)")
        await utils.dump_page_html(page, "manage-appointment")

        # Walk EVERY bookable date, not just the first. A green day does not
        # mean it has slots: across every run so far the 14th was painted
        # available and its time-slot list was empty, while the 15th offered
        # three. Stopping at the first date threw away the ones behind it.
        survey = await self._pick_green_date(page)
        if not survey.get("ok"):
            await utils.screenshot(page, "appointment-no-datepicker")
            return False

        offered = survey.get("offered") or []
        bookable = [
            d for d in offered if d.get("green") and not d.get("soon") and d.get("iso")
        ]
        if not bookable:
            free = [d for d in offered if d.get("green")]
            if free:
                logger.warning(
                    f"  date                  : {len(free)} free date(s) offered but "
                    f"all inside the {self.min_days_ahead}-day lead time — skipping"
                )
            else:
                logger.warning("  date                  : no free date offered at all")
            await utils.screenshot(page, "appointment-no-green-date")
            await self._report_no_date(free)
            return False

        logger.info(
            f"  bookable dates        : {', '.join(d['iso'] for d in bookable)}"
        )

        chosen_slot = False
        for index, day in enumerate(bookable, 1):
            iso = day["iso"]
            logger.info(f"  trying date           : {iso} ({index}/{len(bookable)})")
            # _pick_green_date reopens the picker each time, which is required:
            # selecting a day closes the calendar.
            result = await self._pick_green_date(page, iso)
            if not result.get("clicked"):
                logger.warning(f"  date                  : could not select {iso}")
                continue

            if await self._pick_green_slot(page, self._date_field_id):
                chosen_slot = True
                break
            logger.info(
                f"  date                  : {iso} has no free slot — trying the next"
            )

        if not chosen_slot:
            await utils.screenshot(page, "appointment-no-green-slot")
            logger.warning(
                f"monitor: none of the {len(bookable)} bookable date(s) had a free "
                "time slot"
            )
            await self._report_no_slot_on_any_date(bookable)
            return False

        await self._upload_photo(page)
        await utils.screenshot(page, "appointment-details-filled")

        # Request the emailed code and verify it.
        if not await self._verify_email(page):
            logger.warning("monitor: email not verified — cannot finalise")
            logger.info("=" * 60)
            return False

        # Finally: Verify Appointment, then Submit Appointment.
        ok = await self._finalise_appointment(page)
        logger.info("=" * 60)
        return ok

    async def _finalise_appointment(self, page: Any) -> bool:
        """Verify and submit the appointment, securing the slot.

        CONFIRMED controls, all hidden until their prerequisites are met:
            #btnVerifyAppointment    onclick="VerifyAppointment(event,this)"
            #btnVerifiedAppointment  (revealed once accepted)
            #btnSubmit               onclick="return OnAppointmentSubmit(event)"

        This books the slot. It is not a payment step — payment comes later and
        the bot stops there by design.
        """
        # Clear anything still on screen first: a modal left open swallows the
        # click on Verify/Submit and the step silently does nothing.
        await self._dismiss_open_modals(page)

        # Mobile verification may also be demanded; it needs an SMS the bot
        # cannot read, so hand over rather than stalling silently.
        try:
            mobile_needed = await page.evaluate(
                """
                () => {
                  const box = document.querySelector('.div-mobile-code');
                  if (!box) return false;
                  return getComputedStyle(box).display !== 'none';
                }
                """
            )
        except Exception:
            mobile_needed = False
        if mobile_needed:
            logger.warning("monitor: mobile verification required — handing over")
            shot = await utils.screenshot(page, "appointment-mobile-verification")
            self.state.set_manual_pause("mobile verification code")
            await self.notifier.manual_required(
                "The booking needs an SMS verification code, which the bot cannot "
                "read. Enter it in the browser, then send /resume.",
                screenshot=shot,
            )
            await self.notifier.wait_for_resume()
            self.state.clear_manual_pause()

        # CONFIRMED markup for this step:
        #   <button id="btnVerifyAppointment"   style="display:none"
        #           onclick="VerifyAppointment(event,this)">Verify Appointment
        #   <button id="btnVerifiedAppointment" disabled>Appointment Verified
        #   <button id="btnSubmit" type="submit"
        #           onclick="return OnAppointmentSubmit(event)">Submit Appointment
        #
        # The flow is: Verify Appointment -> captcha popup -> submit selection
        # -> popup closes and "Appointment Verified" appears -> Submit
        # Appointment. Only the FIRST button raises a captcha, so waiting for
        # one after the submit just stalled for the full frame timeout.

        # --- 1. Verify Appointment ------------------------------------------
        await self._dismiss_open_modals(page)
        if not await self._click_when_ready(
            page, "btnVerifyAppointment", "VerifyAppointment"
        ):
            logger.error("  verify appointment    : control never became available")
            await utils.screenshot(page, "appointment-verify-missing")
            await utils.dump_page_html(page, "appointment-verify-missing")
            return False
        logger.success("  verify appointment    : clicked")
        await asyncio.sleep(1.5)

        # --- 2. its captcha, inside the popup's iframe ----------------------
        # The popup is a Kendo window whose body is
        # /Global/CaptchaPublic/GenerateCaptcha. Solving against the PAGE found
        # no captcha, so solve_captcha returned "nothing to do" and the step
        # walked straight past the challenge; 31s later the portal said "Your
        # session has been expired" and #btnSubmit never appeared
        # (run 20261013_162707). CAPTCHA_FRAME_HINT already matches this URL.
        try:
            work = await self._wait_for_captcha_frame(page, timeout=20.0)
            if work is None:
                logger.info("  verify appointment    : no captcha popup appeared")
            else:
                offset = await self._frame_offset(work)
                logger.info(f"  verify appointment    : captcha frame at {offset}")
                if not await self.auth.solve_captcha(page, ctx=work, offset=offset):
                    logger.error("  verify appointment    : captcha not solved")
                    await utils.screenshot(page, "appointment-verify-captcha")
                    return False
        except Exception as exc:
            logger.warning(f"  verify appointment    : captcha step errored — {exc}")

        await self._dismiss_open_modals(page)

        # --- 3. wait for the portal to confirm the verification -------------
        # "Appointment Verified" appearing is the signal that the captcha was
        # accepted and the popup is gone. Clicking Submit before it shows just
        # posts an unverified form.
        verified = await self._wait_for_verified(page)
        if not verified:
            logger.error(
                "  appointment verified  : never confirmed — not submitting an "
                "unverified appointment"
            )
            await utils.screenshot(page, "appointment-not-verified")
            await utils.dump_page_html(page, "appointment-not-verified")
            return False
        logger.success("  appointment verified  : confirmed by the portal")

        # --- 4. Submit Appointment ------------------------------------------
        await self._dismiss_open_modals(page)
        if not await self._click_when_ready(page, "btnSubmit", "OnAppointmentSubmit"):
            logger.error("  submit appointment    : control never became available")
            await utils.screenshot(page, "appointment-submit-missing")
            await utils.dump_page_html(page, "appointment-submit-missing")
            return False
        logger.success("  submit appointment    : clicked")
        await self._dismiss_open_modals(page)
        await self._settle(page)
        await asyncio.sleep(2.0)

        # Submitting lands on a Terms of Service page that must be consented to
        # before the booking proceeds.
        await self._accept_terms_of_service(page)
        await self._dismiss_open_modals(page)

        # Consent can land on the applicant form, a cooldown notice, a bot
        # verdict or payment. Read the page instead of assuming.
        await self._settle(page)
        stage, ok = await self.resume_from_stage(page)
        if stage in ("bot-blocked", "appointment-cooldown"):
            return False
        # Payment means the booking got through and a human took it from there.
        if stage == "payment":
            return ok
        if not ok:
            # The step that the page was actually on did not finish. Saying
            # "appointment submitted" here is how a run with twenty blank
            # required fields still reported success (run 20261014_180450).
            logger.error(
                f"monitor: the {stage or 'unknown'} step did not complete — the "
                "appointment is NOT submitted"
            )
            await utils.screenshot(page, "appointment-not-submitted")
            self.state.log_event(
                "bot", f"appointment incomplete at stage {stage!r}", status="error"
            )
            await self._hold_for_debug(page, f"the {stage or 'unknown'} step failed")
            return False

        await utils.screenshot(page, "appointment-submitted")
        await utils.dump_page_html(page, "appointment-submitted")
        logger.success(f"monitor: appointment submitted — now at {page.url}")
        self.state.log_event("bot", "APPOINTMENT SUBMITTED", status="ok")
        await self.notifier.send(
            "✅ Appointment details submitted — the slot is being held.\n"
            f"Page: {page.url[:120]}\n\n"
            "If a payment step follows, the bot stops there, leaves the browser "
            "open and waits for your /resume."
        )
        await self._survey_page(page, "after-appointment-submit")
        return True

    @staticmethod
    def _normalise_date(value: str) -> str:
        """Accept dd/mm/yyyy as well as yyyy-mm-dd and return yyyy-mm-dd.

        config.yaml documents dob as DD/MM/YYYY and that is what is in it, but
        the Kendo pickers on this form are configured "{0:yyyy-MM-dd}". Passing
        the config value through unchanged produced "bad date" and left the
        field empty.
        """
        text = str(value or "").strip()
        if not text:
            return ""
        if "/" in text:
            parts = [p.strip() for p in text.split("/")]
            if len(parts) == 3:
                day, month, year = parts
                if len(year) == 4:
                    return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
                # yyyy/mm/dd
                if len(day) == 4:
                    return f"{day}-{month.zfill(2)}-{year.zfill(2)}"
        return text.replace(".", "-")

    async def detect_stage(self, page: Any) -> str:
        """Where the browser actually is, URL and DOM together.

        The URL alone stopped being sufficient once three different steps
        started sharing /blsAppointment/ManageAppointment. A refresh or a
        server redirect can drop the bot on any of them, so the page is read
        before deciding what to do next.
        """
        url_stage = classify_page(page.url or "")
        if "/account/bot" in (page.url or "").lower():
            return "bot-blocked"
        # These are unambiguous from the URL alone.
        if url_stage in ("login", "login-captcha", "browser-error"):
            return url_stage

        try:
            found = await page.evaluate(_PAGE_STAGE_JS)
        except Exception as exc:
            logger.debug(f"monitor: stage probe failed: {exc}")
            return url_stage

        return str(found.get("stage") or url_stage)

    async def resume_from_stage(self, page: Any) -> tuple[str, bool]:
        """Read the page and carry on from whatever step it is really on.

        Returns (stage, ok): the stage it acted on, and whether that step
        actually succeeded. The outcome used to be dropped on the floor, so a
        failed applicant form was still announced to the manager as a submitted
        appointment — the caller has to be able to tell the difference.
        """
        stage = await self.detect_stage(page)
        logger.info(f"monitor: page is at stage {stage!r} ({(page.url or '')[:80]})")

        if stage == "bot-blocked":
            logger.error(
                "monitor: the portal sent us to /account/bot — it judged the "
                "last form to be automated"
            )
            self.state.log_event("bot", "portal returned a bot verdict", status="error")
            await utils.screenshot(page, "bot-verdict")
            await self._hold_for_debug(page, "the portal returned a bot verdict")
            return stage, False

        if stage == "appointment-cooldown":
            held = await self._appointment_cooldown(page)
            self._appointment_cooldown_minutes = held
            self._last_form_result = "appointment_in_progress"
            return stage, False

        if stage == "payment":
            return stage, await self._pause_for_payment(page)

        if stage == "terms":
            return stage, await self._accept_terms_of_service(page)

        if stage == "applicant-form":
            return stage, await self.complete_applicant_details(page)

        if stage == "appointment-details":
            return stage, await self.complete_appointment_details(page)

        return stage, False

    def missing_applicant_settings(self) -> list[str]:
        """Config keys the applicant form requires that are blank right now.

        Checked at STARTUP, not when the form finally appears. On
        2026-10-14 18:04 the bot logged in, solved three captchas, took a
        09:00 slot, verified the e-mail and consented — and only then found
        twenty required settings blank, by which point the portal was holding
        a half-finished booking and refusing another for 27 minutes. All of it
        was knowable before the browser opened.
        """
        applicant = self.config.get("applicant", {}) or {}
        # Section 11 only asks for the half the inviting party calls for, so
        # the other half's fields are not missing — they are not asked.
        # Judged on the ELEMENT id, not the config key: the contact-person
        # controls are InvitingContact*, the hotel ones are the other Inviting*
        # — whereas the key prefix "contact_" also matches contact_number,
        # which is the applicant's OWN phone in section 4 and always required.
        party = str(applicant.get("inviting_party", "") or "company").strip().lower()

        def asked_for(base: str) -> bool:
            if party == "hotel":
                return not base.startswith("InvitingContact")
            if party == "person":
                return not (
                    base.startswith("Inviting")
                    and not base.startswith("InvitingContact")
                )
            return True

        return [
            key
            for base, _kind, key, required in APPLICANT_FIELDS
            if required
            and asked_for(base)
            and not str(applicant.get(key, "") or "").strip()
        ]

    async def warn_about_missing_settings(self) -> list[str]:
        """Say so, loudly and over Telegram, before a slot is spent on it."""
        missing = self.missing_applicant_settings()
        if not missing:
            logger.info(
                f"config: all {len(APPLICANT_FIELDS)} applicant field(s) the "
                "form requires have values"
            )
            return []

        logger.warning("=" * 60)
        logger.warning(
            f"CONFIG INCOMPLETE — {len(missing)} required applicant setting(s) "
            "are blank"
        )
        logger.warning(f"  file: {_CONFIG_PATH}")
        logger.warning(
            "  Monitoring still works, but a booking CANNOT be completed: the "
            "portal will hold the half-finished appointment and refuse another "
            "for ~27 minutes."
        )
        logger.warning("  Add these under 'applicant:' (or in the dashboard):")
        for key in missing:
            logger.warning(f'      {key}: ""')
        logger.warning("=" * 60)
        self.state.log_event(
            "bot",
            f"config incomplete: {len(missing)} required applicant setting(s) blank",
            status="warn",
        )
        await self.notifier.error(
            f"⚠️ {len(missing)} required applicant setting(s) are blank in "
            f"config.yaml on this machine, so a booking cannot be completed:\n"
            + "\n".join(f"- {k}" for k in missing[:25])
            + "\n\nFill them in the dashboard Settings tab.",
            retry_info="monitoring continues; booking will stop short",
        )
        return missing

    async def _pause_for_payment(self, page: Any) -> bool:
        """Stop at the payment step, keep the browser open, wait for /resume.

        Payment is the one step the bot must never complete by itself: it moves
        real money. The window is left open ON the payment page so the card
        details can be typed in by hand, and the bot blocks here with no
        timeout until /resume arrives over Telegram.

        Unlike _hold_for_debug this ignores debug.keep_browser_open — the open
        browser IS the handover, so there is nothing to opt into.
        """
        shot = await utils.screenshot(page, "payment-step")
        logger.warning("=" * 60)
        logger.warning("PAYMENT STEP — HANDING OVER")
        logger.warning(f"  url: {page.url}")
        logger.warning("  The browser window stays open on this page. Complete the")
        logger.warning("  payment by hand, then send /resume to the Telegram bot.")
        logger.warning("=" * 60)
        self.state.set_manual_pause("payment step reached")
        await self.notifier.manual_required(
            "💳 The booking has reached the PAYMENT step.\n\n"
            "The browser window is still open on the payment page. Pay there "
            "by hand — the bot will not touch it.\n\n"
            f"Page: {page.url[:120]}",
            screenshot=shot,
        )

        if not await self.notifier.wait_for_resume():
            # Either there is no Telegram link or /stop came in. Hold the page
            # regardless: closing the browser under the user's hands mid-payment
            # is the one outcome worse than waiting.
            logger.warning(
                "monitor: no /resume — holding the payment page open anyway"
            )
            await utils.interruptible_sleep(6 * 3600, self.state)
            return False

        logger.success("monitor: /resume received — payment handled by the user")
        self.state.clear_manual_pause()
        stage = await self.detect_stage(page)
        logger.info(f"monitor: after payment the page is at stage {stage!r}")
        return True

    async def _appointment_cooldown(self, page: Any) -> int:
        """Minutes the portal says to wait, or 0 when it has not said so.

        The message appears when a previous booking was started and never
        finished -- which is exactly what happens when the bot gets most of the
        way through and stops. Until it lapses, every retry is wasted effort
        and more load on an IP that is already being watched.
        """
        try:
            found = await page.evaluate(_APPOINTMENT_COOLDOWN_JS)
        except Exception as exc:
            logger.debug(f"monitor: cooldown probe failed: {exc}")
            return 0
        if not found.get("found"):
            return 0

        minutes = found.get("minutes")
        logger.warning(f"monitor: portal says — {found.get('text')!r}")
        if not minutes:
            # It said so without a number; take the configured default rather
            # than hammering.
            minutes = int(self.no_slots_wait_minutes) or 15
            logger.warning(f"monitor: no figure given — holding {minutes} min")
        self.state.log_event(
            "bot", f"appointment already in progress — wait {minutes} min", status="warn"
        )
        await self.notifier.error(
            "The portal still has an unfinished appointment open for this "
            f"account. It will not accept another for {minutes} minute(s).",
            retry_info=f"waiting {minutes} minute(s), then retrying",
        )
        return int(minutes)

    async def _hold_for_debug(self, page: Any, reason: str) -> None:
        """Leave the browser open so a human can look at the page.

        The window stays up and the bot idles instead of tearing the context
        down, which is the difference between seeing the half-filled form and
        seeing nothing at all.

        debug.keep_browser_open decides, but its DEFAULT is "yes when headful".
        A headful run is a debugging run — that is what start_headful.bat is
        for — and the one run where this mattered most closed the browser
        anyway, because that machine's config.yaml predated the key and the
        absent key read as False. Headless 24/7 runs still never hold.
        """
        debug = (self.config.get("debug", {}) or {})
        headful = not bool(self.config.get("headless", True))
        if not debug.get("keep_browser_open", headful):
            return

        minutes = float(debug.get("hold_minutes", 30))
        logger.warning("=" * 60)
        logger.warning(f"HOLDING THE BROWSER OPEN — {reason}")
        logger.warning(f"  url: {page.url if page else '?'}")
        logger.warning(
            "  The window stays up so the page can be inspected. Close this "
            f"terminal when finished; the bot idles for {minutes:.0f} min."
        )
        logger.warning("=" * 60)
        self.state.log_event("bot", f"holding browser open: {reason}", status="warn")
        try:
            await utils.interruptible_sleep(minutes * 60, self.state)
        except Exception:
            pass

    async def _applicant_form_present(self, page: Any, timeout: float = 20.0) -> bool:
        """True once #visaForm shows up after the consent step."""
        waited = 0.0
        while waited < timeout:
            try:
                if await page.evaluate("() => !!document.getElementById('visaForm')"):
                    return True
            except Exception:
                pass
            await asyncio.sleep(1.0)
            waited += 1.0
        logger.info("  applicant form        : did not appear")
        return False

    async def complete_applicant_details(self, page: Any, applicants: int = 1) -> bool:
        """Fill #visaForm -- the applicant details page -- and submit it.

        Values come from the applicant section of config.yaml, which the
        dashboard Settings tab edits live. Every field the portal marks
        required is checked BEFORE submitting: a blank one is reported by name
        rather than posting a form the portal is going to bounce.
        """
        logger.info("=" * 60)
        logger.info("APPLICANT DETAILS")
        await utils.dump_page_html(page, "applicant-form")

        if not await self._wait_for_applicant_form(page):
            logger.error("monitor: the applicant form never finished rendering")
            await utils.screenshot(page, "applicant-form-not-ready")
            return False

        applicant = self.config.get("applicant", {}) or {}
        filled = 0
        missing_required: list[str] = []
        failures: list[str] = []

        for index in range(max(1, applicants)):
            if index:
                logger.info(f"  applicant {index + 1}")

            # First, because it decides which fields exist at all.
            await self._pick_inviting_party(page, index)
            visible = await self._controls_in_play(page, index)
            skipped: list[str] = []

            for base, kind, key, required in APPLICANT_FIELDS:
                element_id = f"{base}_{index}"
                # A control inside a collapsed section is not in play: the page
                # hides the contact-person half of section 11 unless the
                # inviting party is a company or a private person, and filling
                # it would post a declaration nobody made.
                if visible and not visible.get(element_id, True):
                    skipped.append(base)
                    continue

                value = str(applicant.get(key, "") or "").strip()
                if kind == "date":
                    value = self._normalise_date(value)
                if not value:
                    if required:
                        missing_required.append(f"{key} (-> {element_id})")
                    continue

                try:
                    result = await page.evaluate(
                        _FILL_FIELD_JS,
                        {"id": element_id, "kind": kind, "value": value},
                    )
                except Exception as exc:
                    failures.append(f"{element_id}: {exc}")
                    continue

                if result.get("ok"):
                    filled += 1
                    note = ""
                    if result.get("cleanedFrom"):
                        # The control is <input type="number">; say what was
                        # dropped so a mangled phone number is visible here and
                        # not three steps later.
                        note = (
                            f"  (config said {result['cleanedFrom']!r} — this "
                            "control only takes digits)"
                        )
                    logger.info(f"  {base:<28}: {result.get('set')!r}{note}")
                else:
                    detail = str(result.get("reason"))
                    if result.get("options"):
                        detail += (
                            f" — {key} must be one of these "
                            f"{result.get('count')}: {result['options']}"
                        )
                    failures.append(f"{element_id}: {detail}")
                    logger.warning(f"  {base:<28}: NOT SET - {detail}")
                await asyncio.sleep(0.15)

            if skipped:
                logger.info(
                    f"  not in play           : {len(skipped)} field(s) the page "
                    f"has collapsed — {', '.join(skipped)}"
                )

        # Controls the id-based map cannot name, matched by their label.
        failures.extend(await self._fill_extra_fields(page))

        logger.info(f"  filled                : {filled} field(s)")

        if missing_required:
            # config.yaml is gitignored, so each machine has its own copy and
            # they drift: this machine's was missing the whole Travel/hotel/
            # contact block that the repo's template has (run 20261014_180450,
            # twenty blank fields). Naming the file and printing a paste-ready
            # block makes the fix one copy rather than one guess per field.
            logger.error(
                "monitor: cannot submit — these required settings are blank in "
                f"{_CONFIG_PATH} (this machine's own copy; it is not in git):"
            )
            for name in missing_required:
                logger.error(f"    - {name}")
            logger.error(
                "    Add them under 'applicant:' — or paste the block below and "
                "fill in the values — then restart:"
            )
            for name in missing_required:
                logger.error(f'      {name.split(" ")[0]}: ""')
            await utils.screenshot(page, "applicant-form-incomplete")
            self.state.log_event(
                "bot",
                f"applicant details incomplete: {len(missing_required)} blank",
                status="error",
            )
            bullets = "\n".join(f"- {n}" for n in missing_required[:20])
            await self.notifier.manual_required(
                "The applicant details form needs values the config does not "
                f"have:\n{bullets}\n\nFill them in the dashboard Settings "
                "tab, then send /resume."
            )
            return False

        if failures:
            logger.warning(f"monitor: {len(failures)} field(s) would not set:")
            for name in failures:
                logger.warning(f"    - {name}")

        await utils.screenshot(page, "applicant-form-filled")

        # Ask the PAGE what is still missing, rather than trusting the map to
        # be complete. Submitting with a required field empty just bounces.
        unfilled = await self._report_unfilled_required(page)
        if unfilled:
            await utils.dump_page_html(page, "applicant-form-unmapped")
            await self._hold_for_debug(
                page, f"{len(unfilled)} required field(s) still empty"
            )
            names = ", ".join(f"{f['id']} ({f['label']})" for f in unfilled[:12])
            await self.notifier.manual_required(
                f"The applicant form has {len(unfilled)} required field(s) the "
                f"bot does not know about yet:\n{names}\n\nThey need adding to "
                "the config before it can submit."
            )
            return False

        await self._dismiss_open_modals(page)

        if not await self._submit_applicant_form(page):
            return False

        await self._settle(page)
        await self._dismiss_open_modals(page)
        await utils.screenshot(page, "applicant-form-submitted")
        await utils.dump_page_html(page, "applicant-form-submitted")

        problem = await self._form_validation_error(page)
        if problem:
            logger.error(f"monitor: the portal rejected the form - {problem!r}")
            self.state.log_event(
                "bot", f"applicant form rejected: {problem}", status="error"
            )
            return False

        logger.success(f"monitor: applicant details submitted - now at {page.url}")
        self.state.log_event("bot", "applicant details submitted", status="ok")
        logger.info("=" * 60)
        await self._survey_page(page, "after-applicant-submit")

        # Payment is what follows this form. Stop there rather than letting the
        # next step run against a page asking for card details.
        if await self.detect_stage(page) == "payment":
            return await self._pause_for_payment(page)
        return True

    async def _pick_inviting_party(self, page: Any, index: int = 0) -> bool:
        """Say what the inviting party IS, instead of taking the page's default.

        Section 11 defaults to "Inviting company/organization", which for a
        tourist booking at a hotel is both untrue and expensive: that option
        also opens the contact-person half of the section, eight more required
        fields naming a person at the hotel. Choosing "Hotel(s) or temporary
        accommodation(s)" is the honest answer and closes them.

        Configured as applicant.inviting_party: hotel | company | person.
        """
        applicant = self.config.get("applicant", {}) or {}
        choice = str(applicant.get("inviting_party", "") or "hotel").strip().lower()
        value = INVITING_PARTY_VALUES.get(choice)
        if value is None:
            logger.warning(
                f"  inviting party        : {choice!r} is not one of "
                f"{sorted(INVITING_PARTY_VALUES)} — leaving the page's default"
            )
            return False

        try:
            result = await page.evaluate(
                _PICK_INVITING_PARTY_JS, {"index": index, "value": value}
            )
        except Exception as exc:
            logger.warning(f"  inviting party        : could not select — {exc}")
            return False

        if not result.get("ok"):
            logger.warning(
                f"  inviting party        : {result.get('reason')} "
                f"{result.get('options') or ''}"
            )
            return False

        logger.info(
            f"  inviting party        : {choice} — {result.get('label')!r}"
            + (" (already selected)" if result.get("already") else "")
        )
        # The handler animates with show('slow')/hide('slow'); reading
        # visibility before it settles would mark the wrong half as in play.
        await asyncio.sleep(1.2)
        return True

    async def _controls_in_play(self, page: Any, index: int = 0) -> dict[str, bool]:
        """Which mapped controls are actually visible on this form right now."""
        ids = [f"{base}_{index}" for base, _kind, _key, _req in APPLICANT_FIELDS]
        try:
            return await page.evaluate(_CONTROL_VISIBLE_JS, ids)
        except Exception as exc:
            logger.debug(f"monitor: visibility probe failed: {exc}")
            return {}

    async def _fill_extra_fields(self, page: Any, index: int = 0) -> list[str]:
        """Fill applicant.extra_fields, which are keyed by visible label.

        For controls APPLICANT_FIELDS cannot name because their ids were never
        captured. Returns the labels that could not be set.
        """
        extras = (self.config.get("applicant", {}) or {}).get("extra_fields") or {}
        if not isinstance(extras, dict) or not extras:
            return []

        failures: list[str] = []
        dumped_labels = False
        for label, value in extras.items():
            text = str(value or "").strip()
            if not text:
                continue
            try:
                result = await page.evaluate(
                    _FILL_BY_LABEL_JS,
                    {"label": str(label), "value": text, "index": index},
                )
            except Exception as exc:
                failures.append(f"{label}: {exc}")
                continue

            if result.get("ok"):
                logger.info(
                    f"  {str(label)[:26]:<28}: {result.get('set')!r} "
                    f"(#{result.get('id')}, {result.get('kind')})"
                )
            else:
                detail = str(result.get("reason"))
                if result.get("options"):
                    detail += f" (offered: {result['options']})"
                failures.append(f"{label}: {detail}")
                logger.warning(f"  {str(label)[:26]:<28}: NOT SET - {detail}")
                if result.get("seen") and not dumped_labels:
                    # Once per form, not once per entry: five misses printed
                    # forty labels each and buried the actual error.
                    dumped_labels = True
                    logger.warning(
                        "  extra_fields are matched by visible LABEL. Every "
                        f"label on this page: {result['seen']}"
                    )
                    logger.warning(
                        "  If the control already has a mapped id in "
                        "APPLICANT_FIELDS, this entry is a leftover from the "
                        "old label-matching approach — delete it from "
                        "applicant.extra_fields."
                    )
            await asyncio.sleep(0.15)
        return failures

    async def _report_unfilled_required(self, page: Any) -> list[dict[str, Any]]:
        """Name any required control the page still shows as empty.

        The field map was built from the markup captured so far, which stops
        part way through Travel Information. Anything the portal requires but
        the map does not know about shows up here with its real id, so it can
        be added rather than guessed at.
        """
        try:
            report = await page.evaluate(_UNFILLED_REQUIRED_JS)
        except Exception as exc:
            logger.debug(f"monitor: required-field scan failed: {exc}")
            return []

        fields = report.get("fields") or []
        if not fields:
            logger.info("  required check        : every required field has a value")
            return []

        logger.warning(
            f"  required check        : {len(fields)} required field(s) still empty "
            "— these are not in APPLICANT_FIELDS yet:"
        )
        for field in fields:
            logger.warning(
                f"    - {field['id']:<34} {field['kind']:<9} {field['label']!r}"
            )
        self.state.log_event(
            "bot",
            f"{len(fields)} required applicant field(s) unmapped: "
            + ", ".join(f["id"] for f in fields[:8]),
            status="warn",
        )
        return fields

    async def _wait_for_applicant_form(self, page: Any, timeout: float = 30.0) -> bool:
        """Wait for #visaForm AND its Kendo widgets, not just the markup."""
        waited = 0.0
        last: dict[str, Any] = {}
        while waited < timeout:
            try:
                last = await page.evaluate(_APPLICANT_READY_JS)
            except Exception as exc:
                logger.debug(f"monitor: applicant form probe failed: {exc}")
                last = {}
            if (
                last.get("form")
                and last.get("text")
                and last.get("dropdown")
                and last.get("date")
            ):
                if waited:
                    logger.info(f"  form ready            : after {waited:.0f}s")
                return True
            await asyncio.sleep(1.0)
            waited += 1.0
        logger.warning(
            f"  applicant form        : not ready after {timeout:.0f}s - {last}"
        )
        return False

    async def _submit_applicant_form(self, page: Any) -> bool:
        """Submit #visaForm, never touching a Disagree/Cancel/Back control."""
        try:
            result = await page.evaluate(_APPLICANT_SUBMIT_JS)
        except Exception as exc:
            logger.error(f"monitor: applicant submit failed: {exc}")
            return False

        if not result.get("ok"):
            logger.error(
                f"monitor: could not submit the applicant form "
                f"({result.get('reason')}); controls seen: {result.get('seen')}"
            )
            await utils.screenshot(page, "applicant-no-submit")
            await utils.dump_page_html(page, "applicant-no-submit")
            return False

        logger.success(f"  submitted via         : {result.get('text')!r}")
        await asyncio.sleep(2.0)
        return True

    async def _accept_terms_of_service(self, page: Any, timeout: float = 30.0) -> bool:
        """Give consent on the Terms of Service page that follows submission.

        CONFIRMED markup — the two buttons sit side by side:

          <button class="btn btn-danger p-2"
                  onclick="onDisagree()">I disagree and don't give my consent
          <button class="btn btn-success ml-4 p-2"
                  onclick="onAgree()">I agree to provide my consent

        Targeted by its onclick handler, never by position or by being "the
        other one": clicking Disagree would throw the booking away, so the
        match has to be positive. Anything whose handler or text mentions
        disagreeing is rejected outright, and if only that one is present this
        returns False rather than guessing.
        """
        waited = 0.0
        while waited < timeout:
            try:
                result = await page.evaluate(
                    """
                    () => {
                      const vis = (el) => {
                        const s = getComputedStyle(el);
                        if (s.display === 'none' || s.visibility === 'hidden') return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 2 && r.height > 2;
                      };
                      const all = Array.from(document.querySelectorAll('button, a.btn'));
                      const blob = (el) =>
                        ((el.getAttribute('onclick') || '') + ' ' +
                         (el.innerText || '')).toLowerCase();

                      const seen = [];
                      let agree = null;
                      for (const el of all) {
                        if (!vis(el)) continue;
                        const b = blob(el);
                        seen.push((el.innerText || '').trim().slice(0, 40));
                        // Reject the refusal explicitly before anything else.
                        if (b.includes('ondisagree') || b.includes('disagree') ||
                            b.includes("don't give") || b.includes('do not give')) {
                          continue;
                        }
                        if (b.includes('onagree') ||
                            (b.includes('agree') && b.includes('consent'))) {
                          agree = el;
                          break;
                        }
                      }
                      if (!agree) return { found: false, seen };
                      agree.click();
                      return { found: true, text: (agree.innerText || '').trim(),
                               handler: agree.getAttribute('onclick') || '' };
                    }
                    """
                )
            except Exception as exc:
                logger.debug(f"monitor: consent scan failed: {exc}")
                result = {"found": False}

            if result.get("found"):
                logger.success(
                    f"  terms of service      : agreed via "
                    f"{result.get('handler') or result.get('text')!r}"
                )
                self.state.log_event("bot", "terms of service accepted", status="ok")
                await self._settle(page)
                await asyncio.sleep(1.5)
                return True

            await asyncio.sleep(1.0)
            waited += 1.0

        logger.info("  terms of service      : no consent page appeared")
        return False

    async def _wait_for_verified(self, page: Any, timeout: float = 45.0) -> bool:
        """Wait for "Appointment Verified" to appear after the captcha.

        CONFIRMED: #btnVerifyAppointment is style="display:none" once accepted
        and #btnVerifiedAppointment becomes visible. Either signal will do, and
        the state is polled rather than assumed because the portal has to round
        trip the captcha first.
        """
        waited = 0.0
        last: dict[str, Any] = {}
        while waited < timeout:
            try:
                last = await page.evaluate(
                    """
                    () => {
                      const shown = (id) => {
                        const el = document.getElementById(id);
                        if (!el) return null;
                        const s = getComputedStyle(el);
                        return s.display !== 'none' && s.visibility !== 'hidden';
                      };
                      const flag = document.getElementById('AppointmentVerified');
                      return {
                        verify: shown('btnVerifyAppointment'),
                        verified: shown('btnVerifiedAppointment'),
                        submit: shown('btnSubmit'),
                        flag: flag ? flag.value : null,
                      };
                    }
                    """
                )
            except Exception as exc:
                logger.debug(f"monitor: verified check failed: {exc}")
                last = {}
            if last.get("verified") or str(last.get("flag")).lower() == "true":
                return True
            # The verify button disappearing is the same signal from the other
            # side, but only once something else is on screen in its place.
            if last.get("verify") is False and last.get("submit"):
                return True
            await asyncio.sleep(1.0)
            waited += 1.0
        logger.warning(f"  appointment verified  : state after {timeout:.0f}s — {last}")
        return False

    async def _click_when_ready(
        self, page: Any, button_id: str, fallback_fn: str, timeout: float = 30.0
    ) -> bool:
        """Click a button once the page reveals it, or call its handler.

        These controls start style="display:none" and are shown only when their
        preconditions are satisfied, so clicking immediately would miss them.
        """
        waited = 0.0
        while waited < timeout:
            try:
                state = await page.evaluate(
                    """
                    (args) => {
                      const el = document.getElementById(args.id);
                      if (!el) return 'missing';
                      const shown = getComputedStyle(el).display !== 'none';
                      if (!shown) return 'hidden';
                      if (el.disabled) return 'disabled';
                      el.click();
                      return 'clicked';
                    }
                    """,
                    {"id": button_id},
                )
            except Exception as exc:
                logger.debug(f"monitor: {button_id} probe failed: {exc}")
                state = "error"
            if state == "clicked":
                return True
            await asyncio.sleep(1.0)
            waited += 1.0

        logger.warning(f"monitor: #{button_id} never appeared — trying {fallback_fn}()")
        try:
            called = await page.evaluate(
                """
                (name) => {
                  if (typeof window[name] === 'function') {
                    window[name](new Event('click'), null);
                    return true;
                  }
                  return false;
                }
                """,
                fallback_fn,
            )
            return bool(called)
        except Exception as exc:
            logger.debug(f"monitor: {fallback_fn}() failed: {exc}")
            return False

    async def _verify_email(self, page: Any) -> bool:
        """Request the email verification code, read it, and submit it.

        CONFIRMED controls:
            <button id="btnSenderificationCode" onclick="RequestCode(event,this)">
            <input  id="EmailVerificationCode">
            <button id="btnVerifyEmail" onclick="VerifyEmailCode(event,this)">
            <button id="btnVerifiedEmail">  (revealed once accepted)

        The code is read over IMAP. Without otp.imap_user / imap_pass the step
        is skipped with a clear message rather than silently stalling.
        """
        otp_cfg = self.config.get("otp", {}) or {}
        if not otp_cfg.get("imap_user") or not otp_cfg.get("imap_pass"):
            logger.warning(
                "  email code            : SKIPPED — otp.imap_user / imap_pass "
                "are empty, so the code cannot be read"
            )
            self.state.log_event(
                "bot", "email verification needs IMAP credentials", status="warn"
            )
            return False

        try:
            requested = await page.evaluate(
                """
                () => {
                  const btn = document.getElementById('btnSenderificationCode')
                    || document.querySelector('[onclick*="RequestCode"]');
                  if (!btn) return false;
                  btn.click();
                  return true;
                }
                """
            )
        except Exception as exc:
            logger.warning(f"  email code            : request failed — {exc}")
            return False
        if not requested:
            logger.warning("  email code            : no 'Get Verification Code' button")
            return False
        logger.info("  email code            : requested, waiting for the mail")
        # RequestCode() pops a confirmation modal; the code step cannot proceed
        # while it is up.
        await asyncio.sleep(1.5)
        await self._dismiss_open_modals(page)
        await asyncio.sleep(1.5)

        code = await otp.fetch_email_code(self.config)
        if not code:
            logger.error("  email code            : never arrived")
            self.state.log_event("bot", "email verification code not received", status="error")
            return False
        logger.success(f"  email code            : received ({len(code)} digits)")

        try:
            filled = await page.evaluate(
                """
                (value) => {
                  const box = document.getElementById('EmailVerificationCode');
                  if (!box) return false;
                  box.value = value;
                  box.dispatchEvent(new Event('input', { bubbles: true }));
                  box.dispatchEvent(new Event('change', { bubbles: true }));
                  const btn = document.getElementById('btnVerifyEmail')
                    || document.querySelector('[onclick*="VerifyEmailCode"]');
                  if (btn) { btn.click(); return true; }
                  return 'filled-no-button';
                }
                """,
                code,
            )
        except Exception as exc:
            logger.warning(f"  email code            : entry failed — {exc}")
            return False

        # The result was being discarded, which made a MISSING code box look
        # like success: the booking would then carry on unverified.
        if filled is False:
            logger.error(
                "  email code            : no #EmailVerificationCode box on the "
                "page — cannot enter the code"
            )
            await utils.screenshot(page, "email-code-no-input")
            await utils.dump_page_html(page, "email-code-no-input")
            return False
        if filled == "filled-no-button":
            logger.warning(
                "  email code            : entered, but no verify button found — "
                "relying on the form submit to carry it"
            )
        else:
            logger.info("  email code            : entered and verify clicked")

        await asyncio.sleep(2.5)
        try:
            verified = await page.evaluate(
                """
                () => {
                  const ok = document.getElementById('btnVerifiedEmail');
                  const flag = document.getElementById('EmailVerified');
                  const shown = ok && getComputedStyle(ok).display !== 'none';
                  return { shown: !!shown, flag: flag ? flag.value : null };
                }
                """
            )
        except Exception:
            verified = {}
        logger.info(f"  email verified        : {verified}")
        await utils.screenshot(page, "appointment-email-verified")
        return bool(verified.get("shown") or str(verified.get("flag")).lower() == "true")

    async def _pick_green_date(
        self, page: Any, want_iso: str = ""
    ) -> dict[str, Any]:
        """Open the date picker; report the days on offer, optionally clicking one.

        Returns {"ok", "offered", "clicked"}. With ``want_iso`` empty this only
        surveys the calendar, so the caller can walk the candidates itself.

        The page carries decoy pickers (AppointmentDate1..5); all but one are
        k-state-disabled, so the enabled one is the real field. Availability is
        shown by cell colour, so green is detected from the computed background
        rather than a class name, which is random here as everywhere else.
        """
        # The booking page is server-rendered first and turned into Kendo
        # widgets afterwards. Scanning too early finds the raw input -- which
        # carries disabled="disabled" and NO data-role attribute yet -- and
        # concludes there is no picker at all. Run 20261013_213811 hit exactly
        # that ~3s after the page loaded and reported "no selectable (green)
        # date offered" on a booking page that was perfectly fine; the tell is
        # that its page survey still listed the raw AppointmentSlot3,
        # MissionId and VisaType inputs, which Kendo hides once it wraps them.
        # So poll until a picker is genuinely initialised AND enabled.
        deadline = 25.0
        waited = 0.0
        opened = None
        report: dict[str, Any] = {}
        while waited < deadline:
            try:
                report = await page.evaluate(_DATEPICKER_OPEN_JS)
            except Exception as exc:
                logger.warning(f"monitor: could not open the date picker: {exc}")
                return {"ok": False, "offered": []}
            opened = report.get("opened")
            if opened:
                break
            if report.get("ready") and not report.get("enabled"):
                # Initialised but still disabled: the page gates the date on
                # something else finishing. Keep waiting rather than giving up.
                pass
            await asyncio.sleep(1.0)
            waited += 1.0

        if not opened:
            logger.warning(
                f"monitor: no usable date picker after {deadline:.0f}s — "
                f"{report.get('total', 0)} datepicker input(s), "
                f"{report.get('ready', 0)} initialised, "
                f"{report.get('enabled', 0)} enabled; "
                f"ids={report.get('ids')}"
            )
            await utils.dump_page_html(page, "appointment-no-datepicker")
            return {"ok": False, "offered": []}
        if waited:
            logger.info(f"  date picker           : ready after {waited:.0f}s")
        logger.info(f"  date picker           : opened #{opened}")
        # The slot dropdown shares this suffix; see _SLOT_OPEN_JS.
        self._date_field_id = str(opened)
        await asyncio.sleep(1.2)
        await utils.screenshot(page, "appointment-calendar-open")

        # The portal will not honour a booking that is too close to today;
        # run 20261013_155949 picked tomorrow (14th, with today the 13th) and
        # the time-slot list then never populated at all.
        cutoff = datetime.now().date() + timedelta(days=self.min_days_ahead)
        min_stamp = int(
            datetime(cutoff.year, cutoff.month, cutoff.day).timestamp() * 1000
        )
        logger.info(
            f"  earliest allowed date : {cutoff.isoformat()} "
            f"({self.min_days_ahead} day(s) ahead)"
        )
        try:
            picked = await page.evaluate(
                """
                ({ pickerId, minStamp, wantIso }) => {
                  const green = (el) => {
                    const bg = getComputedStyle(el).backgroundColor || '';
                    const m = /rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/.exec(bg);
                    if (!m) return false;
                    const r = +m[1], g = +m[2], b = +m[3];
                    // Clearly green: dominant green channel, not grey/red/amber.
                    return g > 90 && g > r + 40 && g > b + 40;
                  };
                  const cells = Array.from(
                    document.querySelectorAll('.k-calendar td, .k-calendar .k-link'));
                  const options = [];
                  const seen = {};
                  for (const cell of cells) {
                    if (cell.classList.contains('k-state-disabled')) continue;
                    if (cell.classList.contains('k-other-month')) continue;
                    const link = cell.matches('.k-link') ? cell : cell.querySelector('.k-link');
                    const paint = green(cell) || (link && green(link));
                    const text = ((link || cell).innerText || '').trim();
                    if (!text) continue;
                    // Kendo puts the real date on the cell, which is the only
                    // reliable way to measure lead time across a month edge.
                    const stamp = (cell.getAttribute('data-value') ||
                                   (link && link.getAttribute('data-value')) || '');
                    // No data-value means this is not a day cell at all -- the
                    // month header matched the selector and used to be listed
                    // as if it were an offered date ("October 2026").
                    if (!stamp) continue;
                    const parts = stamp.split('/').map(Number);
                    if (parts.length !== 3) continue;
                    // data-value is "YYYY/M/D" with a 0-based month.
                    const when = new Date(parts[0], parts[1], parts[2]);
                    const pad = (n) => String(n).padStart(2, '0');
                    const iso = when.getFullYear() + '-' +
                      pad(when.getMonth() + 1) + '-' + pad(when.getDate());
                    // td and its .k-link both match, so every day appeared
                    // twice; key on the date instead of the cell.
                    if (seen[iso]) continue;
                    seen[iso] = true;
                    const soon = when.getTime() < minStamp;
                    options.push({ text, green: !!paint, soon, iso });
                    // Click only the date asked for; with no target, just
                    // report what is on offer so the caller can try each in
                    // turn. A date being green does NOT mean it has slots:
                    // the 14th was painted available in every run and its
                    // time-slot list was empty every time, while the 15th
                    // offered three. Abandoning the combination after one
                    // empty date threw away the dates behind it.
                    if (wantIso && iso === wantIso) {
                      (link || cell).click();
                      return { clicked: text, clickedIso: iso, offered: options };
                    }
                  }
                  return { clicked: null, offered: options };
                }
                """,
                {"pickerId": opened, "minStamp": min_stamp, "wantIso": want_iso},
            )
        except Exception as exc:
            logger.warning(f"monitor: date selection failed: {exc}")
            return {"ok": False, "offered": []}

        offered = picked.get("offered") or []
        logger.info(
            "  days offered          : "
            + ", ".join(
                f"{d['text']}{'*' if d['green'] else ''}{'(too soon)' if d.get('soon') else ''}"
                for d in offered[:31]
            )
            + "   (* = available)"
        )
        if not picked.get("clicked"):
            return {"ok": True, "offered": offered, "clicked": None}

        logger.success(f"  date chosen           : {picked['clicked']}")
        await self._settle(page)
        await asyncio.sleep(1.5)
        return {"ok": True, "offered": offered, "clicked": picked["clicked"]}

    async def _pick_green_slot(self, page: Any, date_id: str = "") -> bool:
        """Wait for the slot list to arrive, then click a free (green) option.

        CONFIRMED: the options are fetched by AJAX AFTER a date is chosen, and
        Kendo renders its <li>s from whatever the dataSource holds AT OPEN
        TIME. Opening the box once, as soon as the widget exists, therefore
        builds an empty list that never refills however long it is then
        watched -- which is exactly how a date with slots was being reported as
        having none.

        So the box is RE-OPENED on every poll. Each attempt rebuilds the list
        from the current data, and the first one that renders options wins.
        Availability is a background colour on the item, as on the calendar.
        """
        deadline = float(self.slot_wait_seconds)
        waited = 0.0
        step = 1.0
        input_id = ""
        options: list[dict[str, Any]] = []
        opened: dict[str, Any] = {}

        # A short settle first: the request only goes out once the date change
        # has been handled, so polling instantly just burns an attempt.
        await asyncio.sleep(1.5)

        while waited < deadline:
            try:
                opened = await page.evaluate(_SLOT_OPEN_JS, date_id)
            except Exception as exc:
                logger.warning(f"  slot                  : open failed - {exc}")
                return False

            if opened.get("ok"):
                input_id = str(opened.get("id") or "")
                try:
                    read = await page.evaluate(
                        _SLOT_PICK_JS, {"inputId": input_id, "wantIndex": None}
                    )
                except Exception as exc:
                    logger.warning(f"  slot                  : read failed - {exc}")
                    return False
                options = read.get("options") or []
                if options:
                    break
                if waited == 0 or waited % 5 == 0:
                    logger.info(
                        f"  slot                  : waiting for the list "
                        f"({waited:.0f}s; dataSource rows="
                        f"{opened.get('dataCount')}, rendered="
                        f"{opened.get('rendered')})"
                    )
            await asyncio.sleep(step)
            waited += step

        if not opened.get("ok"):
            logger.warning(
                f"  slot                  : {opened.get('reason')} after "
                f"{deadline:.0f}s (widgets seen: {opened.get('seen')})"
            )
            return False

        if not options:
            logger.warning(
                f"  slot                  : no options after {deadline:.0f}s "
                f"(dataSource rows={opened.get('dataCount')}) - this date has none"
            )
            await utils.screenshot(page, "appointment-slot-list-empty")
            return False

        logger.info(f"  slot dropdown         : #{input_id}, ready after {waited:.0f}s")
        logger.info(
            "  slots offered         : "
            + ", ".join(
                f"{o['text']}{'*' if o['free'] else '(full)' if o['full'] else ''}"
                for o in options[:20]
            )
            + "   (* = free)"
        )

        free = [o for o in options if o.get("free")]
        if not free:
            # Log the colours so a wrong threshold is diagnosable rather than
            # silently reading every slot as taken.
            logger.warning(
                "  slot colours          : "
                + ", ".join(f"{o['text']}={o['colour']}" for o in options[:9])
            )
            logger.warning("  slot                  : every option is taken")
            await utils.screenshot(page, "appointment-no-green-slot")
            return False

        choice = free[0]
        try:
            picked = await page.evaluate(
                _SLOT_PICK_JS, {"inputId": input_id, "wantIndex": choice["index"]}
            )
        except Exception as exc:
            logger.warning(f"  slot                  : click failed - {exc}")
            return False
        if not picked.get("ok") or not picked.get("clicked"):
            logger.warning(
                f"  slot                  : could not click {choice['text']!r} "
                f"({picked.get('reason')})"
            )
            return False

        logger.success(f"  slot chosen           : {picked['clicked']!r}")
        await self._settle(page)
        await asyncio.sleep(1.0)
        return True

    async def _report_no_slot_on_any_date(self, dates: list[dict[str, Any]]) -> None:
        """Tell the manager the dates were offered but none had a free slot.

        Distinct from "no possible appointment date": here the calendar DID
        offer bookable days, they simply had nothing behind them.
        """
        shown = []
        for entry in dates:
            iso = str(entry.get("iso") or "")
            shown.append(f"{iso[5:7]}/{iso[8:10]}" if len(iso) == 10 else str(entry.get("text", "")))
        combo = self._current_combo_text or "current selection"
        signature = f"noslot|{combo}|{','.join(shown)}"
        if signature == self._last_no_date_signature:
            return
        self._last_no_date_signature = signature
        message = (
            "no free time slot on any available date, dates checked are "
            + ", ".join(shown)
        )
        logger.info(f"  notified              : {message}")
        self.state.log_event("bot", message, status="warn")
        await self.notifier.send(f"🕒 {message}\n{combo}")

    async def _report_no_date(self, free: list[dict[str, Any]]) -> None:
        """Tell the manager no bookable date exists, naming what WAS offered.

        Sent once per distinct set of dates, not once per cycle: the sweep runs
        every few minutes and the calendar rarely changes between runs, so
        repeating it would bury every other alert.
        """
        shown: list[str] = []
        for entry in free:
            iso = str(entry.get("iso") or "")
            # Display as MM/DD; fall back to the raw day number.
            if len(iso) == 10:
                shown.append(f"{iso[5:7]}/{iso[8:10]}")
            elif entry.get("text"):
                shown.append(str(entry["text"]))

        combo = self._current_combo_text or "current selection"
        signature = f"{combo}|{','.join(shown)}"
        if signature == self._last_no_date_signature:
            logger.info("  date                  : same as last report — not re-sending")
            return
        self._last_no_date_signature = signature

        if shown:
            dates = ", ".join(shown)
            message = (
                f"there is no possible appointment date, available dates are {dates}"
            )
            detail = (
                f"\n\nThey are all within {self.min_days_ahead} day(s) of today, "
                "which the portal will not book."
            )
        else:
            message = "there is no possible appointment date, no dates are offered"
            detail = ""

        logger.info(f"  notified              : {message}")
        self.state.log_event("bot", message, status="warn")
        await self.notifier.send(f"📅 {message}\n{combo}{detail}")

    async def _dismiss_open_modals(self, page: Any, *, rounds: int = 3) -> list[str]:
        """Close every modal that is actually on screen, confirming it.

        CONFIRMED: the booking page carries a stack of modals (photoUploadModal,
        familyDisclaimer, LivenessDeclaration, userConsent, scamAlert,
        commonModal, ...) and only the open one has computed display:block.
        Uploading the photo opens "Confirm Photo", whose primary button is
        "Understood"; leaving it up blocks every later click. Run
        20261013_162707 ended with photoUploadModal still show/display:block,
        which is why "Verify Appointment" then sat for 31s until the final
        captcha expired and #btnSubmit never appeared.

        The PRIMARY button is the one to press: "Understood" / "Ok" / "Accept".
        Never the secondary ("Change Photo"), which restarts the upload.
        """
        dismissed: list[str] = []
        for _ in range(rounds):
            try:
                closed = await page.evaluate(
                    """
                    () => {
                      // Bootstrap marks the OPEN modal with .show plus an
                      // inline display:block, and leaves the rest display:none
                      // via its stylesheet. Requiring a positive signal (not
                      // merely "the stylesheet did not hide it") keeps a
                      // dormant modal from being clicked if the CSS is slow or
                      // absent -- without this, scamAlert was picked ahead of
                      // the photo modal that was actually up.
                      const onScreen = (el) => {
                        const s = getComputedStyle(el);
                        if (s.display === 'none' || s.visibility === 'hidden') return false;
                        const open = el.classList.contains('show')
                          || (el.style && el.style.display === 'block');
                        if (!open) return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 20 && r.height > 20;
                      };
                      for (const modal of document.querySelectorAll('.modal')) {
                        if (!onScreen(modal)) continue;
                        const label =
                          (modal.querySelector('.modal-title') || {}).innerText ||
                          modal.id || 'modal';
                        // Primary action first; it is the confirming one.
                        let btn = modal.querySelector('.modal-footer .btn-primary')
                               || modal.querySelector('.btn-primary');
                        if (!btn) {
                          for (const b of modal.querySelectorAll('button, a.btn')) {
                            const t = (b.innerText || '').trim().toLowerCase();
                            if (['ok', 'okay', 'understood', 'accept', 'yes', 'close']
                                  .includes(t)) { btn = b; break; }
                          }
                        }
                        if (!btn) continue;
                        btn.click();
                        return { label: String(label).trim().slice(0, 60),
                                 button: (btn.innerText || '').trim().slice(0, 30),
                                 id: modal.id || '' };
                      }
                      return null;
                    }
                    """
                )
            except Exception as exc:
                logger.debug(f"monitor: modal sweep failed: {exc}")
                return dismissed
            if not closed:
                break
            what = f"{closed.get('label')!r} via {closed.get('button')!r}"
            logger.info(f"  modal dismissed       : {what}")
            dismissed.append(str(closed.get("id") or closed.get("label")))
            await asyncio.sleep(0.8)
        return dismissed

    async def _upload_photo(self, page: Any) -> bool:
        """Attach the applicant photo to the booking form."""
        photo = (self.config.get("applicant", {}) or {}).get("documents", {}).get("photo")
        if not photo:
            logger.warning("  photo                 : not configured")
            return False
        from pathlib import Path as _Path

        path = _Path(photo)
        if not path.is_absolute():
            path = _Path.cwd() / path
        if not path.exists():
            logger.error(f"  photo                 : MISSING at {path}")
            self.state.log_event("bot", f"photo not found: {photo}", status="error")
            return False
        size_kb = path.stat().st_size / 1024
        if size_kb > 200:
            logger.warning(
                f"  photo                 : {size_kb:.0f} KB — the form requires "
                "under 200 KB and will reject it"
            )

        try:
            element = await page.query_selector("#uploadfile-1")
            if element is None:
                element = await page.query_selector('input[type="file"]')
            if element is None:
                logger.warning("  photo                 : no file input found")
                return False
            await element.set_input_files(str(path))
            logger.success(f"  photo                 : uploaded {path.name} ({size_kb:.0f} KB)")
            # "Confirm Photo" opens here and blocks everything behind it.
            await asyncio.sleep(1.2)
            await self._dismiss_open_modals(page)
            await asyncio.sleep(1.5)
            return True
        except Exception as exc:
            logger.warning(f"  photo                 : upload failed — {exc}")
            return False

    async def _unfilled_fields(self, page: Any) -> list[str]:
        """Labels of visible required dropdowns still showing the placeholder."""
        try:
            return await page.evaluate(
                """
                () => {
                  const vis = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width < 5 || r.height < 5) return false;
                    let n = el;
                    while (n && n.nodeType === 1) {
                      const s = getComputedStyle(n);
                      if (s.display === 'none' || s.visibility === 'hidden') return false;
                      n = n.parentElement;
                    }
                    return true;
                  };
                  const empty = [];
                  for (const inp of document.querySelectorAll('input[data-role="dropdownlist"]')) {
                    const widget = inp.closest('span.k-widget');
                    if (!vis(widget)) continue;
                    const box = inp.closest('div.mb-3');
                    if (box && !vis(box)) continue;
                    const shown = widget.querySelector('.k-input');
                    const text = ((shown && shown.innerText) || '').trim();
                    if (!text || /^-+\\s*select/i.test(text)) {
                      const lab = inp.id
                        ? document.querySelector('label[for="' + inp.id + '"]') : null;
                      let name = (lab && lab.innerText) || inp.name || inp.id || '?';
                      empty.push(name.replace(/\\*/g, '').trim());
                    }
                  }
                  return empty;
                }
                """
            )
        except Exception as exc:
            logger.debug(f"monitor: field check failed: {exc}")
            return []

    async def _read_common_modal(self, page: Any) -> dict[str, str] | None:
        """Read the portal's shared result modal, if it is showing."""
        try:
            return await page.evaluate(
                """
                () => {
                  const head = document.getElementById('commonModalHeader');
                  const body = document.getElementById('commonModalBody');
                  if (!head && !body) return null;
                  const probe = head || body;
                  const r = probe.getBoundingClientRect();
                  if (r.width < 5 || r.height < 5) return null;
                  let n = probe;
                  while (n && n.nodeType === 1) {
                    if (getComputedStyle(n).display === 'none') return null;
                    n = n.parentElement;
                  }
                  return {
                    header: (head && head.innerText) || '',
                    body: (body && body.innerText) || '',
                  };
                }
                """
            )
        except Exception as exc:
            logger.debug(f"monitor: modal read failed: {exc}")
            return None

    async def _dismiss_common_modal(self, page: Any) -> None:
        """Close the result modal via its own dismiss control."""
        try:
            await page.evaluate(
                """
                () => {
                  const btn = document.querySelector(
                    '.modal-footer button[data-bs-dismiss="modal"], '
                    + '.modal-header button[data-bs-dismiss="modal"]');
                  if (btn) { btn.click(); return true; }
                  return false;
                }
                """
            )
            await asyncio.sleep(0.8)
        except Exception as exc:
            logger.debug(f"monitor: modal dismiss failed: {exc}")

    async def _form_validation_error(self, page: Any) -> str | None:
        """The portal's own inline validation message, if one is showing."""
        try:
            return await page.evaluate(
                """
                () => {
                  const sel = '.validation-summary, .field-validation-error,'
                            + ' .text-danger, .validation-summary-errors';
                  for (const el of document.querySelectorAll(sel)) {
                    const r = el.getBoundingClientRect();
                    if (r.width < 5 || r.height < 5) continue;
                    if (getComputedStyle(el).display === 'none') continue;
                    const t = (el.innerText || '').trim();
                    if (t && t.length > 3) return t.slice(0, 200);
                  }
                  return null;
                }
                """
            )
        except Exception:
            return None

    async def _restart_journey(self, page: Any) -> None:
        """Reload, and if the portal has dropped us at login, start over.

        Reached when the appointment POST fails (4xx) or bounces to login —
        typically an expired session. Reloading alone is not enough: the stored
        cookies are stale too, so they are discarded and the next cycle performs
        a full login, verification captcha and form fill from scratch.
        """
        logger.warning("monitor: restarting the journey from the login page")
        try:
            await page.reload(wait_until="domcontentloaded")
            await self._settle(page)
        except Exception as exc:
            logger.debug(f"monitor: reload failed: {exc}")

        url_now = (page.url or "").lower()
        at_login = "/account/login" in url_now
        logger.info(f"monitor: after reload — {page.url} (login page: {at_login})")

        # Drop the saved session either way: whatever state produced a 4xx is
        # not worth replaying on the next cycle.
        try:
            self.auth._clear_saved_session()
            logger.info("monitor: stored session discarded — next cycle logs in fresh")
        except Exception as exc:
            logger.debug(f"monitor: could not clear the session: {exc}")

        self.state.log_event(
            "bot", "appointment submit failed — restarting from login", status="warn"
        )

    def install_navigation_watch(self, page: Any) -> None:
        """Track every main-frame navigation and name the stage it lands on.

        The portal can drop the session at any moment — a redirect to the login
        page can happen mid-step, not only at the checkpoints the code happens
        to test. Watching navigations catches it wherever it occurs, and sets a
        restart flag the running step checks so the journey begins again from
        the login page instead of continuing against a dead session.
        """
        if id(page) in self._nav_hooked:
            return

        def _on_nav(frame: Any) -> None:
            try:
                if frame != page.main_frame:
                    return  # iframes navigate constantly; only the page matters
                url = frame.url or ""
            except Exception:
                return

            stage = classify_page(url)
            previous = self._page_stage
            self._page_stage = stage
            if stage != previous:
                logger.info(f"monitor: page -> {stage}  ({url[:110]})")

            # Landing back on login from anywhere further along means the
            # session died; the journey has to restart.
            if stage == "login" and previous not in (None, "login", "login-captcha"):
                logger.warning(
                    f"monitor: bounced to the login page from {previous!r} — "
                    "session lost, restarting the journey"
                )
                self._needs_restart = True

            # A refresh can also drop us back to the verification gate. That is
            # cheaper than a full restart: the session is still good, only the
            # visa-type verification has to be redone.
            if stage == "verification" and previous in ("appointment-form", "appointment"):
                logger.warning(
                    f"monitor: returned to the verification gate from {previous!r} "
                    "— it must be solved again"
                )
                self._needs_reverify = True

        try:
            page.on("framenavigated", _on_nav)
            self._nav_hooked.add(id(page))
            self._page_stage = classify_page(page.url or "")
            logger.info(f"monitor: navigation watch installed (now: {self._page_stage})")
        except Exception as exc:
            logger.debug(f"monitor: could not install the navigation watch: {exc}")

    def _restart_requested(self) -> bool:
        """True once a navigation showed we were dropped back at login."""
        if self._needs_restart:
            self._needs_restart = False
            return True
        return False

    def _install_form_listener(self, page: Any) -> None:
        """Record the status of the appointment form's own POST."""
        if id(page) in self._form_hooked:
            return

        def _on_response(response: Any) -> None:
            url = (response.url or "").lower()
            if "/bls/visatype" in url and "verification" not in url:
                self._last_visatype_status = response.status
                logger.info(f"monitor: [net] {response.status} {response.url[:120]}")

        try:
            page.on("response", _on_response)
            self._form_hooked.add(id(page))
        except Exception as exc:
            logger.debug(f"monitor: could not install the form listener: {exc}")

    async def _handle_form_modals(self, page: Any) -> None:
        """Dismiss the modals the appointment form raises while being filled.

        CONFIRMED:
          * Family Appointment — <button onclick="return OnFamilyAccept();">
            Accept. Appears whenever Appointment For is set to Family, and the
            form cannot be submitted until it is cleared.
          * #PremiumTypeModel — shown by onCategoryChangeN() when the chosen
            category's Code is CATEGORY_PREMIUM.

        Calls the page's own handlers; clicking by text would hit the <button>
        correctly here, but the handler is what the site actually relies on.
        """
        try:
            result = await page.evaluate(
                """
                () => {
                  const out = [];
                  const vis = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 5 && r.height > 5 &&
                           getComputedStyle(el).display !== 'none';
                  };
                  // Family confirmation.
                  const fam = Array.from(document.querySelectorAll('button'))
                    .find((b) => /OnFamilyAccept/.test(b.getAttribute('onclick') || '')
                                 && vis(b));
                  if (fam) { fam.click(); out.push('family-accept'); }
                  else if (typeof OnFamilyAccept === 'function') {
                    const dlg = document.querySelector('.modal.show, .modal[style*="display: block"]');
                    if (dlg && /family/i.test(dlg.innerText || '')) {
                      OnFamilyAccept(); out.push('family-accept-fn');
                    }
                  }
                  // Premium category notice.
                  const prem = document.getElementById('PremiumTypeModel');
                  if (prem && vis(prem)) {
                    const ok = prem.querySelector('button.btn-success, button.btn-primary');
                    if (ok) { ok.click(); out.push('premium-dismissed'); }
                    else { out.push('premium-open-no-button'); }
                  }
                  return out;
                }
                """
            )
        except Exception as exc:
            logger.debug(f"monitor: modal handling failed: {exc}")
            return
        if result:
            logger.info(f"  form modals           : {result}")
            await asyncio.sleep(1.0)

    async def _form_failed(self, page: Any, tag: str) -> bool:
        await utils.screenshot(page, f"visatype-{tag}-failed")
        await utils.dump_page_html(page, f"visatype-{tag}-failed")
        await self._survey_page(page, f"visatype-{tag}-failed")
        return False

    @staticmethod
    async def _frame_offset(frame: Any) -> tuple[float, float]:
        """Where the iframe sits in the page, for converting clip coordinates.

        Tile rects measured inside the frame are frame-relative, but only a Page
        can screenshot, and its clip is in page coordinates. Adding the iframe's
        own position bridges the two.
        """
        try:
            element = await frame.frame_element()
            box = await element.bounding_box()
            if box:
                logger.info(
                    f"monitor: captcha frame at x={box['x']:.0f} y={box['y']:.0f} "
                    f"({box['width']:.0f}x{box['height']:.0f})"
                )
                return float(box["x"]), float(box["y"])
        except Exception as exc:
            logger.warning(f"monitor: could not locate the iframe: {exc}")
        return 0.0, 0.0

    async def _probe_captcha_endpoint(self, page: Any) -> None:
        """Fetch /Global/NewCaptcha/GenerateCaptcha directly and report back.

        Separates the two candidate causes. If this returns the grid, the frame
        failed for transport reasons (mixed-content blocking). If it returns the
        login page, the session is not valid for that endpoint and no amount of
        frame handling will help.
        """
        base = (self.config.get("bls", {}) or {}).get("url", "").rstrip("/")
        url = f"{base}/Global/NewCaptcha/GenerateCaptcha"
        logger.info(f"monitor: probing the captcha endpoint directly — {url}")
        probe = None
        try:
            probe = await page.context.new_page()
            response = await probe.goto(url, wait_until="domcontentloaded")
            status = response.status if response else "?"
            text = (await probe.inner_text("body"))[:300] if response else ""
            looks_login = "/account/login" in (probe.url or "").lower()
            has_grid = bool(re.search(r"number\s+\d+", text, re.I))
            logger.info(f"  probe status    : {status}")
            logger.info(f"  probe final url : {probe.url}")
            logger.info(f"  looks like login: {looks_login}")
            logger.info(f"  has number prompt: {has_grid}")
            logger.info(f"  body starts     : {text[:160]!r}")
            await utils.screenshot(probe, "captcha-endpoint-probe")
            await utils.dump_page_html(probe, "captcha-endpoint-probe")
            if looks_login:
                logger.error(
                    "monitor: the captcha endpoint redirects to login — the "
                    "session is not accepted for it. This is an auth problem, "
                    "not an iframe problem."
                )
                await self._dump_session_cookies(page, url)
            elif has_grid:
                logger.warning(
                    "monitor: the endpoint serves the grid fine on its own, so "
                    "the iframe is being blocked in the browser (mixed content)."
                )
        except Exception as exc:
            logger.warning(f"monitor: captcha endpoint probe failed: {exc}")
        finally:
            if probe is not None:
                try:
                    await probe.close()
                except Exception:
                    pass

    async def _dump_session_cookies(self, page: Any, target_url: str) -> None:
        """List the cookies the browser holds, to show what the endpoint saw.

        The parent page is authenticated while GenerateCaptcha is not, so the
        difference is in what gets sent: either an auth cookie is scoped so it
        does not reach that path, or a fingerprint cookie the endpoint requires
        was never set (which is what a crashed FingerprintJS would cause).
        """
        try:
            cookies = await page.context.cookies([target_url])
        except Exception as exc:
            logger.warning(f"monitor: could not read cookies: {exc}")
            return

        logger.info(f"  cookies sent to {target_url[:70]} — {len(cookies)} total")
        for cookie in cookies:
            name = cookie.get("name", "")
            value = str(cookie.get("value", ""))
            # Never log a session or fingerprint value, only its shape.
            logger.info(
                f"     {name:<34} len={len(value):<5} "
                f"path={cookie.get('path', '')} "
                f"secure={cookie.get('secure')} "
                f"httpOnly={cookie.get('httpOnly')} "
                f"sameSite={cookie.get('sameSite')}"
            )
        names = {c.get("name", "").lower() for c in cookies}
        if not any("auth" in n or "session" in n or ".aspnet" in n for n in names):
            logger.error(
                "  no auth/session cookie reaches this URL — that alone explains "
                "the redirect to login"
            )

    async def _open_verification_popup(self, page: Any) -> Any | None:
        """Click "Verify Selection" and return the page holding the challenge.

        Returns the popup page when one opens, the original page when the
        challenge renders inline, or None when the button could not be clicked.
        """
        clicked = False
        popup = None
        try:
            async with page.context.expect_page(timeout=8000) as info:
                clicked = await utils.click_by_text(
                    page, VERIFY_SELECTION_TEXTS, config=self.config
                )
                if not clicked:
                    raise PlaywrightTimeout("button not clicked")
            popup = await info.value
        except PlaywrightTimeout:
            pass
        except Exception as exc:
            logger.debug(f"monitor: popup wait ended: {exc}")

        if not clicked:
            # Last resort: the button carries a stable id on this portal.
            try:
                await page.evaluate(
                    "() => { const b = document.getElementById('btnVerify');"
                    " if (b) b.click(); }"
                )
                clicked = True
                logger.info("monitor: clicked Verify Selection via #btnVerify")
            except Exception as exc:
                logger.warning(f"monitor: #btnVerify click failed: {exc}")
                return None
        else:
            logger.info("monitor: clicked 'Verify Selection'")

        if popup is not None:
            try:
                await popup.wait_for_load_state("domcontentloaded")
            except Exception:
                pass
            await utils.human_delay(self.config)
            return popup

        await self._settle(page)
        return page

    def _install_dialog_handler(self, page: Any) -> None:
        """Capture and accept native JS dialogs.

        Playwright auto-dismisses dialogs when nothing is listening, so an
        "Invalid selection" alert would disappear without trace and a failed
        attempt would be indistinguishable from a successful one. Registering a
        handler both accepts the dialog and records its message.
        """
        if id(page) in self._dialog_hooked:
            return

        def _on_dialog(dialog: Any) -> None:
            try:
                message = dialog.message or ""
            except Exception:
                message = ""
            self._dialogs.append(message)
            logger.info(f"monitor: JS dialog accepted — {message!r}")
            asyncio.ensure_future(self._accept_dialog(dialog))

        page.on("dialog", _on_dialog)
        self._dialog_hooked.add(id(page))
        logger.debug("monitor: dialog handler installed")

    @staticmethod
    async def _accept_dialog(dialog: Any) -> None:
        try:
            await dialog.accept()
        except Exception as exc:
            logger.debug(f"monitor: could not accept dialog: {exc}")

    async def _gate_state(self, page: Any) -> dict[str, Any]:
        """Visibility of the Verified / Submit buttons that follow a pass."""
        try:
            state = await page.evaluate(
                """
                () => {
                  const vis = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' &&
                           s.visibility !== 'hidden';
                  };
                  const byText = (re) => Array.from(
                      document.querySelectorAll('button, input[type=submit]'))
                    .filter(vis)
                    .filter((b) => re.test((b.innerText || b.value || '').trim()));
                  return {
                    verified: vis(document.getElementById('btnVerified'))
                              || byText(/^verified$/i).length > 0,
                    submit: vis(document.getElementById('btnSubmit'))
                            || byText(/^submit$/i).length > 0,
                    modalOpen: Array.from(
                        document.querySelectorAll('.modal, [role="dialog"]'))
                      .some(vis),
                    url: location.href,
                  };
                }
                """
            )
        except Exception as exc:
            logger.debug(f"monitor: gate probe failed: {exc}")
            return {}
        logger.info(f"  gate state      : {state}")
        return state

    async def _submit_verified_form(self, page: Any) -> bool:
        """Click the Submit revealed once the captcha has been verified.

        CONFIRMED markup:
            <button class="btn btn-primary" id="btnSubmit" type="submit">Submit</button>
        It is hidden until verification passes, then shown alongside a
        "Verified" badge. Submitting redirects to /Global/bls/visatype?data=...
        """
        clicked = False
        try:
            button = page.locator("#btnSubmit")
            await button.wait_for(state="visible", timeout=10000)
            await button.click(timeout=10000)
            clicked = True
            logger.info("monitor: clicked #btnSubmit")
        except Exception as exc:
            logger.debug(f"monitor: #btnSubmit not clickable ({exc}) — trying text")
            clicked = await utils.click_by_text(page, ("submit",), config=self.config)

        if not clicked:
            logger.warning("monitor: could not click the verification Submit")
            await utils.screenshot(page, "verified-submit-missing")
            return False

        # The form posts and redirects to the appointment selection page.
        try:
            await page.wait_for_url(
                lambda url: "/bls/visatype" in (url or "").lower()
                and "visatypeverification" not in (url or "").lower(),
                timeout=30000,
            )
            logger.success(f"monitor: reached the appointment form — {page.url}")
        except PlaywrightTimeout:
            logger.warning(
                f"monitor: no redirect to /bls/visatype within 30s — still at {page.url}"
            )
        await self._settle(page)
        await utils.screenshot(page, "visatype-form")
        await utils.dump_page_html(page, "visatype-form")
        return True

    async def _reload_captcha_images(self, page: Any) -> None:
        """Ask for a fresh grid after a rejected selection."""
        if await utils.click_by_text(page, RELOAD_IMAGES_TEXTS, config=self.config):
            logger.info("monitor: requested a fresh verification grid")
            await self._settle(page)
            return
        # No reload control — reopening the popup has the same effect.
        if await utils.click_by_text(page, VERIFY_SELECTION_TEXTS, config=self.config):
            logger.info("monitor: reopened the verification popup for a fresh grid")
            await self._settle(page)

    async def _survey_page(self, page: Any, label: str) -> None:
        """Log what is on the current page so the next step can be built."""
        try:
            info = await page.evaluate(
                """
                () => {
                  const txt = (el) => (el.innerText || el.value || '').trim().slice(0, 60);
                  const vis = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' &&
                           s.visibility !== 'hidden';
                  };
                  const pick = (sel) => Array.from(document.querySelectorAll(sel))
                    .filter(vis).slice(0, 25).map((el) => ({
                      t: txt(el), id: el.id || '', name: el.name || '',
                      href: el.getAttribute('href') || '',
                    }));
                  return {
                    url: location.href,
                    title: document.title,
                    headings: Array.from(document.querySelectorAll('h1,h2,h3,h4'))
                      .filter(vis).slice(0, 10).map(txt),
                    buttons: pick('button, input[type=submit], input[type=button]'),
                    links: pick('a[href]'),
                    selects: Array.from(document.querySelectorAll('select'))
                      .filter(vis).map((s) => ({
                        id: s.id, name: s.name,
                        options: Array.from(s.options).slice(0, 15).map((o) => o.text.trim()),
                      })),
                    inputs: pick('input:not([type=hidden])'),
                  };
                }
                """
            )
        except Exception as exc:
            logger.warning(f"monitor: page survey failed: {exc}")
            return

        logger.info("=" * 60)
        logger.info(f"PAGE SURVEY ({label})")
        logger.info(f"  url      : {info.get('url')}")
        logger.info(f"  title    : {info.get('title')}")
        logger.info(f"  headings : {info.get('headings')}")
        logger.info(f"  selects  : {info.get('selects')}")
        logger.info(f"  buttons  : {[b['t'] for b in info.get('buttons', []) if b['t']]}")
        logger.info(f"  inputs   : {[(i['name'] or i['id']) for i in info.get('inputs', [])]}")
        logger.info(f"  links    : {[l['href'] for l in info.get('links', []) if l['href']][:20]}")
        logger.info("=" * 60)
        await utils.screenshot(page, label)
        await utils.dump_page_html(page, label)

    async def _settle(self, page: Any) -> None:
        try:
        # MEASURED: networkidle never fires on this portal — it keeps a
        # connection open, so this ALWAYS burns the full timeout. At 15s it was
        # costing 15s per call and pushing the captcha token past its ~45s life
        # (run 20261013_044241: "clicked Verify Selection" 04:45:01 -> frame
        # ready 04:45:17). 3s is enough for the DOM work that follows.
            await page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            logger.debug("monitor: networkidle timed out")
        await utils.human_delay(self.config, scale=0.5)

    async def close(self) -> None:
        await self.auth.stop()

    # ------------------------------------------------------------------ #
    # Per-location check
    # ------------------------------------------------------------------ #
    async def _check_location(self, page: Any, location: str) -> None:
        self.state.set_location_status(location, STATUS_CHECKING, "checking availability")

        try:
            available, detail = await utils.with_retries(
                lambda: self._probe_location(page, location),
                attempts=self.max_attempts,
                delay=5,
                description=f"availability check for {location}",
            )
        except PlaywrightTimeout as exc:
            await self._location_error(page, location, f"timeout: {exc}", self.page_error_wait)
            return
        except Exception as exc:
            await self._location_error(page, location, str(exc), self.page_error_wait)
            return

        if not available:
            self.state.set_location_status(location, STATUS_NO_SLOTS, detail, checked=True)
            logger.info(f"monitor[{location}]: no slots ({detail})")
            self.state.log_event(location, f"no slots — {detail}", status="info")
            return

        # Slot found.
        self.state.set_location_status(location, STATUS_SLOT_FOUND, detail, checked=True)
        self.state.log_event(location, f"SLOT FOUND — {detail}", status="ok")
        logger.success(f"monitor[{location}]: slot found! {detail}")
        await self.notifier.slot_found(location, detail)

        await self.booking.book(page, location, slot_hint=detail)

    async def _probe_location(self, page: Any, location: str) -> tuple[bool, str]:
        """Navigate to the appointment page and decide whether slots exist.

        TODO(unconfirmed — needs live portal inspection): per CLAUDE.md the
        appointment page structure is TBD. Detection currently combines
        configured text phrases with generic "available" cell probes. Once the
        real markup is captured, replace this with an exact check (and record the
        availability endpoint if the portal fetches slots over XHR — polling that
        directly would be far more reliable than scraping the calendar).
        """
        url = self.auth.path("appointment")
        logger.debug(f"monitor[{location}]: navigating to {url}")
        await page.goto(url, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeout:
            logger.debug(f"monitor[{location}]: networkidle timed out")
        await utils.human_delay(self.config, scale=0.5)

        # An unexpected redirect usually means the session died.
        current = (page.url or "").lower()
        if any(hint in current for hint in ("login", "signin")):
            raise RuntimeError("redirected to the login page — session expired")

        detection = self.config.get("detection", {}) or {}
        text = await utils.page_text(page)

        unavailable = utils.contains_any(text, detection.get("unavailable_phrases", []))
        if unavailable:
            return False, f"matched {unavailable!r}"

        for probe in SLOT_PROBES:
            cells = await utils.visible_elements(page, probe)
            if cells:
                return True, f"{len(cells)} slot cell(s) via {probe}"

        positive = utils.contains_any(text, detection.get("available_phrases", []))
        if positive:
            return True, f"matched {positive!r}"

        # Neither signal fired — treat as no slots but keep the evidence.
        await utils.screenshot(page, f"availability-unclear-{location}")
        return False, "no availability signal on the page"

    # ------------------------------------------------------------------ #
    # Error handling
    # ------------------------------------------------------------------ #
    async def _location_error(
        self, page: Any, location: str, message: str, wait: float
    ) -> None:
        logger.error(f"monitor[{location}]: {message}")
        self.state.set_location_status(location, STATUS_ERROR, message, checked=True)
        self.state.log_event(location, f"error: {message}", status="error")
        shot = await utils.screenshot(page, f"monitor-error-{location}")
        await self.notifier.error(
            f"{location.title()} check failed: {message}",
            retry_info=f"retrying in {int(wait)}s",
        )
        if shot:
            await self.notifier.send_photo(shot, f"Monitor error — {location.title()}")
        await utils.interruptible_sleep(wait, self.state)

    async def _handle_portal_block(self, exc: Exception) -> None:
        """403 / blocked: wait out the cooldown, then log in again from scratch.

        Never stops the bot and never asks for a human. The stored session is
        discarded so the next attempt is a clean login rather than a replay of
        cookies the portal may already have invalidated.
        """
        wait_minutes = self.block_cooldown_minutes
        logger.error(f"monitor: portal blocked — {exc}")
        logger.warning(
            f"monitor: sleeping {wait_minutes:.0f} min, then logging in from scratch"
        )
        self.state.log_event(
            "bot", f"portal blocked — retrying in {wait_minutes:.0f} min", status="error"
        )
        for location in self.locations:
            self.state.set_location_status(
                location, STATUS_PAUSED, f"blocked — retry in {wait_minutes:.0f} min"
            )

        # Only alert the first time, so a long block does not spam Telegram.
        if not self._block_alerted:
            self._block_alerted = True
            await self.notifier.error(
                f"Portal is blocking us (HTTP 403). The bot will keep running and "
                f"retry automatically in {wait_minutes:.0f} minutes.",
                retry_info="no action needed — this is handled automatically",
            )

        await self._reset_session()
        await utils.interruptible_sleep(wait_minutes * 60, self.state)

    async def _handle_login_failure(self, exc: Exception) -> None:
        """Login failed for a non-block reason: back off, then try again.

        Backoff grows with consecutive failures so a persistent problem (wrong
        password, portal change) does not hammer the site, while a transient one
        recovers quickly.
        """
        self._login_failures += 1
        schedule = [2, 5, 10, 20, 30]
        idx = min(self._login_failures - 1, len(schedule) - 1)
        wait_minutes = schedule[idx]

        logger.error(
            f"monitor: login failed ({self._login_failures} in a row): {exc}"
        )
        logger.warning(f"monitor: retrying login in {wait_minutes} min")
        self.state.log_event(
            "bot",
            f"login failed x{self._login_failures} — retry in {wait_minutes} min",
            status="error",
        )
        for location in self.locations:
            self.state.set_location_status(
                location, STATUS_ERROR, f"login failed — retry in {wait_minutes} min"
            )

        # A stale session is a common cause; drop it after the first failure.
        if self._login_failures >= 1:
            await self._reset_session()

        # Beyond this many consecutive failures it is no longer a blip — wrong
        # credentials, a changed portal, something the bot cannot fix by waiting.
        # Escalate to a real pause so the manager is told, instead of retrying
        # quietly forever.
        if self._login_failures >= self.critical_login_failures:
            logger.critical(
                f"monitor: {self._login_failures} consecutive login failures — "
                "escalating to a manual pause"
            )
            self.state.set_manual_pause(
                f"CRITICAL: login failing repeatedly ({self._login_failures}x)"
            )
            self.state.log_event("bot", "CRITICAL: login failing repeatedly", status="error")
            await self.notifier.manual_required(
                f"CRITICAL: login has failed {self._login_failures} times in a row.\n\n"
                f"Last error: {exc}\n\n"
                "The bot cannot fix this by waiting. Likely causes: wrong "
                "password, account locked, or the portal changed. Check, then "
                "send /resume to continue."
            )
            await self.notifier.wait_for_resume()
            self.state.clear_manual_pause()
            self._login_failures = 0
            return

        if self._login_failures in (1, 3, 6):
            await self.notifier.error(
                f"Login failed {self._login_failures} time(s) in a row: {exc}",
                retry_info=f"retrying automatically in {wait_minutes} min",
            )

        await utils.interruptible_sleep(wait_minutes * 60, self.state)

    async def _relogin_and_clear_gate(self) -> Any | None:
        """Rebuild the session and clear the verification gate.

        Returns the fresh page, or None when the cycle has to be abandoned.
        """
        await self._reset_session()
        try:
            page = await self.auth.ensure_logged_in(force=True)
        except PortalUnreachableError:
            raise
        except Exception as exc:
            logger.error(f"monitor: could not log in again: {exc}")
            return None
        self.install_navigation_watch(page)
        self._needs_reverify = False
        if not await self.ensure_visa_type_verified(page):
            logger.error("monitor: could not clear the gate after re-login")
            return None
        return page

    async def _reset_session(self) -> None:
        """Drop cookies and the browser so the next attempt is a clean login.

        ORDER MATTERS. Closing the browser used to save the session on the way
        out, which silently rewrote the file that had just been deleted; the
        next start restored those cookies, landed already authenticated on the
        verification page where there is no email field, and failed the login
        twice (run 20261013_155949). Close WITHOUT saving, then delete.
        """
        try:
            await self.auth.stop(save=False)
            logger.info("monitor: browser closed without saving the session")
        except Exception as exc:
            logger.debug(f"monitor: could not close the browser: {exc}")
        try:
            self.auth._clear_saved_session()
            logger.info("monitor: stored cookies cleared — next login is clean")
        except Exception as exc:
            logger.debug(f"monitor: could not clear the stored session: {exc}")

    async def _handle_cycle_error(self, exc: Exception) -> None:
        self._consecutive_errors += 1
        message = f"{type(exc).__name__}: {exc}"
        logger.exception(f"monitor: cycle failed ({self._consecutive_errors}): {message}")
        self.state.log_event("bot", f"cycle error: {message}", status="error")

        is_network = any(
            hint in message.lower()
            for hint in ("net::", "econn", "dns", "timeout", "socket", "connection")
        )
        wait = self.network_error_wait if is_network else self.page_error_wait

        await self.notifier.error(message, retry_info=f"retrying in {int(wait)}s")

        # Repeated failures usually mean the session is gone — force a re-login.
        if self._consecutive_errors >= self.max_attempts:
            logger.warning("monitor: too many consecutive errors — forcing a fresh login")
            self._consecutive_errors = 0
            try:
                await self.auth.refresh_session()
            except Exception as refresh_exc:
                logger.error(f"monitor: forced re-login failed: {refresh_exc}")

        await utils.interruptible_sleep(wait, self.state)

    # ------------------------------------------------------------------ #
    # Idle / paused states
    # ------------------------------------------------------------------ #
    async def _idle(self) -> None:
        """Monitoring is switched off (dashboard Stop) — wait to be restarted."""
        for location in self.locations:
            self.state.set_location_status(location, STATUS_PAUSED, "monitoring stopped")
        await utils.interruptible_sleep(2, self.state, step=0.5)

    async def _await_manual_resume(self) -> None:
        """Hold until the user sends /resume (or presses Start on the dashboard).

        Must always consume real time before returning. When Telegram is not
        configured ``wait_for_resume`` returns False instantly, and the caller
        re-enters this method immediately — without the sleep below that becomes
        a 100% CPU busy-loop that also floods the log.
        """
        reason = self.state.pause_reason or "manual step"
        if reason != self._last_pause_reason:
            logger.info(f"monitor: paused — {reason}")
            self._last_pause_reason = reason

        resumed = await self.notifier.wait_for_resume(timeout=60)
        if resumed or not self.state.paused:
            self._last_pause_reason = None
            return

        logger.debug("monitor: still waiting for /resume")
        # Guarantees forward progress even when wait_for_resume cannot block.
        await utils.interruptible_sleep(15, self.state)
