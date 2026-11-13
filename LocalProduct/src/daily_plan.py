"""Local daily checklist and Steve's progress with atomic, revision-checked writes."""

from contextlib import contextmanager
from datetime import date
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from activity_core import exclusive_lock, no_links
from paths import DEPLOY_ROOT


MAX_TASKS = 10
MAX_TASK_LENGTH = 120
MAX_TASK_MINUTES = 120
MAX_FEEDBACK_LENGTH = 240
MAX_FILE_BYTES = 8192
TASK_POINTS = (10, 15, 20)
DEFAULT_TASK_POINTS = 15
DAILY_POINTS_CAP = 30
REBIRTH_POINTS = 600
TASK_STATES = ('pending', 'done', 'help')
PLAN_STATES = ('open', 'submitted', 'approved')
EQUIPMENT_TIERS = (
    (0, 'none', 'iron', False),
    (10, 'leather', 'iron', False),
    (75, 'copper', 'iron', False),
    (150, 'iron', 'iron', True),
    (240, 'gold', 'diamond', False),
    (360, 'diamond', 'diamond', False),
    (480, 'diamond', 'netherite', False),
    (REBIRTH_POINTS, 'netherite', 'netherite', True),
)


class DailyPlanError(ValueError):
    """A plan is unavailable or the requested change is invalid."""


class DailyPlanConflict(DailyPlanError):
    """The plan was changed or the day rolled over after the last read."""


def _clean_text(value, limit, *, allow_empty=False):
    if not isinstance(value, str):
        raise DailyPlanError('Введите текст.')
    value = value.strip()
    if (not value and not allow_empty) or len(value) > limit or (value and not value.isprintable()):
        raise DailyPlanError(f'Введите текст в одну строку, до {limit} символов.')
    return value


def _check_minutes(value):
    if type(value) is not int or not 5 <= value <= 120:
        raise DailyPlanError('Время игры должно быть от 5 до 120 минут.')


def _check_task_minutes(value):
    if type(value) is not int or not 1 <= value <= MAX_TASK_MINUTES:
        raise DailyPlanError(f'Время на дело должно быть от 1 до {MAX_TASK_MINUTES} минут.')


def _check_task_points(value):
    if type(value) is not int or value not in TASK_POINTS:
        raise DailyPlanError('Укажите 10, 15 или 20 баллов на дело.')


def _check_date(value):
    try:
        if date.fromisoformat(value).isoformat() == value:
            return
    except (TypeError, ValueError):
        pass
    raise DailyPlanError('Повреждена дата плана.')


def _validate(plan):
    required = {'date', 'revision', 'status', 'game_minutes', 'feedback', 'tasks'}
    if not isinstance(plan, dict) or not required <= plan.keys() <= required | {'study_session_id'}:
        raise DailyPlanError('Повреждён файл плана.')
    _check_date(plan['date'])
    revision = plan['revision']
    if type(revision) is not int or not 0 < revision < 2**128:
        raise DailyPlanError('Повреждена версия плана.')
    if plan['status'] not in PLAN_STATES:
        raise DailyPlanError('Повреждён статус плана.')
    if 'study_session_id' in plan:
        session_id = plan['study_session_id']
        if (plan['status'] == 'open' or not isinstance(session_id, str)
                or not re.fullmatch(r'[0-9a-f]{32}', session_id)):
            raise DailyPlanError('Повреждена привязка плана к урокам.')
    _check_minutes(plan['game_minutes'])
    _clean_text(plan['feedback'], MAX_FEEDBACK_LENGTH, allow_empty=True)
    tasks = plan['tasks']
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_TASKS:
        raise DailyPlanError('Повреждён список дел.')
    for task in tasks:
        if not isinstance(task, dict) or not {'text', 'status'} <= task.keys() <= {
            'text', 'status', 'estimated_minutes', 'points',
        }:
            raise DailyPlanError('Повреждён пункт плана.')
        if task['status'] not in TASK_STATES:
            raise DailyPlanError('Повреждён пункт плана.')
        _clean_text(task['text'], MAX_TASK_LENGTH)
        if 'estimated_minutes' in task:
            _check_task_minutes(task['estimated_minutes'])
        if 'points' in task:
            _check_task_points(task['points'])
    if plan['status'] != 'open' and any(task['status'] != 'done' for task in tasks):
        raise DailyPlanError('У отправленного плана есть незавершённые дела.')
    return plan


