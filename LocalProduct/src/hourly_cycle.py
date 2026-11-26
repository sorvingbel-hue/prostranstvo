"""Parent-PIN UI adapter over the shared, exact-session approval lock."""
from hourly_break import DEFAULT_TASK


class HourlyCycleControl:
    def __init__(self, approval=None):
        if approval is None:
            from board_approval import BoardApproval
            approval = BoardApproval()
        self.approval = approval

    def status(self):
        return self.approval.breaks.status()

    def configure(self, *, enabled, task, expected_revision):
        with self.approval.locked():
            play = self.approval.play.status()['current']
            ignore = (play['play_id'] if play and (not enabled or
                play['phase'] in ('completed','cancelled')) else None)
            return self.approval.breaks.configure(enabled, task, expected_revision,
                ignored_play_id=ignore)

    def request_done(self, *, break_id, expected_revision):
        with self.approval.locked():
            actual = self.approval._study()
            self.approval.breaks.observe_completed(self.approval.play.status(),
                actual['session_id'] if actual else None)
            return self.approval.breaks.request_done(break_id, expected_revision)

    def approve_break(self, *, break_id, expected_revision):
        # Caller has checked the parent PIN and still-visible ID/revision.
        with self.approval.locked():
            actual = self.approval._study()
            self.approval.breaks.approve(break_id, expected_revision,
                actual['session_id'] if actual else None)
            self.approval._continue_break_locked()
            self.approval._pump_locked()
            return self.status()
