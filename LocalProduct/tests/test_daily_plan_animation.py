"""Check the mining canvas against synthetic plan snapshots."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import tkinter as tk
import unittest
from unittest.mock import patch

from daily_plan_animation import ARMOR_COLORS, DailyPlanAnimation, PICKAXES


class DailyPlanAnimationTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f'Tk unavailable: {error}')
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.scene = DailyPlanAnimation(self.root)
        self.scene.pack()
        self.root.update_idletasks()

    def test_ten_weighted_sections_fill_in_task_order(self):
        tasks = tuple((index, [10, 15, 20][index % 3], index in (1, 8)) for index in range(10))
        self.scene.sync(tasks, 20)
        slots = [self.scene.coords(item) for item in self.scene.find_withtag('cabinet-slot')]
        self.assertEqual(len(slots), 10)
        first, second, third = [right - left + 1 for left, _, right, _ in slots[:3]]
        self.assertAlmostEqual(second / first, 1.5)
        self.assertAlmostEqual(third / first, 2.0)
        centers = [(slots[index][0] + slots[index][2] + 1) / 2 for index in (1, 8)]
        self.assertEqual(self.diamond_centers('filled-diamond'), [round(center) for center in centers])

        all_done = tuple((index, weight, True) for index, weight, _ in tasks)
        self.scene.sync(all_done, 30)
        self.assertEqual(len(self.scene.find_withtag('filled-diamond')), 10)
        self.assertFalse(self.scene.find_withtag('ore'))

    def test_cracks_and_diamond_flight_for_out_of_order_completion(self):
        tasks = ((0, 10, True), (1, 20, False), (2, 15, True))
        with patch('daily_plan_animation.time.monotonic', return_value=20.0) as clock:
            self.scene.celebrate(tasks, 16, 2)
            self.assertEqual(len(self.scene.find_withtag('filled-diamond')), 1)
            self.assertTrue(self.scene.find_withtag('ore'))
            timer = self.scene._after_id
            self.scene.sync(tasks, 16)
            self.assertEqual(self.scene._after_id, timer)

            self.scene._draw(0.30)
            self.assertTrue(self.scene.find_withtag('ore-cracks'))
            self.scene._draw(0.86)
            self.assertFalse(self.scene.find_withtag('ore'))
            self.assertEqual(len(self.scene.find_withtag('flying-diamond')), 1)
            final_slot = self.scene.coords(self.scene.find_withtag('cabinet-slot')[2])
            self.assertGreater(self.diamond_centers('flying-diamond')[0], 300)
            self.assertLess(self.diamond_centers('flying-diamond')[0], final_slot[2])

            self.scene.after_cancel(timer)
            clock.return_value += 2
            self.scene._tick()
            self.assertFalse(self.scene.find_withtag('flying-diamond'))
            self.assertEqual(len(self.scene.find_withtag('filled-diamond')), 2)
            self.assertIsNone(self.scene._after_id)

    def test_all_milestones_and_rebirth_equipment(self):
        for points, armor, pickaxe, enchanted in (
            (0, None, 'iron', False),
            (10, 'leather', 'iron', False),
            (75, 'copper', 'iron', False),
            (150, 'iron', 'iron', True),
            (240, 'gold', 'diamond', False),
            (360, 'diamond', 'diamond', False),
            (480, 'diamond', 'netherite', False),
            (600, 'netherite', 'netherite', True),
            (0, None, 'iron', False),
        ):
            with self.subTest(points=points):
                self.scene.sync((), points)
                armor_parts = self.scene.find_withtag('armor')
                if armor is None:
                    self.assertFalse(armor_parts)
                else:
                    self.assertGreater(len(armor_parts), 15)
                    fills = {self.scene.itemcget(item, 'fill') for item in armor_parts}
                    self.assertEqual(fills, set(ARMOR_COLORS[armor]))
                head = self.scene.find_withtag('pickaxe-head')[0]
                self.assertEqual(self.scene.itemcget(head, 'fill'), PICKAXES[pickaxe][2])
                self.assertEqual(bool(self.scene.find_withtag('pickaxe-glint')), enchanted)
                self.assertEqual(bool(self.scene.find_withtag('armor-trim')), points == 600)

    def test_reset_to_next_day_cancels_animation_and_clears_cabinet(self):
        self.scene.celebrate(((0, 15, True),), 30, 0)
        self.assertIsNotNone(self.scene._after_id)
        self.scene.sync((), 30)
        self.assertIsNone(self.scene._after_id)
        self.assertFalse(self.scene.find_withtag('filled-diamond'))
        self.assertFalse(self.scene.find_withtag('cabinet-slot'))

    def diamond_centers(self, tag):
        centers = []
        for item in self.scene.find_withtag(tag):
            points = self.scene.coords(item)
            centers.append((points[0] + points[6]) / 2)
        return centers


if __name__ == '__main__':
    unittest.main()
