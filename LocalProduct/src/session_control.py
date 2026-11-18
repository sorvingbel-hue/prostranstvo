"""Owner-requested Windows session lock without logging off or killing apps."""
import ctypes
import os


def lock_session(*, backend=None):
    """Initiate Windows locking; success alone does not prove lock completion.

    The caller must be authorized by the computer owner. Unlocking is exclusively
    the normal Windows credential flow; this module neither reads nor sets it.
    ``backend`` permits an inert test double, never a tool-supplied command.
    """
    if backend is None:
        if os.name != 'nt':
            raise OSError('Блокировка сеанса поддерживается только Windows')
        backend = ctypes.WinDLL('user32', use_last_error=True).LockWorkStation
        backend.argtypes = []
        backend.restype = ctypes.c_int
    if not backend():
        raise OSError('Windows не подтвердила запрос блокировки сеанса')
    return {'action': 'lock', 'status': 'lock_requested',
            'lock_completion_verified': False, 'processes_terminated': False,
            'unlock_requires_windows_credentials': True}
