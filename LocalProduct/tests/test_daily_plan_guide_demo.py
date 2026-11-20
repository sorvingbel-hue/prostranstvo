"""The tutorial must teach approval-gated equipment using only demo widgets."""
import gc
from pathlib import Path
import sys
import tkinter as tk
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from daily_plan_guide import DailyPlanGuide


class DailyPlanGuideDemoTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.guide = DailyPlanGuide(self.root)
        self.guide.withdraw()

    def tearDown(self):
        try:
            self.guide.destroy()
            self.root.destroy()
        finally:
            self.guide = self.root = None
            gc.collect()

    def test_demo_checks_remain_provisional_until_demo_parent_approval(self):
        self.guide.next_button.invoke()
        self.guide.example_buttons['task_0'].invoke()
        self.guide.example_buttons['task_1'].invoke()
        self.assertEqual(self.guide.scene._points, 0)
        self.assertIn('ждёт проверки: 30', self.guide.points_label.cget('text'))
        self.guide.next_button.invoke()
        self.guide.example_buttons['submit'].invoke()
        self.guide.example_buttons['approve'].invoke()
        self.guide.back_button.invoke()
        self.assertEqual(self.guide.scene._points, 30)
        self.assertIn('подтверждено: 30', self.guide.points_label.cget('text'))

    def test_editing_demo_completion_requires_a_fresh_demo_review(self):
        self.guide.next_button.invoke()
        self.guide.example_buttons['task_0'].invoke()
        self.guide.example_buttons['task_1'].invoke()
        self.guide.next_button.invoke()
        self.guide.example_buttons['submit'].invoke()
        self.guide.example_buttons['approve'].invoke()
        self.guide.back_button.invoke()
        self.guide.example_buttons['task_0'].invoke()
        self.assertEqual(self.guide.example_review, 'open')
        self.assertEqual(self.guide.scene._points, 0)
        self.assertIn('ждёт проверки', self.guide.points_label.cget('text'))


if __name__ == '__main__':
    unittest.main()