def _initial_state(plan=None):
    return {'version': 1, 'plan': plan, 'past_points': 0, 'rebirth_count': 0, 'rebirth_date': None}


def _validate_state(state):
    if not isinstance(state, dict):
        raise DailyPlanError('Повреждён файл плана.')
    if 'version' not in state:
        return _initial_state(_validate(state))
    if state.keys() != _initial_state().keys() or type(state['version']) is not int or state['version'] != 1:
        raise DailyPlanError('Неизвестный формат плана.')
    if any(type(state[key]) is not int or state[key] < 0 for key in ('past_points', 'rebirth_count')):
        raise DailyPlanError('Повреждён прогресс Стива.')
    if state['rebirth_count']:
        _check_date(state['rebirth_date'])
    elif state['rebirth_date'] is not None:
        raise DailyPlanError('Повреждён прогресс Стива.')
    if state['plan'] is not None:
        _validate(state['plan'])
    return state


def _earned_points(plan):
    if plan is None:
        return 0
    weights = [task.get('points', DEFAULT_TASK_POINTS) for task in plan['tasks']]
    completed = sum(weight for task, weight in zip(plan['tasks'], weights) if task['status'] == 'done')
    return DAILY_POINTS_CAP * completed // sum(weights)


def _unsettled_points(state):
    plan = state['plan']
    # The day of a rebirth has already contributed to the previous cycle.
    if plan is None or plan['date'] <= (state['rebirth_date'] or ''):
        return 0
    return _earned_points(plan)


def _latest_date(state):
    return max(state['plan']['date'] if state['plan'] else '', state['rebirth_date'] or '')


def equipment_for_points(points):
    """Return the current equipment and the next milestone, if one exists."""
    current = None
    for threshold, armor, pickaxe, enchanted in EQUIPMENT_TIERS:
        tier = {'threshold': threshold, 'armor': armor, 'pickaxe': pickaxe, 'enchanted': enchanted}
        if threshold > points and current is not None:
            return current, tier
        current = tier
    return current, None


