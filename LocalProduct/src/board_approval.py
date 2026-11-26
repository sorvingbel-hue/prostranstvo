"""One parent-approval path for local PIN and authenticated Telegram commands.

Authentication belongs to the caller. Screen text and child checkboxes never
authorize this service. Reads are pure; only explicit approval/runtime ticks
persist state. The durable plan approval ID joins retries across stores.
"""
from __future__ import annotations

from datetime import date
from contextlib import nullcontext, contextmanager
from pathlib import Path
import os
import math
import re
import subprocess
import sys
import uuid

from activity_core import exclusive_lock, no_links
from daily_plan import DailyPlan, DailyPlanConflict, DailyPlanError
from family_rewards import RewardBook
from play_session import PlaySession, approval_key
from study_resume import StudyResume
from hourly_break import HourlyBreakStore
import paths


def launch_resume_worker(resume):
    """Fixed owned runtime only. Called after a durable parent-approved grant."""
    if resume.beta and getattr(sys, 'frozen', False):
        program = no_links(paths.APP_ROOT / 'ProstranstvoCore.exe', True)
        command = [str(program), 'study-resume']
    else:
        app = paths.APP_ROOT if resume.beta else paths.DEPLOY_ROOT / 'app'
        if resume.beta:
            python = Path(getattr(sys, '_base_executable', sys.executable)).with_name('pythonw.exe')
        else:
            python = Path(no_links(app / 'python-path.txt', True).read_text(encoding='utf-8-sig').strip())
        if not python.is_absolute() or python.name.casefold() != 'pythonw.exe':
            raise ValueError('Неверный путь игрового контроллера')
        command = [str(no_links(python, True)), '-B', str(no_links(app / 'study_runtime.py', True)), '--resume']
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
                     cwd=str(Path(command[0]).parent))


