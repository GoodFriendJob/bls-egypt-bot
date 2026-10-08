"""Read the CAPTCHA tile digits with GPT-4o vision.

The BLS CAPTCHA renders each tile's number as a base64 image with no text, alt
or data attribute anywhere in the DOM, and the images are unique on every load —
so neither DOM scraping nor a hash lookup table can work. Reading the pixels is
the only route to solving it automatically.

The solver is optional: with no ``openai.api_key`` configured every call returns
None and the caller falls back to a manual solve over Telegram.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any

from loguru import logger

try:
    from openai import AsyncOpenAI

    OPENAI_AVAILABLE = True
except ImportError:  # pragma: no cover - dependency not installed
    AsyncOpenAI = None  # type: ignore
    OPENAI_AVAILABLE = False

SAMPLE_DIR = Path("logs/captcha_samples")

PROMPT_TEMPLATE = (
    "This is a CAPTCHA grid. The task says to select all boxes showing the "
    "number {target}.\n"
    "Look at the {rows}x{cols} grid of images. Each box shows a {digits}-digit number.\n"
    "Tell me which grid positions (1-{count}, left-to-right top-to-bottom) show "
    "the number {target}.\n"
    "Reply with ONLY a comma-separated list of position numbers, e.g: 1,4,7\n"
    "If none match, reply: none"
)

_POSITION_RE = re.compile(r"\d+")


class VisionSolver:
    """Thin wrapper around the OpenAI vision endpoint."""

    def __init__(self, config: dict[str, Any]) -> None:
        cfg = config.get("openai", {}) or {}
        self.api_key = (cfg.get("api_key") or "").strip()
        self.model = cfg.get("model") or "gpt-4o"
        self.timeout = float(cfg.get("timeout", 45))
        self._client: Any = None

        if not OPENAI_AVAILABLE:
            logger.warning("vision: the 'openai' package is not installed — disabled")
        elif not self.api_key:
            logger.warning("vision: openai.api_key is empty — automatic solving disabled")

    @property
    def enabled(self) -> bool:
        return bool(OPENAI_AVAILABLE and self.api_key)

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = AsyncOpenAI(api_key=self.api_key, timeout=self.timeout)
        return self._client

    # ------------------------------------------------------------------ #
    # Main entry point
    # ------------------------------------------------------------------ #
    async def find_matching_positions(
        self,
        image_bytes: bytes,
        target: str,
        *,
        count: int,
        rows: int = 3,
        cols: int = 3,
    ) -> list[int] | None:
        """Return 1-based grid positions showing ``target``.

        ``[]`` means the model looked and found none — a real answer, distinct
        from ``None``, which means the lookup could not be performed at all
        (disabled, network error, unparseable reply) and the caller should fall
        back to a human.
        """
        if not self.enabled:
            return None
        if not image_bytes:
            logger.warning("vision: empty screenshot — cannot ask the model")
            return None

        prompt = PROMPT_TEMPLATE.format(
            target=target,
            rows=rows,
            cols=cols,
            count=count,
            digits=len(str(target)),
        )
        encoded = base64.b64encode(image_bytes).decode("ascii")

        try:
            response = await self._get_client().chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{encoded}",
                                    # The digits are small and deliberately
                                    # distorted; low detail loses them.
                                    "detail": "high",
                                },
                            },
                        ],
                    }
                ],
                max_tokens=50,
                temperature=0,  # deterministic: this is a reading task
            )
        except Exception as exc:
            logger.error(f"vision: API call failed: {type(exc).__name__}: {exc}")
            return None

        try:
            reply = (response.choices[0].message.content or "").strip()
        except (AttributeError, IndexError) as exc:
            logger.error(f"vision: unexpected response shape: {exc}")
            return None

        logger.info(f"vision: model replied {reply!r}")
        return self._parse_positions(reply, count)

    # ------------------------------------------------------------------ #
    # Parsing
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_positions(reply: str, count: int) -> list[int] | None:
        """Parse "1,4,7" / "none" into validated 1-based positions."""
        lowered = reply.strip().lower()
        if not lowered:
            return None
        if "none" in lowered and not _POSITION_RE.search(lowered):
            return []

        found = [int(n) for n in _POSITION_RE.findall(lowered)]
        if not found:
            return None

        valid = sorted({n for n in found if 1 <= n <= count})
        dropped = [n for n in found if not (1 <= n <= count)]
        if dropped:
            logger.warning(
                f"vision: ignoring out-of-range position(s) {dropped} "
                f"(grid holds {count} tiles)"
            )
        if not valid:
            logger.warning("vision: no usable positions in the reply")
            return None
        return valid


def save_sample(image_bytes: bytes, label: str) -> str | None:
    """Persist a captcha grid screenshot for auditing. Returns the path."""
    if not image_bytes:
        return None
    try:
        SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)[:80] or "sample"
        path = SAMPLE_DIR / f"{safe}.png"
        path.write_bytes(image_bytes)
        logger.info(f"captcha sample saved: {path}")
        return str(path)
    except OSError as exc:
        logger.warning(f"vision: could not save sample: {exc}")
        return None
