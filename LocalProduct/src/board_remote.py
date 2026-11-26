"""Local wiring for the already-paired parent Telegram channel; no new transport."""
from board_approval import BoardApproval
from board_telegram import BoardTelegram, binding_authorized
from daily_plan import DailyPlan
from study_resume import StudyResume
from contextlib import contextmanager
from daily_plan import DailyPlanError


class BoardRemote:
    def __init__(self, store, settings, *, board=None, approval=None):
        self.store, self.settings = store, settings
        plans = DailyPlan(root=store.root.parent / 'daily-plan')
        self.board = board if board is not None else BoardTelegram(
            store.root.parent / 'board-telegram', get_plan=plans.read)
        self.approval = approval if approval is not None else BoardApproval(
            plans=plans, study=StudyResume(store.root.parent / 'study'),
            root=store.root.parent / 'board-approval')

    @contextmanager
    def _approval_guard(self, expected_binding):
        with self.settings._lock:
            if not expected_binding or not binding_authorized(self.settings, self.store, expected_binding):
                raise DailyPlanError('Родительский канал отключён или изменился.')
            yield

    def accept_message(self, **values):
        # Called by transport only under its authenticated settings lock.
        return self.board.accept_message(**values)

    def recover(self):
        # Called only after the existing sole family WorkerLock was acquired.
        return self.board.recover_inflight()

    def _approve(self, *, expected_binding, date, **values):
        if not expected_binding:
            return {'status':'denied'}
        result = self.approval.approve(plan_date=date,
            approval_guard=lambda:self._approval_guard(expected_binding), **values)
        if result.get('status') == 'approved_without_play':
            return {**result, 'status':'approved'}
        return result

    def tick(self):
        expected = {}
        def authorize(identity):
            valid = binding_authorized(self.settings, self.store, identity)
            if valid:expected['binding'] = identity
            return valid
        result = self.board.process_pending(
            lambda **values:self._approve(expected_binding=expected.get('binding'), **values),
            is_authorized=authorize)
        identity = self.store.get_binding()
        if identity and binding_authorized(self.settings, self.store, identity):
            def enqueue_reply(text, client_id):
                with self.settings._lock:
                    if not binding_authorized(self.settings, self.store, identity):
                        raise DailyPlanError('Адресат ответа изменился.')
                    return self.settings.queue_reply(self.store,text,client_id)
            self.board.flush_replies(enqueue_reply, identity)
        return result
