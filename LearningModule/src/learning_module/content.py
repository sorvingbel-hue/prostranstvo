"""Original limited fraction lessons, exact checks, and bounded local practice.

These explanations are a prototype, not a claim about a real teacher's method
or independently established learning efficacy. No school database is needed.
"""
from __future__ import annotations

from fractions import Fraction
from hashlib import sha256
from itertools import product
from math import lcm

from .fractions import MAX_NUMBER, MathInputError, ParsedAnswer, fraction_text, parse_answer

CONTENT_VERSION = "fractions-0.1.0"
CHECKER_VERSION = "exact_fraction_v1"
_SKILL_SAME = "fraction.add.same_denominator"
_SKILL_UNLIKE = "fraction.add.unlike_denominators"
_PRESETS = (
    ("g4_same_1", "1/4", "2/4", "Одинаковые доли: 1/4 + 2/4", "Equal-sized parts: 1/4 + 2/4"),
    ("g4_same_2", "1/6", "2/6", "Сложение и сокращение: 1/6 + 2/6", "Add and simplify: 1/6 + 2/6"),
    ("g5_unlike_1", "1/2", "1/3", "Разные доли: 1/2 + 1/3", "Different-sized parts: 1/2 + 1/3"),
    ("g5_unlike_2", "2/3", "1/4", "Новая задача: 2/3 + 1/4", "Another task: 2/3 + 1/4"),
)


def _operand(text: str) -> ParsedAnswer:
    try:
        parsed = parse_answer(text)
    except MathInputError:
        raise MathInputError("unsupported_task") from None
    if not 0 <= parsed.value < 1 or parsed.numerator < 0 or parsed.denominator <= 0:
        raise MathInputError("unsupported_task")
    return parsed


def make_task(left: str, right: str, require_simplest: bool = True, task_id: str = "") -> dict:
    """Build a task from two confirmed proper fractions, preserving given parts."""
    if type(require_simplest) is not bool or not isinstance(task_id, str) or len(task_id) > 64:
        raise MathInputError("invalid_task")
    a, b = _operand(left), _operand(right)
    expected = a.value + b.value
    if expected > 2 or abs(expected.numerator) > MAX_NUMBER or expected.denominator > MAX_NUMBER:
        raise MathInputError("unsupported_task")
    left, right = f"{a.numerator}/{a.denominator}", f"{b.numerator}/{b.denominator}"
    if not task_id:
        task_id = "fraction_" + sha256(f"{left}|{right}|{require_simplest}".encode("ascii")).hexdigest()[:20]
    same = a.denominator == b.denominator
    ru_form = " Запиши ответ в сокращённом виде." if require_simplest else " Можно записать равную дробь без сокращения."
    en_form = " Write the answer in simplest form." if require_simplest else " An equivalent unsimplified fraction is accepted."
    return {
        "task_id": task_id,
        "skill_id": _SKILL_SAME if same else _SKILL_UNLIKE,
        "grade_hint": 4 if same else 5,
        "left": left,
        "right": right,
        "require_simplest": require_simplest,
        "prompt_ru": f"Сложи {left} и {right}." + ru_form,
        "prompt_en": f"Add {left} and {right}." + en_form,
        "source": {"kind": "family_provided", "label": "Confirmed numeric operands; original local prompt", "use_scope": "private_family_only"},
        "content_version": CONTENT_VERSION,
        "teacher_method_status": "unknown",
    }


def list_presets() -> list[dict]:
    return [{"id": row[0], "label_ru": row[3], "label_en": row[4]} for row in _PRESETS]


def select_preset(preset_id: str) -> dict:
    for identifier, left, right, _, _ in _PRESETS:
        if preset_id == identifier:
            task = make_task(left, right, task_id=identifier)
            task["source"] = {"kind": "original_demo", "label": "Original local practice example", "use_scope": "original_example"}
            return task
    raise MathInputError("invalid_preset")