class DailyPlan:
    def __init__(self, root=None, *, today=date.today):
        self.root = Path(root) if root is not None else DEPLOY_ROOT / 'data' / 'daily-plan'
        self.path = self.root / 'today.json'
        self.today = today

    def _read_state(self):
        try:
            with no_links(self.path, True).open('rb') as stream:
                content = stream.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return _initial_state()
        if len(content) > MAX_FILE_BYTES:
            raise DailyPlanError('Файл плана слишком большой.')
        try:
            decoded = json.loads(content.decode('utf-8'))
        except (ValueError, RecursionError) as error:
            raise DailyPlanError('Повреждён файл плана.') from error
        return _validate_state(decoded)

    @staticmethod
    def _today_plan(state, day):
        plan = state['plan']
        return plan if plan is not None and plan['date'] == day else None

    def read(self):
        return self._today_plan(self._read_state(), self.today().isoformat())

    def snapshot(self):
        """Read today's checklist and the matching equipment from one file snapshot."""
        state = self._read_state()
        day = self.today().isoformat()
        plan = self._today_plan(state, day)
        total_points = state['past_points'] + _unsettled_points(state)
        current_tier, next_tier = equipment_for_points(total_points)
        return plan, {
            'today_points': _earned_points(plan),
            'planned_points': DAILY_POINTS_CAP if plan is not None else 0,
            'total_points': total_points,
            'daily_cap': DAILY_POINTS_CAP,
            'current_tier': current_tier,
            'next_tier': next_tier,
            'rebirth_count': state['rebirth_count'],
            'can_rebirth': total_points >= REBIRTH_POINTS and day >= _latest_date(state),
        }

    def progress(self):
        return self.snapshot()[1]

    @contextmanager
    def _transaction(self):
        no_links(self.root).mkdir(parents=True, exist_ok=True)
        with exclusive_lock(self.root / 'today.lock'):
            day = self.today().isoformat()
            state = self._read_state()
            if day < _latest_date(state):
                raise DailyPlanConflict('Дата на компьютере раньше сохранённого плана. Проверьте часы.')
            yield state, day
            if self.today().isoformat() != day:
                raise DailyPlanConflict('Наступил новый день. Обновите окно плана.')
            self._write(_validate_state(state))

    @staticmethod
    def _check_revision(expected_revision, current):
        if expected_revision is not None and type(expected_revision) is not int:
            raise DailyPlanError('Неверная версия плана.')
        if expected_revision != (current['revision'] if current else None):
            raise DailyPlanConflict('План уже изменился. Обновите окно и попробуйте снова.')

    @contextmanager
    def _change(self, expected_revision):
        with self._transaction() as (state, day):
            current = self._today_plan(state, day)
            self._check_revision(expected_revision, current)
            if current is None:
                state['past_points'] += _unsettled_points(state)
                current = {'date': day}
            yield current
            current['revision'] = uuid.uuid4().int
            state['plan'] = current

    def _write(self, state):
        descriptor, temporary = tempfile.mkstemp(prefix='.today-', suffix='.tmp', dir=self.root)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(state, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            no_links(self.path, True)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def publish(self, tasks, game_minutes, *, expected_revision, task_minutes=None, task_points=None):
        if not isinstance(tasks, (list, tuple)) or not 1 <= len(tasks) <= MAX_TASKS:
            raise DailyPlanError(f'Добавьте от одного до {MAX_TASKS} дел.')
        if task_minutes is not None and (
            not isinstance(task_minutes, (list, tuple)) or len(task_minutes) != len(tasks)
        ):
            raise DailyPlanError('Укажите время для каждого дела или оставьте его пустым.')
        if task_points is not None and (
            not isinstance(task_points, (list, tuple)) or len(task_points) != len(tasks)
        ):
            raise DailyPlanError('Укажите баллы для каждого дела.')
        items = [{'text': _clean_text(text, MAX_TASK_LENGTH), 'status': 'pending'} for text in tasks]
        if task_minutes is not None:
            for item, estimate in zip(items, task_minutes):
                if estimate is not None:
                    _check_task_minutes(estimate)
                    item['estimated_minutes'] = estimate
        if task_points is not None:
            for item, weight in zip(items, task_points):
                _check_task_points(weight)
                item['points'] = weight
        _check_minutes(game_minutes)
        with self._change(expected_revision) as plan:
            if plan.get('status') in ('submitted', 'approved'):
                raise DailyPlanError('Изменить можно только открытый план.')
            plan.update(status='open', game_minutes=game_minutes, feedback='', tasks=items)
        return plan

    def rebirth(self, *, expected_revision, expected_cycle):
        if type(expected_cycle) is not int or expected_cycle < 0:
            raise DailyPlanError('Неверная версия прогресса.')
        with self._transaction() as (state, day):
            current = self._today_plan(state, day)
            self._check_revision(expected_revision, current)
            if expected_cycle != state['rebirth_count']:
                raise DailyPlanConflict('Прогресс уже изменился. Обновите окно и попробуйте снова.')
            total_points = state['past_points'] + _unsettled_points(state)
            if total_points < REBIRTH_POINTS:
                raise DailyPlanError('Перерождение доступно после получения последнего комплекта.')
            state.update(past_points=0, rebirth_count=expected_cycle + 1, rebirth_date=day)
            if current is not None:
                current['revision'] = uuid.uuid4().int
        return current

    @staticmethod
    def _task(plan, index):
        if type(index) is not int or not 0 <= index < len(plan['tasks']):
            raise DailyPlanError('Выберите пункт из списка.')
        return plan['tasks'][index]

    def set_task(self, index, status, *, expected_revision):
        if status not in TASK_STATES:
            raise DailyPlanError('Неверная отметка дела.')
        with self._change(expected_revision) as plan:
            if plan.get('status') != 'open':
                raise DailyPlanError('Отметки доступны в открытом плане.')
            self._task(plan, index)['status'] = status
        return plan

    def submit(self, *, expected_revision, study_session_id=None):
        with self._change(expected_revision) as plan:
            if plan.get('status') != 'open' or any(task['status'] != 'done' for task in plan['tasks']):
                raise DailyPlanError('Отметь все дела, чтобы отправить план на проверку.')
            plan['status'] = 'submitted'
            if study_session_id is not None:
                plan['study_session_id'] = study_session_id
        return plan

    def review(self, *, approved, feedback, expected_revision, task_index=None):
        if type(approved) is not bool or (approved and task_index is not None):
            raise DailyPlanError('Неверное решение по плану.')
        feedback = _clean_text(feedback, MAX_FEEDBACK_LENGTH, allow_empty=approved)
        with self._change(expected_revision) as plan:
            if plan.get('status') != 'submitted':
                raise DailyPlanError('План должен быть отправлен на проверку.')
            if not approved:
                self._task(plan, task_index)['status'] = 'pending'
                plan.pop('study_session_id', None)
            plan.update(status='approved' if approved else 'open', feedback=feedback)
        return plan