class BoardApproval:
    def __init__(self, plans=None, study=None, play=None, rewards=None, *, root=None,
                 ensure_running=launch_resume_worker, approval_guard=nullcontext, breaks=None):
        self.root = Path(root) if root is not None else paths.DEPLOY_ROOT / 'data/board-approval'
        self.plans = plans if plans is not None else DailyPlan()
        self.study = study if study is not None else StudyResume()
        self.play = play if play is not None else PlaySession(self.study.root.parent / 'play-time')
        self.rewards = rewards if rewards is not None else RewardBook(self.study.root.absolute().parent / 'family-rewards')
        self.ensure_running = ensure_running
        self.approval_guard = approval_guard
        self.breaks = breaks if breaks is not None else HourlyBreakStore(
            self.study.root.absolute().parent / 'hourly-break', clock=self.play.clock,
            monotonic=self.play.monotonic, today=self.plans.today)

    @contextmanager
    def locked(self):
        no_links(self.root).mkdir(parents=True, exist_ok=True)
        with exclusive_lock(self.root / 'approval.lock'):
            yield

    def _study(self):
        lease = self.study.read()
        return lease if lease is not None and self.study.remaining_seconds(lease) > 0 else None

    def start_parent_study(self, minutes):
        """Authenticated parent start supersedes every previous game/break grant.

        Revocation must be durable even if this new short Study finishes before
        the keeper's next tick. Authentication remains the caller's job.
        """
        if type(minutes) is not int or not 0 <= minutes <= 240:
            raise DailyPlanError('Выберите «Без таймера» или от 1 до 240 минут')
        now = self.study.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now <= 0:
            raise DailyPlanError('Не удалось определить время Windows')
        with self.locked():
            cycle = self.breaks.status()
            current = self.play.status()['current']
            # Register before revocation so a registration error changes no grant.
            self.study.enable_startup()
            if cycle['enabled'] or cycle['current'] is not None:
                self.breaks.cancel(source_play_id=current['play_id'] if current else None,
                    reason='study_replaced')
            if current is not None:
                self.play.cancel(current['play_id'], reason='parent_study_started')
            return self.study.activate(minutes)

    def agreement(self, plan):
        revision = plan.get('contract_revision')
        if revision is None:
            return None
        key = f'{plan["date"]}:{revision}'
        return next((item for item in self.rewards.snapshot()['agreements'] if item['key'] == key), None)

    def approve(self, *, plan_date, revision, study_id, request_id=None, quality_percent=100, comment='',
                approval_guard=None):
        if (not isinstance(plan_date, str) or type(revision) is not int or not 0 < revision < 2**128
                or type(quality_percent) is not int or not 0 <= quality_percent <= 100
                or (study_id is not None and (not isinstance(study_id, str)
                    or not re.fullmatch(r'[0-9a-f]{32}', study_id)))):
            raise DailyPlanError('Неверные параметры одобрения.')
        request_id = uuid.uuid4().hex if request_id is None else request_id
        if not isinstance(request_id, str) or not re.fullmatch(r'[0-9a-f]{32}', request_id):
            raise DailyPlanError('Неверный идентификатор одобрения.')
        no_links(self.root).mkdir(parents=True, exist_ok=True)
        with exclusive_lock(self.root / 'approval.lock'):
            plan = self.plans.read(plan_date=plan_date)
            if plan is None or plan.get('study_session_id') != study_id:
                raise DailyPlanConflict('План или связанный сеанс изменился.')
            agreement = self.agreement(plan)
            if quality_percent < 100 and (agreement is None or not str(comment).strip()):
                raise DailyPlanError('Оценку ниже 100% можно дать по заранее согласованным критериям и с пояснением.')
            if agreement is not None:
                if len(agreement['tasks']) != 1 or agreement['tasks'][0]['task_id'] != 'plan':
                    raise DailyPlanError('Этот договор требует отдельной проверки заданий.')
                games = [reward for reward in agreement['tasks'][0]['rewards'] if reward['kind'] == 'game_minutes']
                if len(games) != 1 or games[0]['amount'] != plan['game_minutes']:
                    raise DailyPlanConflict('Игровое время не совпадает с согласованной наградой.')
            day = self.plans.today().isoformat()
            # Remote binding/pause is checked under its settings mutex only
            # through this small durable commit, never through Study/process work.
            with (self.approval_guard if approval_guard is None else approval_guard)():
                saved = self.plans.review(approved=True, feedback=comment, expected_revision=revision,
                    plan_date=plan_date, approval_id=request_id, quality_percent=quality_percent)
            if agreement is not None:
                state = self.rewards.snapshot()
                self.rewards.evaluate(plan_date, plan['contract_revision'], {'plan':quality_percent},
                    review_id=request_id, expected_revision=state['revision'], comment=comment)
            minutes = plan['game_minutes'] * quality_percent // 100
            cycle = self.breaks.status()
            if cycle['enabled']:
                actual_break_lease = self._study()
                self.breaks.observe_completed(self.play.status(),
                    actual_break_lease['session_id'] if actual_break_lease else None)
                cycle = self.breaks.status()
                minutes = min(minutes, 60)
                if cycle['phase'] in ('waiting','submitted','approved','blocked'):
                    return {'status':'approved_without_play', 'plan':saved, 'game_minutes':0,
                            'meaning':'hourly_break_requires_parent_review'}
            if plan_date != day or minutes == 0:
                return {'status':'approved_without_play', 'plan':saved, 'game_minutes':0,
                        'meaning':'past_day_or_no_minutes; existing restrictions preserved'}
            actual = self._study()
            actual_id = actual['session_id'] if actual else None
            if actual_id is not None and actual_id != study_id:
                return {'status':'approved_without_play', 'plan':saved, 'game_minutes':0,
                        'meaning':'newer_study_preserved; no_play_granted'}
            key = approval_key(plan_date, revision)
            previous = self.play.status()['current']
            linked = (previous['linked_study_id'] if previous and previous['approval_key'] == key
                      else actual_id)
            view = self.play.prepare(plan_date, revision, minutes, linked, current_study_id=actual_id)
            # Startup must succeed before any restriction is released. Failure
            # leaves pending_release durable and its Study untouched for retry.
            self.study.enable_startup()
            self.ensure_running(self.study)
            view = self._pump_locked()
            return {'status':'approved', 'plan':saved, 'game_minutes':minutes, 'play':view,
                    'runtime_start_requested':True, 'runtime_enforcement_verified':False}

    def pump(self):
        """Run only from the owned runtime / an authenticated approval, never a status read."""
        current = self.play.status()
        if not current['requires_startup'] and not self.breaks.status()['enabled']:
            return current
        no_links(self.root).mkdir(parents=True, exist_ok=True)
        with exclusive_lock(self.root / 'approval.lock'):
            return self._pump_locked()

    def _pump_locked(self):
        lease = self._study()
        actual_id = lease['session_id'] if lease else None
        view = self.play.reconcile(current_study_id=actual_id)
        current = view['current']
        if not current or not view['requires_startup']:
            return self._hourly_after_play_locked(view)
        if view['action'] == 'release_linked_study':
            # Ensure startup even on recovery after a crash before registration.
            self.study.enable_startup()
            if current['linked_study_id'] is not None:
                self.study.clear(current['linked_study_id'])
            lease = self._study()
            view = self.play.confirm_release(current['play_id'],
                current_study_id=lease['session_id'] if lease else None)
        if view['action'] == 'restore_study':
            self.study.enable_startup()
            raw = self.study.read()
            if raw is not None and self.study.remaining_seconds(raw) <= 0:
                self.study.clear(raw['session_id'])
            self.study.activate_if_empty(0, session_id=view['current']['return_study_id'])
            lease = self._study()
            view = self.play.confirm_return(current['play_id'],
                current_study_id=lease['session_id'] if lease else None)
        return self._hourly_after_play_locked(view)

    def _hourly_after_play_locked(self, view):
        if not self.breaks.status()['enabled']:
            return view
        actual = self._study()
        self.breaks.observe_completed(view, actual['session_id'] if actual else None)
        following = self._continue_break_locked()
        return self._pump_locked() if following is not None else view

    def _continue_break_locked(self):
        """Only a durable checked parent approval may create the next hour."""
        cycle = self.breaks.status()
        grant = cycle['grant']
        if cycle['phase'] != 'approved' or not grant:
            return None
        current = self.play.status()['current']
        if current is None:
            raise DailyPlanConflict('Исходный игровой час не найден; новый не выдан.')
        key = approval_key(grant['date'], grant['revision'])
        if current['approval_key'] == key:
            if current['duration_seconds'] != 3600 or current['linked_study_id'] != grant['linked_study_id']:
                raise DailyPlanConflict('Игровая выдача изменилась.')
            # This exact grant was prepared before a crash. Never re-prepare it
            # across midnight or reset its original deadline.
            if current['phase'] in ('pending_release','playing'):
                self.study.enable_startup()
                self.ensure_running(self.study)
            self.breaks.confirm_next(cycle['break_id'], current['play_id'])
            return current
        if current['play_id'] != cycle['current']['source_play_id']:
            raise DailyPlanConflict('Начат другой игровой сеанс. Старое одобрение не применяется.')
        actual = self._study()
        # status.grant is not authority: fresh ID/date/generation/time checks.
        checked = self.breaks.approve(cycle['break_id'], cycle['revision'],
            actual['session_id'] if actual else None)
        grant = checked['grant']
        self.study.enable_startup()
        next_view = self.play.prepare(grant['date'], grant['revision'], 60,
            grant['linked_study_id'], current_study_id=actual['session_id'] if actual else None)
        self.ensure_running(self.study)
        self.breaks.confirm_next(cycle['break_id'], next_view['current']['play_id'])
        return next_view['current']

    def cancel(self):
        """Parent cancels allowance and only its exact returned Study lease."""
        no_links(self.root).mkdir(parents=True, exist_ok=True)
        with exclusive_lock(self.root / 'approval.lock'):
            cycle = self.breaks.status()
            current = self.play.status()['current']
            if cycle['enabled'] or cycle['current'] is not None:
                self.breaks.cancel(source_play_id=current['play_id'] if current else None)
            if current and current['return_study_id'] is not None:
                self.study.clear(current['return_study_id'])
            return self.play.cancel(current['play_id']) if current else self.play.status()