def _parts(task: dict) -> tuple[ParsedAnswer, ParsedAnswer, bool]:
    try:
        left, right, simplest = task["left"], task["right"], task["require_simplest"]
        if type(simplest) is not bool:
            raise MathInputError("invalid_task")
        a, b = _operand(left), _operand(right)
        total = a.value + b.value
        if total.numerator > MAX_NUMBER or total.denominator > MAX_NUMBER:
            raise MathInputError("unsupported_task")
        return a, b, simplest
    except (KeyError, TypeError):
        raise MathInputError("invalid_task") from None


def _invalid_feedback(code: str) -> dict:
    messages = {
        "zero_denominator": ("Знаменатель не может быть нулём. Проверь запись дроби.", "The denominator cannot be zero. Check the fraction you typed."),
        "answer_too_long": ("Запись слишком длинная. Введи одно целое число или дробь a/b.", "That entry is too long. Enter one integer or a fraction a/b."),
        "number_out_of_range": ("Числа пока слишком большие для этого проверяющего. Используй числа до 1000000.", "Those numbers are outside this checker's range. Use numbers up to 1000000."),
        "invalid_number": ("Сейчас я проверяю одно целое число или дробь a/b. Смешанное число можно записать неправильной дробью.", "For now, enter one integer or a fraction a/b. You can write a mixed number as an improper fraction."),
    }
    ru, en = messages.get(code, messages["invalid_number"])
    return {"state": "invalid", "correct_value": None, "correct_form": None, "correct": False,
            "code": code, "text_ru": ru, "text_en": en, "checker": CHECKER_VERSION, "checked_claims": []}


def check_answer(task: dict, text: str) -> dict:
    a, b, require_simplest = _parts(task)
    try:
        answer = parse_answer(text)
    except MathInputError as error:
        return _invalid_feedback(error.code)
    correct_value = answer.value == a.value + b.value
    correct_form = answer.is_simplest
    correct = correct_value and (correct_form or not require_simplest)
    if correct:
        code = "correct"
        ru = "Числовой ответ верный. Я проверил значение" + (" и сокращённую запись." if require_simplest else ". Сокращение в этом задании не требовалось.")
        en = "The numerical answer is correct. I checked the value" + (" and simplest form." if require_simplest else ". Simplest form was not required for this task.")
    elif correct_value:
        code = "equivalent_not_simplest"
        ru = "Значение верное. Сделай знаменатель положительным и сократи числитель и знаменатель на общий делитель, если он есть."
        en = "The value is correct. Make the denominator positive and divide both numbers by a common factor if there is one."
    else:
        code = "incorrect_value"
        if answer.value == Fraction(a.numerator + b.numerator, a.denominator + b.denominator):
            ru = "Похоже, здесь сложены и числители, и знаменатели. Знаменатель показывает размер долей: сначала нужны доли одинакового размера."
            en = "It looks as if both numerators and denominators were added. A denominator tells us the size of the parts: first use equal-sized parts."
        else:
            ru = "Пока значение не совпало. Проверь размер долей и число долей; можно попросить следующую подсказку."
            en = "The value does not match yet. Check the size and number of parts; you can ask for the next hint."
    claims = ["answer_equivalence"]
    if require_simplest:
        claims.append("simplest_form")
    return {"state": "verified", "correct_value": correct_value, "correct_form": correct_form,
            "correct": correct, "code": code, "text_ru": ru, "text_en": en,
            "checker": CHECKER_VERSION, "checked_claims": claims}


