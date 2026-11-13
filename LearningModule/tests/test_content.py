from fractions import Fraction
import re
import unittest

from learning_module.content import check_answer, hint, list_presets, make_task, practice_task, select_preset
from learning_module.fractions import MathInputError


class FractionContentTests(unittest.TestCase):
    def test_presets_cover_same_and_unlike_and_are_independent_objects(self):
        presets = list_presets()
        self.assertEqual(len({row["id"] for row in presets}), 4)
        self.assertEqual({select_preset(row["id"])["grade_hint"] for row in presets}, {4, 5})
        changed = select_preset("g4_same_1")
        changed["left"] = "0/4"
        self.assertEqual(select_preset("g4_same_1")["left"], "1/4")
        self.assertEqual(select_preset("g4_same_1")["teacher_method_status"], "unknown")

    def test_preserves_given_denominators_for_teaching(self):
        task = make_task("2/4", "1/4")
        self.assertEqual(task["left"], "2/4")
        self.assertEqual(task["skill_id"], "fraction.add.same_denominator")
        self.assertIn("2/4", task["prompt_ru"])

    def test_same_denominator_and_unlike_exact_answers(self):
        for left, right, answer in (("1/4", "2/4", "3/4"), ("1/2", "1/3", "5/6"), ("2/3", "3/4", "17/12"), ("0/3", "0/3", "0")):
            with self.subTest(left=left, right=right):
                result = check_answer(make_task(left, right), answer)
                self.assertEqual(result["state"], "verified")
                self.assertTrue(result["correct"])
                self.assertEqual(result["checked_claims"], ["answer_equivalence", "simplest_form"])

    def test_form_and_value_are_independent(self):
        task = make_task("1/6", "2/6")
        result = check_answer(task, "3/6")
        self.assertTrue(result["correct_value"])
        self.assertFalse(result["correct_form"])
        self.assertFalse(result["correct"])
        self.assertEqual(result["code"], "equivalent_not_simplest")
        self.assertTrue(check_answer(task, "1/2")["correct"])
        self.assertTrue(check_answer(make_task("1/6", "2/6", False), "3/6")["correct"])

    def test_negative_denominator_requires_canonical_form(self):
        result = check_answer(make_task("1/6", "2/6"), "-1/-2")
        self.assertTrue(result["correct_value"])
        self.assertFalse(result["correct_form"])

    def test_invalid_syntax_is_not_a_mathematical_wrong_answer(self):
        for text in ("5/0", "1 1/6", "1/2+1/3"):
            with self.subTest(text=text):
                result = check_answer(select_preset("g5_unlike_1"), text)
                self.assertEqual(result["state"], "invalid")
                self.assertIsNone(result["correct_value"])
                self.assertIsNone(result["correct_form"])
                self.assertEqual(result["checked_claims"], [])

    def test_common_misconception_feedback_does_not_reveal_answer(self):
        result = check_answer(make_task("1/2", "1/3"), "2/5")
        self.assertFalse(result["correct"])
        self.assertIn("знаменатели", result["text_ru"])
        self.assertNotIn("5/6", result["text_ru"])

    def test_task_bounds_and_unsupported_types(self):
        for left, right in (("1", "1/2"), ("-1/2", "1/3"), ("1/-2", "1/3"), ("1/0", "1/3"), ("word problem", "1/3"), ("1/999983", "1/999979")):
            with self.subTest(left=left, right=right), self.assertRaises(MathInputError):
                make_task(left, right)
        with self.assertRaises(MathInputError):
            make_task("1/2", "1/3", 1)
        with self.assertRaises(MathInputError):
            select_preset("unknown")

    def test_progressive_hints_and_exact_visual_model(self):
        task = make_task("1/2", "1/3")
        self.assertIsNone(hint(task, 1)["visual"])
        visual = hint(task, 2)["visual"]
        self.assertEqual(visual, {"denominator": 6, "left_parts": 3, "right_parts": 2, "total_parts": 5})
        self.assertNotIn("5/6", hint(task, 1)["text_ru"])
        self.assertIn("другой пример", hint(task, 3)["text_ru"])
        self.assertIn("5/6", hint(task, 4)["text_ru"])
        self.assertEqual(hint(task, 100)["level"], 4)

    def test_large_visual_model_is_omitted_not_allocated(self):
        self.assertIsNone(hint(make_task("1/25", "1/25"), 2)["visual"])

    def test_bad_hint_level_is_rejected(self):
        for level in (0, -1, True, "1", None):
            with self.subTest(level=level), self.assertRaises(MathInputError):
                hint(select_preset("g4_same_1"), level)

    def test_practice_has_different_values_and_target_for_each_preset(self):
        for preset in list_presets():
            original = select_preset(preset["id"])
            practice = practice_task(original)
            with self.subTest(preset=preset["id"]):
                self.assertNotEqual(practice["task_id"], original["task_id"])
                original_values = sorted((Fraction(original["left"]), Fraction(original["right"])))
                practice_values = sorted((Fraction(practice["left"]), Fraction(practice["right"])))
                self.assertNotEqual(original_values, practice_values)
                self.assertNotEqual(sum(original_values), sum(practice_values))
                self.assertTrue(check_answer(practice, str(sum(practice_values)))["correct"])
                self.assertEqual(practice_task(original), practice)

    def test_small_denominator_practice_stays_proper(self):
        for task in (make_task("0", "0"), make_task("1/2", "1/2"), make_task("0", "1/3")):
            practice = practice_task(task)
            self.assertTrue(0 <= Fraction(practice["left"]) < 1)
            self.assertTrue(0 <= Fraction(practice["right"]) < 1)

    def test_worked_hint_is_not_reused_as_independent_practice(self):
        tasks = [select_preset(row["id"]) for row in list_presets()]
        tasks.extend((make_task("0", "0"), make_task("1/2", "1/2"), make_task("0", "1/3")))
        for task in tasks:
            practice = practice_task(task)
            practice_values = sorted((Fraction(practice["left"]), Fraction(practice["right"])))
            for language in ("ru", "en"):
                with self.subTest(task=task["task_id"], language=language):
                    worked = hint(task, 3)["text_" + language]
                    operands = re.search(r"([0-9]+/[0-9]+) \+ ([0-9]+/[0-9]+) =", worked)
                    self.assertIsNotNone(operands)
                    worked_values = sorted((Fraction(operands[1]), Fraction(operands[2])))
                    self.assertNotEqual(worked_values, practice_values)
                    self.assertNotEqual(sum(worked_values), sum(practice_values))

    def test_invalid_task_cannot_be_checked(self):
        for task in ({}, None, {"left": "1/2", "right": "1/3", "require_simplest": "yes"}):
            with self.subTest(task=task), self.assertRaises(MathInputError):
                check_answer(task, "5/6")


if __name__ == "__main__":
    unittest.main()
