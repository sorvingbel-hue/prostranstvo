import unittest
from fractions import Fraction

from learning_module.fractions import MathInputError, parse_answer


class FractionParsingTests(unittest.TestCase):
    def test_equivalent_forms_preserve_input_and_canonical_value(self):
        result = parse_answer(" 2 / 4 ")
        self.assertEqual(result.value, Fraction(1, 2))
        self.assertEqual((result.numerator, result.denominator), (2, 4))
        self.assertEqual(result.normalized, "1/2")
        self.assertFalse(result.is_simplest)

    def test_integer_zero_and_negative(self):
        for text, value, simplified in (("0", 0, True), ("-3", -3, True), ("0/4", 0, False), ("- 1 / 2", Fraction(-1, 2), True)):
            with self.subTest(text=text):
                answer = parse_answer(text)
                self.assertEqual(answer.value, value)
                self.assertEqual(answer.is_simplest, simplified)

    def test_unicode_fraction_slash(self):
        self.assertEqual(parse_answer("3⁄4").value, Fraction(3, 4))

    def test_negative_denominator_is_exact_but_not_canonical_form(self):
        answer = parse_answer("-1/-2")
        self.assertEqual(answer.value, Fraction(1, 2))
        self.assertFalse(answer.is_simplest)
        self.assertEqual(answer.normalized, "1/2")

    def test_unsupported_expression_forms_are_not_evaluated(self):
        for text in ("1/2+1/3", "1 1/2", "0.5", "1e3", "2**3", "NaN", "__import__('os')", "½", "١/٢", "1\x00/2", "", " ", "--1/2"):
            with self.subTest(text=text), self.assertRaises(MathInputError) as error:
                parse_answer(text)
            self.assertEqual(error.exception.code, "invalid_number")

    def test_zero_denominator_has_stable_safe_error(self):
        with self.assertRaises(MathInputError) as error:
            parse_answer("987654/0")
        self.assertEqual(str(error.exception), "zero_denominator")
        self.assertNotIn("987654", str(error.exception))

    def test_bounds(self):
        self.assertEqual(parse_answer("1000000/1000000").value, 1)
        for text in ("1000001/2", "1/1000001", "-1000001"):
            with self.subTest(text=text), self.assertRaises(MathInputError) as error:
                parse_answer(text)
            self.assertEqual(error.exception.code, "number_out_of_range")
        with self.assertRaises(MathInputError) as error:
            parse_answer("0" * 81)
        self.assertEqual(error.exception.code, "answer_too_long")

    def test_non_string_input(self):
        for value in (None, 1, True, [], {}):
            with self.subTest(value=value), self.assertRaises(MathInputError):
                parse_answer(value)


if __name__ == "__main__":
    unittest.main()
