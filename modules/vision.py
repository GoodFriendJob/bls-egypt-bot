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
import io
import json
import re
from pathlib import Path
from typing import Any

from loguru import logger

# Tiles are ~110px square with ~40px digits; 3x gives the model far more to read.
UPSCALE_FACTOR = 3

try:
    from openai import AsyncOpenAI

    OPENAI_AVAILABLE = True
except ImportError:  # pragma: no cover - dependency not installed
    AsyncOpenAI = None  # type: ignore
    OPENAI_AVAILABLE = False

SAMPLE_DIR = Path("logs/captcha_samples")

# Preferred strategy: ask the model to READ all nine tiles, then do the matching
# in Python. Pure OCR is an easier task than "OCR + compare + report positions",
# it makes every misread visible in the log, and the reply can be sanity-checked
# (exactly N numbers expected) instead of trusted blindly.
READ_PROMPT_TEMPLATE = (
    "This is a {rows}x{cols} grid of {count} tiles, each showing a number.\n"
    "Read the number in every tile and output them in order, "
    "left-to-right then top-to-bottom "
    "(position 1=top-left, {cols}=top-right, {count}=bottom-right).\n"
    "The numbers may be coloured, styled, crossed out, underlined or have "
    "decorative lines through them. Ignore all colour and decoration and read "
    "only the digit shapes. Look carefully at each tile.\n"
    "Output ONLY the {count} numbers separated by commas, nothing else.\n"
    "Example: 123,456,789,234,567,890,345,678,901"
)

PROMPT_TEMPLATE = (
    "This is a {rows}x{cols} image grid ({cols} columns, {rows} rows = {count} "
    "tiles total). Each tile contains a number. The tiles are numbered "
    "left-to-right, top-to-bottom: position 1=top-left, 2=top-center, "
    "3=top-right, 4=middle-left, 5=middle-center, 6=middle-right, "
    "7=bottom-left, 8=bottom-center, 9=bottom-right. "
    "The target number is {target}. "
    "Reply with ONLY the position numbers (1-{count}) that contain {target}, "
    "comma-separated. Example: 2,5,9\n"
    "If no tile shows {target}, reply: none"
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
    async def read_tiles_individually(
        self,
        image_bytes: bytes,
        *,
        count: int = 9,
        rows: int = 3,
        cols: int = 3,
    ) -> list[str] | None:
        """Read each tile from its own cropped image.

        Measured against human labels, reading the whole grid in one picture
        fails by losing track of *which* tile is which rather than by misreading
        digits: on a grid with six matching tiles the model returned five
        positions and only three were right. Cropping each tile and sending them
        as separate images removes the spatial bookkeeping from the task
        entirely — every image contains exactly one number.

        Still a single API call: the parts are sent together in one message.
        """
        if not self.enabled or not image_bytes:
            return None

        crops = split_grid(image_bytes, rows=rows, cols=cols)
        if len(crops) != count:
            logger.warning(
                f"vision: expected {count} tile crops, produced {len(crops)}"
            )
            return None

        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    f"You will see {count} separate images. Each is one CAPTCHA "
                    "tile containing a single number.\n"
                    "Read the number in each image, in the order given.\n"
                    "The digits may be coloured, styled, crossed out, underlined "
                    "or decorated. Ignore all colour and decoration and read only "
                    "the digit shapes.\n"
                    f"Output ONLY the {count} numbers separated by commas, "
                    "nothing else.\n"
                    "Example: 123,456,789,234,567,890,345,678,901"
                ),
            }
        ]
        for crop in crops:
            encoded = base64.b64encode(upscale_png(crop)).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{encoded}",
                        "detail": "high",
                    },
                }
            )

        try:
            response = await self._get_client().chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": content}],
                max_tokens=120,
                temperature=0,
            )
            reply = (response.choices[0].message.content or "").strip()
        except Exception as exc:
            logger.error(f"vision: per-tile call failed: {type(exc).__name__}: {exc}")
            return None

        logger.info(f"vision: per-tile reply {reply!r}")
        numbers = _POSITION_RE.findall(reply)
        if len(numbers) != count:
            logger.warning(
                f"vision: per-tile reply held {len(numbers)} numbers, expected {count}"
            )
            return None
        return numbers

    async def read_grid_numbers(
        self,
        image_bytes: bytes,
        *,
        count: int = 9,
        rows: int = 3,
        cols: int = 3,
    ) -> list[str] | None:
        """Read every tile's number, left-to-right then top-to-bottom.

        Returns exactly ``count`` strings, or None when the reply could not be
        used. Matching is then done in Python, which keeps the model's job to
        pure OCR and puts every digit it read into the log.
        """
        if not self.enabled or not image_bytes:
            return None

        prompt = READ_PROMPT_TEMPLATE.format(rows=rows, cols=cols, count=count)
        reply = await self._ask(prompt, image_bytes)
        if reply is None:
            return None

        numbers = _POSITION_RE.findall(reply)
        if len(numbers) != count:
            logger.warning(
                f"vision: expected {count} numbers but parsed {len(numbers)} "
                f"from {reply!r}"
            )
            return None
        return numbers

    async def _ask(self, prompt: str, image_bytes: bytes) -> str | None:
        """One vision round-trip. Returns the raw reply text."""
        payload = upscale_png(image_bytes)
        encoded = base64.b64encode(payload).decode("ascii")
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
                                    "detail": "high",
                                },
                            },
                        ],
                    }
                ],
                max_tokens=120,
                temperature=0,
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
        return reply

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
        reply = await self._ask(prompt, image_bytes)
        if reply is None:
            return None
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