def _visual(a: ParsedAnswer, b: ParsedAnswer) -> dict | None:
    denominator = lcm(a.denominator, b.denominator)
    if denominator > 24:
        return None
    left = a.numerator * (denominator // a.denominator)
    right = b.numerator * (denominator // b.denominator)
    return {"denominator": denominator, "left_parts": left, "right_parts": right, "total_parts": left + right}


def _worked(a: ParsedAnswer, b: ParsedAnswer, language: str) -> str:
    denominator = lcm(a.denominator, b.denominator)
    left = a.numerator * (denominator // a.denominator)
    right = b.numerator * (denominator // b.denominator)
    total = left + right
    equation = f"{a.numerator}/{a.denominator} + {b.numerator}/{b.denominator} = {left}/{denominator} + {right}/{denominator} = {total}/{denominator}"
    value = a.value + b.value
    if f"{total}/{denominator}" != fraction_text(value):
        equation += " = " + fraction_text(value)
    if language == "ru":
        return f"Берём доли размера 1/{denominator}, затем складываем их количество: {equation}."
    return f"Use parts of size 1/{denominator}, then add the number of parts: {equation}."


def _analogous_task(task: dict, excluded_totals: tuple[Fraction, ...] = ()) -> dict:
    a, b, require_simplest = _parts(task)
    # Preserve the original given denominator sizes whenever a distinct task
    # exists. A different target value avoids simply copying the last answer.
    da, db = a.denominator, b.denominator
    if da == db:
        da = db = da if da >= 3 else 4
    elif da < 2 or db < 2:
        da, db = 2, 3
    a_numbers = sorted({0, 1, min(2, da - 1), min(3, da - 1)})
    b_numbers = sorted({0, 1, min(2, db - 1), min(3, db - 1)})
    original_values = sorted((a.value, b.value))
    original_total = a.value + b.value
    candidate_pairs = list(product(a_numbers, b_numbers))
    positive = [(f"{na}/{da}", f"{nb}/{db}") for na, nb in candidate_pairs if na and nb]
    zero = [(f"{na}/{da}", f"{nb}/{db}") for na, nb in candidate_pairs if not na or not nb]
    # Keep a worked example useful even when the original denominators offer
    # only one other positive pair. These are original, bounded alternatives.
    alternatives = [("1/5", "1/5"), ("1/5", "2/5"), ("1/6", "1/6"), ("1/6", "2/6")]
    if a.denominator != b.denominator:
        alternatives = [("1/3", "1/4"), ("1/4", "1/6"), ("1/2", "1/5"), ("2/3", "1/5")]
    for left, right in positive + alternatives + zero:
        values = sorted((Fraction(left), Fraction(right)))
        if values == original_values:
            continue
        total = sum(values)
        if total == original_total or total in excluded_totals:
            continue
        try:
            new = make_task(left, right, require_simplest)
        except MathInputError:
            continue
        new["source"] = {"kind": "original_demo", "label": "Original analogous local practice", "use_scope": "original_example"}
        return new
    raise MathInputError("unsupported_task")


def practice_task(task: dict) -> dict:
    return _analogous_task(task)


def hint(task: dict, level: int) -> dict:
    a, b, _ = _parts(task)
    if type(level) is not int or level < 1:
        raise MathInputError("invalid_hint_level")
    level = min(level, 4)
    visual = None
    if level == 1:
        ru = "Знаменатель показывает, на сколько равных долей разделено целое. У наших дробей доли одинакового размера?"
        en = "A denominator tells us how many equal parts make one whole. Are the parts in these two fractions the same size?"
    elif level == 2:
        denominator = lcm(a.denominator, b.denominator)
        ru = f"Обе дроби можно выразить долями размера 1/{denominator}. Сколько таких долей в первой дроби и сколько во второй?"
        en = f"Both fractions can use parts of size 1/{denominator}. How many of those parts are in each fraction?"
        visual = _visual(a, b)
    elif level == 3:
        practice = practice_task(task)
        practice_value = Fraction(practice["left"]) + Fraction(practice["right"])
        # Never work through the same target that the next independent
        # practice button will present. Both operands AND answer differ.
        example = _analogous_task(task, excluded_totals=(practice_value,))
        x, y, _ = _parts(example)
        ru = "Разберём другой пример, затем вернёмся к твоему. " + _worked(x, y, "ru")
        en = "Let's work through a different example, then return to yours. " + _worked(x, y, "en")
        visual = _visual(x, y)
    else:
        ru = "Разберём исходный пример вместе. " + _worked(a, b, "ru") + " После этого попробуй новый похожий пример самостоятельно."
        en = "Let's work through the original problem together. " + _worked(a, b, "en") + " Next, try a new similar problem on your own."
        visual = _visual(a, b)
    return {"text_ru": ru, "text_en": en, "level": level, "visual": visual}
