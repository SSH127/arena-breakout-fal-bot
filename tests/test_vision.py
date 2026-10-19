from __future__ import annotations

import unittest
from pathlib import Path

import cv2
import numpy as np

from falbot import vision

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
TEMPLATE = cv2.imread(str(ROOT / "templates" / "handguard_cell.png"), cv2.IMREAD_GRAYSCALE)

WINDOW_W, WINDOW_H = 2558, 1598


def window_with_pane(name: str) -> np.ndarray:
    """Paste a stored pane screenshot where the game draws it."""
    pane = cv2.imread(str(FIXTURES / name))
    assert pane is not None, name
    canvas = np.zeros((WINDOW_H, WINDOW_W, 3), dtype=np.uint8)
    x1, y1, x2, y2 = vision.pane_box(canvas)
    h = min(pane.shape[0], y2 - y1)
    w = min(pane.shape[1], x2 - x1)
    canvas[y1 : y1 + h, x1 : x1 + w] = pane[:h, :w]
    return canvas


class FindCellsTests(unittest.TestCase):
    def test_finds_every_handguard_in_the_pane(self) -> None:
        boxes, scale = vision.find_cells(window_with_pane("pane_with_handguards.png"), TEMPLATE)

        self.assertEqual(len(boxes), 8)
        self.assertIsNotNone(scale)

    def test_counts_a_full_page_of_handguards(self) -> None:
        boxes, _ = vision.find_cells(window_with_pane("pane_all_handguards.png"), TEMPLATE)

        # 5 columns x 12 rows on screen; a few edge rows may be clipped.
        self.assertGreaterEqual(len(boxes), 50)

    def test_other_items_are_not_handguards(self) -> None:
        boxes, _ = vision.find_cells(window_with_pane("pane_without_handguards.png"), TEMPLATE)

        self.assertEqual(boxes, [])

    def test_scale_hint_is_reused(self) -> None:
        image = window_with_pane("pane_with_handguards.png")
        _, scale = vision.find_cells(image, TEMPLATE)
        boxes, again = vision.find_cells(image, TEMPLATE, scale_hint=scale)

        self.assertEqual(len(boxes), 8)
        self.assertEqual(again, scale)

    def test_boxes_are_inside_the_window_and_sorted(self) -> None:
        boxes, _ = vision.find_cells(window_with_pane("pane_all_handguards.png"), TEMPLATE)

        x1, y1, x2, y2 = vision.pane_box(np.zeros((WINDOW_H, WINDOW_W, 3), dtype=np.uint8))
        for x, y, w, h in boxes:
            self.assertGreaterEqual(x, x1)
            self.assertGreaterEqual(y, y1)
            self.assertLessEqual(x + w, x2)
            self.assertLessEqual(y + h, y2)
        rows = [round(box[1] / 40) for box in boxes]
        self.assertEqual(rows, sorted(rows))


class ScrollTests(unittest.TestCase):
    def setUp(self) -> None:
        image = window_with_pane("pane_without_handguards.png")
        self.gray = vision.pane_gray(image)

    def test_measures_how_far_the_list_moved(self) -> None:
        for dy in (60, 170, 300):
            after = np.zeros_like(self.gray)
            after[dy:] = self.gray[:-dy]
            self.assertEqual(vision.measure_scroll_shift(self.gray, after), dy)

    def test_measures_upward_scroll_as_negative(self) -> None:
        after = np.zeros_like(self.gray)
        after[:-120] = self.gray[120:]

        self.assertEqual(vision.measure_scroll_shift(self.gray, after), -120)

    def test_identical_views_mean_the_end_of_the_list(self) -> None:
        self.assertTrue(vision.same_view(self.gray, self.gray.copy()))

    def test_moved_views_are_not_the_same(self) -> None:
        after = np.zeros_like(self.gray)
        after[170:] = self.gray[:-170]

        self.assertFalse(vision.same_view(self.gray, after))

    def test_grid_of_identical_icons_is_ambiguous(self) -> None:
        rows = np.zeros((900, 400), dtype=np.uint8)
        rows[::85, :] = 200  # perfectly periodic stripes: one row looks like any other
        rows[1::85, :] = 200
        after = np.zeros_like(rows)
        after[85:] = rows[:-85]

        self.assertIsNone(vision.measure_scroll_shift(rows, after))


if __name__ == "__main__":
    unittest.main()
