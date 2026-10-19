"""Pure image helpers for the right-hand storage pane.

Nothing in here touches the mouse, the game window, or OCR, so it can be unit
tested against screenshots.

The storage pane renders every item as a small icon with a tiny abbreviated
caption ("FAL标准护"). OCR cannot read those captions reliably, but the icon
itself is pixel-identical from cell to cell, so template matching finds every
copy instantly and without false positives.
"""

from __future__ import annotations

import cv2
import numpy as np

# Templates were cut from a 1600 px tall window.
TEMPLATE_REF_HEIGHT = 1600

# Right-hand storage pane as fractions of the window: (left, top, right, bottom).
# It sits at the same place on the trader page and on the market sell page. The
# toolbar above it and the "批量出售" bar below it are left out.
PANE = (0.575, 0.14, 0.94, 0.875)

MATCH_THRESHOLD = 0.80
_WIDE_SCALES = (1.0, 0.92, 1.08, 0.84, 1.16)


def pane_box(image: np.ndarray, region: tuple[float, float, float, float] = PANE) -> tuple[int, int, int, int]:
    height, width = image.shape[:2]
    return (
        int(width * region[0]),
        int(height * region[1]),
        int(width * region[2]),
        int(height * region[3]),
    )


def pane_gray(image: np.ndarray, region: tuple[float, float, float, float] = PANE) -> np.ndarray:
    x1, y1, x2, y2 = pane_box(image, region)
    return cv2.cvtColor(image[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)


def find_cells(
    image: np.ndarray,
    template: np.ndarray,
    *,
    scale_hint: float | None = None,
    threshold: float = MATCH_THRESHOLD,
    region: tuple[float, float, float, float] = PANE,
) -> tuple[list[tuple[float, float, float, float]], float | None]:
    """Find every copy of ``template`` (grayscale) inside the storage pane.

    Returns ``(boxes, scale)``. Boxes are ``(x, y, w, h)`` in full-image pixels,
    ordered top to bottom, left to right. ``scale`` is the template scale that
    matched; feed it back as ``scale_hint`` so later calls only try one scale.
    """
    if template is None or image is None:
        return [], scale_hint
    height = image.shape[0]
    x1, y1, _, _ = pane_box(image, region)
    gray = pane_gray(image, region)
    base = height / TEMPLATE_REF_HEIGHT

    def scan(scales: tuple[float, ...]):
        best = None
        for scale in scales:
            resized = template
            if abs(scale - 1.0) > 0.01:
                resized = cv2.resize(template, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            th, tw = resized.shape[:2]
            if th >= gray.shape[0] or tw >= gray.shape[1]:
                continue
            result = cv2.matchTemplate(gray, resized, cv2.TM_CCOEFF_NORMED)
            result = np.nan_to_num(result, nan=0.0, posinf=0.0, neginf=0.0)
            peak = float(result.max())
            if best is None or peak > best[0]:
                best = (peak, scale, result, tw, th)
        return best

    best = None
    if scale_hint is not None:
        best = scan((scale_hint,))
    if best is None or best[0] < threshold:
        best = scan(tuple(base * s for s in _WIDE_SCALES)) or best
    if best is None or best[0] < threshold:
        return [], scale_hint

    _, scale, result, tw, th = best
    boxes: list[tuple[float, float, float, float]] = []
    work = result.copy()
    while len(boxes) < 400:
        _, peak, _, loc = cv2.minMaxLoc(work)
        if peak < threshold:
            break
        x, y = loc
        boxes.append((float(x1 + x), float(y1 + y), float(tw), float(th)))
        work[max(0, y - th // 2) : y + th // 2 + 1, max(0, x - tw // 2) : x + tw // 2 + 1] = -1.0
    boxes.sort(key=lambda box: (round(box[1] / max(th * 0.5, 1)), box[0]))
    return boxes, scale


def small_signature(gray: np.ndarray, size: tuple[int, int] = (64, 96)) -> np.ndarray:
    return cv2.resize(gray, size, interpolation=cv2.INTER_AREA)


def same_view(before: np.ndarray, after: np.ndarray, tolerance: float = 1.2) -> bool:
    """True when two pane captures show the same thing (scrolling hit an end)."""
    if before.shape != after.shape:
        return False
    a = small_signature(before).astype(np.int16)
    b = small_signature(after).astype(np.int16)
    return float(np.abs(a - b).mean()) <= tolerance


def measure_scroll_shift(before: np.ndarray, after: np.ndarray) -> int | None:
    """How many pixels the content moved vertically between two captures.

    Positive means the content moved down (the list scrolled up). Returns
    ``None`` when the answer is ambiguous, which happens in a grid of identical
    icons: shifting by one row looks the same as shifting by two.
    """
    if before.shape != after.shape:
        return None
    height = before.shape[0]
    band_h = max(24, int(height * 0.16))
    for top in (0.42, 0.18, 0.64, 0.30, 0.52):
        y0 = int(height * top)
        if y0 + band_h > height:
            continue
        band = before[y0 : y0 + band_h]
        if float(band.std()) < 8.0:
            continue
        column = cv2.matchTemplate(after, band, cv2.TM_CCOEFF_NORMED)[:, 0]
        column = np.nan_to_num(column, nan=-1.0)
        best = int(column.argmax())
        score = float(column[best])
        if score < 0.93:
            continue
        masked = column.copy()
        masked[max(0, best - 6) : best + 7] = -1.0
        if float(masked.max()) > score - 0.03:
            continue
        return best - y0
    return None
