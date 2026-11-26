"""Local negotiated rewards and two child-chosen goals; no GUI/network/Windows actions.

The caller authenticates parent-only actions and creates a contract BEFORE work
begins. This same-user JSON store is not an authorization or child-proof boundary.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from activity_core import exclusive_lock, no_links

MAX_BYTES = 2 * 1024 * 1024
MAX_AGREEMENTS = 400
MAX_GOALS = 32
MAX_ACTIVE_GOALS = 2
MAX_REVIEWS = 20
MAX_AMOUNT = 1_000_000
IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,80}\Z')


class RewardError(ValueError):
    pass


class RewardConflict(RewardError):
    pass


def _text(value, limit, empty=False):
    if not isinstance(value, str) or value != value.strip() or len(value) > limit:
        raise RewardError('Нужен короткий текст без пробелов по краям.')
    if (not value and not empty) or (value and not value.isprintable()):
        raise RewardError('Введите текст в одну строку.')
    return value


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise RewardError('Число вне допустимого диапазона.')
    return value


def _identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise RewardError('Неверный идентификатор.')
    return value


def plan_key(plan_date, plan_revision):
    try:
        valid = isinstance(plan_date, str) and date.fromisoformat(plan_date).isoformat() == plan_date
    except ValueError:
        valid = False
    if not valid:
        raise RewardError('Неверная дата договора.')
    _integer(plan_revision, 1, 2**128 - 1)
    return f'{plan_date}:{plan_revision}'


def _initial():
    return {'version': 1, 'revision': 0, 'goals': [], 'agreements': []}


def _goal(value):
    if type(value) is not dict or set(value) != {'goal_id', 'title', 'target', 'unit', 'balance', 'status', 'created_ms'}:
        raise RewardError('Повреждена цель.')
    _identifier(value['goal_id'])
    _text(value['title'], 120)
    _text(value['unit'], 40)
    _integer(value['target'], 1, MAX_AMOUNT)
    _integer(value['balance'], 0, MAX_AMOUNT * MAX_AGREEMENTS * 10 * 4)
    _integer(value['created_ms'], 0, 2**63 - 1)
    if value['status'] not in ('proposed', 'confirmed', 'retired'):
        raise RewardError('Повреждён статус цели.')
    return value


def _tasks(value, goals, *, preparing=False):
    if type(value) is not list or not 1 <= len(value) <= 10:
        raise RewardError('В договоре должно быть от 1 до 10 дел.')
    result, seen = [], set()
    for task in value:
        if type(task) is not dict or set(task) != {'task_id', 'criteria', 'rewards'}:
            raise RewardError('Неверное дело в договоре.')
        ident = _identifier(task['task_id'])
        if ident in seen:
            raise RewardError('Повторяется идентификатор дела.')
        seen.add(ident)
        _text(task['criteria'], 500)
        rewards = task['rewards']
        if type(rewards) is not list or len(rewards) > 4:
            raise RewardError('Допускается до четырёх наград за дело.')
        items, reward_ids = [], set()
        for reward in rewards:
            required = {'reward_id', 'kind', 'amount', 'label'}
            if type(reward) is not dict or not required <= set(reward) <= required | {'unit', 'goal_id'}:
                raise RewardError('Неверная награда.')
            rid = _identifier(reward['reward_id'])
            if rid in reward_ids:
                raise RewardError('Повторяется идентификатор награды.')
            reward_ids.add(rid)
            kind = reward['kind']
            if kind not in ('game_minutes', 'custom', 'goal'):
                raise RewardError('Неизвестный вид награды.')
            _integer(reward['amount'], 1, 120 if kind == 'game_minutes' else MAX_AMOUNT)
            _text(reward['label'], 120)
            item = {key: reward[key] for key in ('reward_id', 'kind', 'amount', 'label')}
            if kind == 'goal':
                goal_id = _identifier(reward.get('goal_id'))
                goal = next((g for g in goals if g['goal_id'] == goal_id), None)
                if goal is None or (preparing and goal['status'] != 'confirmed'):
                    raise RewardError('Сначала родитель подтверждает выбранную цель.')
                unit = reward.get('unit', goal['unit'])
                if unit != goal['unit']:
                    raise RewardError('Единица награды не совпадает с единицей цели.')
                item['goal_id'] = goal_id
            else:
                if 'goal_id' in reward:
                    raise RewardError('Цель указана у другого вида награды.')
                unit = reward.get('unit', 'минуты' if kind == 'game_minutes' else 'баллы')
                if kind == 'game_minutes' and unit != 'минуты':
                    raise RewardError('Для игрового времени используются минуты.')
            item['unit'] = _text(unit, 40)
            items.append(item)
        result.append({'task_id': ident, 'criteria': task['criteria'], 'rewards': items})
    if sum(reward['amount'] for task in result for reward in task['rewards']
           if reward['kind'] == 'game_minutes') > 120:
        raise RewardError('Суммарное игровое время договора должно быть не больше 120 минут.')
    return result


def _slots(tasks):
    return {f'{task["task_id"]}/{reward["reward_id"]}': reward
            for task in tasks for reward in task['rewards']}


def _validate(state):
    if type(state) is not dict or set(state) != {'version', 'revision', 'goals', 'agreements'}:
        raise RewardError('Повреждена запись наград.')
    if type(state['version']) is not int or state['version'] != 1:
        raise RewardError('Неизвестная версия записи наград.')
    _integer(state['revision'], 0, 2**63 - 1)
    if type(state['goals']) is not list or len(state['goals']) > MAX_GOALS:
        raise RewardError('Слишком много сохранённых целей.')
    goals = [_goal(goal) for goal in state['goals']]
    if len({goal['goal_id'] for goal in goals}) != len(goals):
        raise RewardError('Повторяются цели.')
    active = [goal for goal in goals if goal['status'] != 'retired' and goal['balance'] < goal['target']]
    if len(active) > MAX_ACTIVE_GOALS:
        raise RewardError('Одновременно доступны не более двух больших целей.')
    agreements = state['agreements']
    if type(agreements) is not list or len(agreements) > MAX_AGREEMENTS:
        raise RewardError('Архив договорённостей заполнен.')
    seen, review_ids = set(), set()
    goal_totals = {goal['goal_id']: 0 for goal in goals}
    for item in agreements:
        expected = {'key', 'date', 'plan_revision', 'tasks', 'credited', 'ratings', 'reviews', 'revisits', 'created_ms'}
        if type(item) is not dict or set(item) != expected:
            raise RewardError('Повреждён договор о наградах.')
        if item['key'] != plan_key(item['date'], item['plan_revision']) or item['key'] in seen:
            raise RewardError('Повреждён идентификатор договора.')
        seen.add(item['key'])
        canonical = _tasks(item['tasks'], goals)
        if canonical != item['tasks']:
            raise RewardError('Неоднозначная запись договора.')
        _integer(item['created_ms'], 0, 2**63 - 1)
        slots = _slots(canonical)
        task_ids = {task['task_id'] for task in canonical}
        if type(item['credited']) is not dict or set(item['credited']) != set(slots):
            raise RewardError('Повреждено начисление награды.')
        for key, amount in item['credited'].items():
            _integer(amount, 0, slots[key]['amount'])
            if slots[key]['kind'] == 'goal':
                goal_totals[slots[key]['goal_id']] += amount
        if type(item['ratings']) is not dict or set(item['ratings']) - task_ids:
            raise RewardError('Повреждена оценка качества.')
        for percent in item['ratings'].values():
            _integer(percent, 0, 100)
        if type(item['reviews']) is not list or len(item['reviews']) > MAX_REVIEWS:
            raise RewardError('Архив оценок заполнен.')
        sums = {key: 0 for key in slots}
        for review in item['reviews']:
            fields = {'review_id', 'ratings', 'comment', 'at_ms', 'credits', 'fingerprint'}
            if type(review) is not dict or set(review) != fields:
                raise RewardError('Повреждена история оценки.')
            rid = _identifier(review['review_id'])
            if rid in review_ids:
                raise RewardError('Повторяется событие оценки.')
            review_ids.add(rid)
            if type(review['ratings']) is not dict or set(review['ratings']) != task_ids:
                raise RewardError('Неполная сохранённая оценка.')
            for percent in review['ratings'].values():
                _integer(percent, 0, 100)
            _text(review['comment'], 500, empty=True)
            _integer(review['at_ms'], 0, 2**63 - 1)
            if review['fingerprint'] != _fingerprint(item['key'], review['ratings'], review['comment']):
                raise RewardError('Повреждена привязка оценки.')
            if type(review['credits']) is not dict or set(review['credits']) != set(slots):
                raise RewardError('Повреждена история начислений.')
            for key, amount in review['credits'].items():
                _integer(amount, 0, slots[key]['amount'])
                task_id = key.split('/', 1)[0]
                expected_delta = max(0, slots[key]['amount'] * review['ratings'][task_id] // 100 - sums[key])
                if amount != expected_delta:
                    raise RewardError('Начисление не соответствует согласованной оценке.')
                sums[key] += amount
        if sums != item['credited']:
            raise RewardError('История не совпадает с начисленной наградой.')
        if item['reviews'] and item['ratings'] != item['reviews'][-1]['ratings']:
            raise RewardError('Последняя оценка не совпадает с историей.')
        if not item['reviews'] and (item['ratings'] or any(item['credited'].values())):
            raise RewardError('Награда начислена без оценки.')
        if type(item['revisits']) is not dict or set(item['revisits']) - task_ids:
            raise RewardError('Повреждён запрос пересмотра.')
        for request in item['revisits'].values():
            if type(request) is not dict or set(request) != {'note', 'at_ms', 'requested', 'resolved_review_id'}:
                raise RewardError('Повреждён запрос пересмотра.')
            _text(request['note'], 500)
            _integer(request['at_ms'], 0, 2**63 - 1)
            if type(request['requested']) is not bool:
                raise RewardError('Повреждён статус пересмотра.')
            if request['resolved_review_id'] is not None:
                _identifier(request['resolved_review_id'])
                if request['resolved_review_id'] not in {r['review_id'] for r in item['reviews']}:
                    raise RewardError('Пересмотр не связан с оценкой.')
    if any(goal['balance'] != goal_totals[goal['goal_id']] for goal in goals):
        raise RewardError('Копилка не совпадает с подтверждёнными начислениями.')
    return state


def _fingerprint(key, ratings, comment):
    data = json.dumps([key, ratings, comment], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(data.encode('utf-8')).hexdigest()


class RewardBook:
    def __init__(self, root, *, clock=time.time):
        self.root = Path(root)
        if (not self.root.is_absolute() or str(self.root).startswith(('\\\\', '//')) or
                '..' in self.root.parts or any(':' in part for part in self.root.parts[1:])):
            raise RewardError('Нужен абсолютный путь данных наград.')
        self.path = self.root / 'rewards.json'
        self.clock = clock

    def snapshot(self):
        try:
            with no_links(self.path, True).open('rb') as stream:
                data = stream.read(MAX_BYTES + 1)
        except FileNotFoundError:
            return _initial()
        if len(data) > MAX_BYTES:
            raise RewardError('Файл наград слишком большой; данные сохранены.')
        try:
            state = json.loads(data.decode('utf-8'), object_pairs_hook=_unique)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise RewardError('Не удалось прочитать файл наград.') from error
        return deepcopy(_validate(state))

    def _now(self):
        value = self.clock()
        if (type(value) not in (int, float) or not math.isfinite(value) or
                not 0 <= value <= (2**63 - 1) / 1000):
            raise RewardError('Не удалось определить время.')
        return _integer(int(value * 1000), 0, 2**63 - 1)

    def _write(self, state):
        _validate(state)
        data = json.dumps(state, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(data) > MAX_BYTES:
            raise RewardError('Архив наград заполнен; прежние данные сохранены.')
        descriptor, name = tempfile.mkstemp(prefix='.rewards-', suffix='.tmp', dir=self.root)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            no_links(self.path, self.path.exists())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                no_links(name, True).unlink()

    def _change(self, expected_revision, operation):
        _integer(expected_revision, 0, 2**63 - 1)
        no_links(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        no_links(self.root)
        with exclusive_lock(self.root / 'rewards.lock'):
            state = self.snapshot()
            original = deepcopy(state)
            result, replay = operation(state)
            if replay:
                return result
            if state['revision'] != expected_revision:
                raise RewardConflict('Договорённости изменились. Обновите доску.')
            if state != original:
                state['revision'] += 1
                self._write(state)
            return {**result, 'state_revision': state['revision']}

    def propose_goal(self, title, target, unit, *, expected_revision):
        _text(title, 120); _integer(target, 1, MAX_AMOUNT); _text(unit, 40)
        def change(state):
            active = [goal for goal in state['goals'] if goal['status'] != 'retired' and goal['balance'] < goal['target']]
            if len(active) >= MAX_ACTIVE_GOALS or len(state['goals']) >= MAX_GOALS:
                raise RewardError('Сначала заверши или отложи одну из двух целей.')
            goal = {'goal_id': uuid.uuid4().hex, 'title': title, 'target': target, 'unit': unit,
                    'balance': 0, 'status': 'proposed', 'created_ms': self._now()}
            state['goals'].append(goal)
            return {'goal': deepcopy(goal)}, False
        return self._change(expected_revision, change)

    def _goal_status(self, goal_id, status, expected_revision):
        _identifier(goal_id)
        def change(state):
            goal = next((item for item in state['goals'] if item['goal_id'] == goal_id), None)
            if goal is None:
                raise RewardError('Цель не найдена.')
            if status == 'confirmed' and goal['status'] == 'retired':
                raise RewardError('Отложенная цель остаётся в истории; предложи новую.')
            goal['status'] = status
            return {'goal': deepcopy(goal)}, False
        return self._change(expected_revision, change)

    def confirm_goal(self, goal_id, *, expected_revision):
        """Parent-only: confirm the child's proposal. Caller performs authentication."""
        return self._goal_status(goal_id, 'confirmed', expected_revision)

    def retire_goal(self, goal_id, *, expected_revision):
        """Parent-only: retain all earned units/history; allow a replacement goal."""
        return self._goal_status(goal_id, 'retired', expected_revision)

    def prepare_plan(self, plan_date, plan_revision, tasks, *, expected_revision):
        """Parent-only, BEFORE tasks begin. Date + initial published revision is stable."""
        key = plan_key(plan_date, plan_revision)
        def change(state):
            canonical = _tasks(tasks, state['goals'])
            found = next((item for item in state['agreements'] if item['key'] == key), None)
            if found is not None:
                if found['tasks'] != canonical:
                    raise RewardConflict('Согласованные критерии и награды менять задним числом нельзя.')
                return {'agreement': deepcopy(found), 'state_revision': state['revision'], 'replayed': True}, True
            canonical = _tasks(tasks, state['goals'], preparing=True)
            if len(state['agreements']) >= MAX_AGREEMENTS:
                raise RewardError('Архив договорённостей заполнен; старые награды сохранены.')
            item = {'key': key, 'date': plan_date, 'plan_revision': plan_revision, 'tasks': canonical,
                    'credited': {slot: 0 for slot in _slots(canonical)}, 'ratings': {}, 'reviews': [],
                    'revisits': {}, 'created_ms': self._now()}
            state['agreements'].append(item)
            return {'agreement': deepcopy(item), 'replayed': False}, False
        return self._change(expected_revision, change)

    def evaluate(self, plan_date, plan_revision, ratings, *, review_id, expected_revision, comment=''):
        """Parent-only. Returns NEW delta; replay returns game_minutes_awarded=0."""
        key = plan_key(plan_date, plan_revision)
        _identifier(review_id); _text(comment, 500, empty=True)
        if type(ratings) is not dict or not ratings:
            raise RewardError('Укажите оценку качества.')
        for task_id, percent in ratings.items():
            _identifier(task_id); _integer(percent, 0, 100)
        if any(percent < 100 for percent in ratings.values()) and not comment:
            raise RewardError('К оценке ниже 100% добавьте, что именно можно улучшить.')
        fingerprint = _fingerprint(key, ratings, comment)
        def change(state):
            for agreement in state['agreements']:
                for review in agreement['reviews']:
                    if review['review_id'] == review_id:
                        if review['fingerprint'] != fingerprint:
                            raise RewardConflict('Идентификатор оценки уже использован для другого действия.')
                        return {'replayed': True, 'game_minutes_awarded': 0, 'credits': {},
                                'review': deepcopy(review), 'state_revision': state['revision']}, True
            item = next((value for value in state['agreements'] if value['key'] == key), None)
            if item is None:
                raise RewardError('Сначала согласуйте критерии и награды до начала дел.')
            if set(ratings) != {task['task_id'] for task in item['tasks']}:
                raise RewardError('Оцените все дела согласованного договора.')
            if len(item['reviews']) >= MAX_REVIEWS:
                raise RewardError('Сохранено максимальное число пересмотров; история не удалена.')
            slots, credits = _slots(item['tasks']), {}
            minutes = 0
            for task in item['tasks']:
                for reward in task['rewards']:
                    slot = f'{task["task_id"]}/{reward["reward_id"]}'
                    target = reward['amount'] * ratings[task['task_id']] // 100
                    delta = max(0, target - item['credited'][slot])
                    item['credited'][slot] += delta
                    credits[slot] = delta
                    if reward['kind'] == 'game_minutes':
                        minutes += delta
                    elif reward['kind'] == 'goal':
                        goal = next(g for g in state['goals'] if g['goal_id'] == reward['goal_id'])
                        goal['balance'] += delta
            review = {'review_id': review_id, 'ratings': deepcopy(ratings), 'comment': comment,
                      'at_ms': self._now(), 'credits': credits, 'fingerprint': fingerprint}
            item['ratings'] = deepcopy(ratings)
            item['reviews'].append(review)
            for request in item['revisits'].values():
                request.update(requested=False, resolved_review_id=review_id)
            return {'replayed': False, 'game_minutes_awarded': minutes, 'credits': deepcopy(credits),
                    'review': deepcopy(review)}, False
        return self._change(expected_revision, change)

    def request_revisit(self, plan_date, plan_revision, task_id, note, *, expected_revision):
        """Child-visible request, never a new award or a parent approval."""
        key = plan_key(plan_date, plan_revision)
        _identifier(task_id); _text(note, 500)
        def change(state):
            item = next((value for value in state['agreements'] if value['key'] == key), None)
            if item is None or task_id not in item['ratings']:
                raise RewardError('Пока нет оценки этого дела для пересмотра.')
            previous = item['revisits'].get(task_id)
            if previous and previous['requested'] and previous['note'] == note:
                return {'request': deepcopy(previous), 'state_revision': state['revision'], 'replayed': True}, True
            request = {'note': note, 'at_ms': self._now(), 'requested': True, 'resolved_review_id': None}
            item['revisits'][task_id] = request
            return {'request': deepcopy(request), 'replayed': False}, False
        return self._change(expected_revision, change)


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise RewardError('Повторяется ключ записи наград.')
        value[key] = item
    return value
