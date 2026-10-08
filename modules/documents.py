"""Document upload automation.

Standard ``<input type="file">`` fields are driven with ``set_input_files``.
Custom drop-zone widgets get a synthetic drag-and-drop via JS injection. If
neither works the bot pauses and asks the user to finish the upload by hand.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from modules import utils

# Hints used to match a config document key to the right upload control.
DOCUMENT_HINTS: dict[str, tuple[str, ...]] = {
    "passport_scan": ("passport", "جواز"),
    "photo": ("photo", "picture", "image", "صورة"),
    "bank_statement": ("bank", "statement", "financial"),
    "travel_insurance": ("insurance", "travel", "تأمين"),
    "itinerary": ("itinerary", "ticket", "flight", "booking"),
    "hotel": ("hotel", "accommodation", "reservation"),
}

# JS shim: build a DataTransfer and fire dragenter/dragover/drop at a drop zone.
_DROP_JS = """
(element, payload) => {
  const binary = atob(payload.base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  const file = new File([bytes], payload.name, {type: payload.mime});
  const dt = new DataTransfer();
  dt.items.add(file);
  for (const type of ['dragenter', 'dragover', 'drop']) {
    const evt = new DragEvent(type, {bubbles: true, cancelable: true, dataTransfer: dt});
    element.dispatchEvent(evt);
  }
  // Some widgets only listen on a nested hidden file input.
  const inner = element.querySelector('input[type="file"]');
  if (inner) {
    inner.files = dt.files;
    inner.dispatchEvent(new Event('change', {bubbles: true}));
  }
  return true;
}
"""

MIME_TYPES = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

DROP_ZONE_SELECTORS = (
    "[class*='dropzone']",
    "[class*='drop-zone']",
    "[class*='upload']",
    "[id*='upload']",
    "[data-dz-message]",
)


async def upload_documents(
    page: Any,
    config: dict[str, Any],
    *,
    state: Any = None,
    notifier: Any = None,
) -> dict[str, bool]:
    """Upload every configured document. Returns ``{key: succeeded}``.

    TODO(unconfirmed — needs live portal inspection): the BLS upload step has not
    been seen yet. Confirm whether documents are uploaded on the appointment form
    itself or on a separate page, how many inputs there are, and what the labels
    say, then tighten ``DOCUMENT_HINTS`` and drop the generic fallbacks.
    """
    configured = (config.get("applicant") or {}).get("documents") or {}
    results: dict[str, bool] = {}
    if not configured:
        logger.info("documents: nothing configured to upload")
        return results

    file_inputs = await _file_inputs(page)
    logger.info(f"documents: {len(file_inputs)} file input(s) visible on the page")
    used: list[Any] = []

    for key, rel_path in configured.items():
        if not rel_path:
            continue
        path = Path(rel_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists():
            logger.error(f"documents: {key} missing at {path}")
            _log(state, f"document not found: {rel_path}", status="error")
            results[key] = False
            continue

        ok = await _upload_one(page, key, path, file_inputs, used, config)
        results[key] = ok
        _log(
            state,
            f"{'uploaded' if ok else 'could not upload'} {key} ({path.name})",
            status="ok" if ok else "warn",
        )
        await utils.human_delay(config)

    failed = [key for key, ok in results.items() if not ok]
    if failed and notifier is not None:
        shot = await utils.screenshot(page, "upload-manual-required")
        if state is not None:
            state.set_manual_pause(f"manual upload needed: {', '.join(failed)}")
        await notifier.manual_required(
            "Could not upload automatically: "
            + ", ".join(failed)
            + ". Attach the files in the browser, then send /resume.",
            screenshot=shot,
        )
        await notifier.wait_for_resume()
        if state is not None:
            state.clear_manual_pause()

    return results


async def _upload_one(
    page: Any,
    key: str,
    path: Path,
    file_inputs: list[Any],
    used: list[Any],
    config: dict[str, Any],
) -> bool:
    """Try the matching input, then any free input, then a drop zone."""
    target = await _match_input(key, file_inputs, used)
    if target is None:
        target = next((inp for inp in file_inputs if not any(inp is u for u in used)), None)

    if target is not None:
        try:
            await target.set_input_files(str(path))
            used.append(target)
            logger.info(f"documents: {key} set via file input")
            return True
        except Exception as exc:
            logger.warning(f"documents: set_input_files failed for {key}: {exc}")

    if await _drop_file(page, key, path):
        return True

    logger.warning(f"documents: no upload mechanism worked for {key}")
    return False


async def _file_inputs(page: Any) -> list[Any]:
    """All file inputs, including the visually hidden ones.

    ``set_input_files`` works on hidden inputs, so unlike text fields these are
    not filtered by visibility — custom widgets routinely hide the real input.
    """
    try:
        return await page.query_selector_all('input[type="file"]')
    except Exception as exc:
        logger.debug(f"documents: could not query file inputs: {exc}")
        return []


async def _match_input(key: str, file_inputs: list[Any], used: list[Any]) -> Any | None:
    hints = DOCUMENT_HINTS.get(key, (key.replace("_", " "),))
    for element in file_inputs:
        if any(element is other for other in used):
            continue
        haystack = await _context_text(element)
        if any(hint.lower() in haystack for hint in hints):
            return element
    return None


async def _context_text(element: Any) -> str:
    """Nearby text/attributes for an upload control, lower-cased."""
    try:
        return (
            await element.evaluate(
                """
                (el) => {
                  const bits = [el.name || '', el.id || '', el.accept || ''];
                  if (el.id) {
                    const lab = document.querySelector(`label[for="${el.id}"]`);
                    if (lab) bits.push(lab.innerText || '');
                  }
                  const wrap = el.closest('label, .form-group, .upload, div, td, tr');
                  if (wrap) bits.push((wrap.innerText || '').slice(0, 200));
                  return bits.join(' ');
                }
                """
            )
            or ""
        ).lower()
    except Exception:
        return ""


async def _drop_file(page: Any, key: str, path: Path) -> bool:
    """Last resort: synthesise a drag-and-drop onto a custom drop zone."""
    import base64

    zone = await utils.first_visible(page, list(DROP_ZONE_SELECTORS))
    if zone is None:
        return False

    try:
        payload = {
            "base64": base64.b64encode(path.read_bytes()).decode("ascii"),
            "name": path.name,
            "mime": MIME_TYPES.get(path.suffix.lower(), "application/octet-stream"),
        }
        await zone.evaluate(_DROP_JS, payload)
        logger.info(f"documents: {key} delivered via synthetic drag-and-drop")
        return True
    except Exception as exc:
        logger.warning(f"documents: drag-and-drop failed for {key}: {exc}")
        return False


def _log(state: Any, message: str, *, status: str = "info") -> None:
    logger.info(f"documents: {message}")
    if state is not None:
        state.log_event("documents", message, status=status)