def split_grid(image_bytes: bytes, *, rows: int = 3, cols: int = 3) -> list[bytes]:
    """Cut a grid screenshot into one PNG per tile, in reading order.

    A small inset is trimmed from each cell so neighbouring tiles' borders do
    not bleed into a crop and give the model a second number to look at.
    """
    try:
        from PIL import Image
    except ImportError:
        logger.warning("vision: Pillow not installed — cannot split the grid")
        return []

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img = img.convert("RGB")
            width, height = img.size
            cell_w = width / cols
            cell_h = height / rows
            inset_x = cell_w * 0.04
            inset_y = cell_h * 0.04

            crops: list[bytes] = []
            for row in range(rows):
                for col in range(cols):
                    box = (
                        int(col * cell_w + inset_x),
                        int(row * cell_h + inset_y),
                        int((col + 1) * cell_w - inset_x),
                        int((row + 1) * cell_h - inset_y),
                    )
                    buffer = io.BytesIO()
                    img.crop(box).save(buffer, format="PNG")
                    crops.append(buffer.getvalue())
            logger.info(
                f"vision: split {width}x{height} grid into {len(crops)} tiles "
                f"(~{int(cell_w)}x{int(cell_h)} each)"
            )
            return crops
    except Exception as exc:
        logger.warning(f"vision: grid split failed: {exc}")
        return []


def record_reading(
    sample: str,
    target: str,
    tiles: list[str] | None,
    positions: list[int] | None,
    method: str,
) -> None:
    """Append what the model read to readings.jsonl, for later grading.

    Pairs with ground_truth.json: tools/grade_captcha.py joins the two to
    measure accuracy and surface which digits get confused.
    """
    try:
        SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
        row = {
            "sample": Path(sample).name if sample else "",
            "target": target,
            "tiles": tiles,
            "positions": positions,
            "method": method,
        }
        with (SAMPLE_DIR / "readings.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
    except Exception as exc:
        logger.debug(f"vision: could not record reading: {exc}")


def upscale_png(image_bytes: bytes, factor: int = UPSCALE_FACTOR) -> bytes:
    """Enlarge the crop with LANCZOS before sending it to the model.

    The tiles are roughly 110px square with ~40px digits, deliberately styled to
    resist reading. Resampling up gives the model materially more pixels to work
    with. Returns the original bytes unchanged if Pillow is unavailable or the
    resize fails — a slightly worse image beats no lookup at all.
    """
    if factor <= 1 or not image_bytes:
        return image_bytes
    try:
        from PIL import Image
    except ImportError:
        logger.warning("vision: Pillow not installed — sending the crop unscaled")
        return image_bytes

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img = img.convert("RGB")
            width, height = img.size
            enlarged = img.resize(
                (width * factor, height * factor), Image.Resampling.LANCZOS
            )
            buffer = io.BytesIO()
            enlarged.save(buffer, format="PNG")
            logger.info(
                f"vision: upscaled grid {width}x{height} -> "
                f"{width * factor}x{height * factor} (x{factor}, LANCZOS)"
            )
            return buffer.getvalue()
    except Exception as exc:
        logger.warning(f"vision: upscale failed ({exc}) — sending the original")
        return image_bytes


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
