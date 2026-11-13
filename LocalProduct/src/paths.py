"""Portable per-user locations. Importing this module never creates files."""
import ctypes
import os
from pathlib import Path
import sys
import uuid

APP_ROOT = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
BASE_ROOT = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local') / 'ProstranstvoLocalBeta'
DATA_ROOT = BASE_ROOT / 'data'
DEPLOY_ROOT = BASE_ROOT
SOURCE_ROOT = APP_ROOT
EXECUTOR_DATA = DATA_ROOT / 'executor'
MAINTENANCE_ROOT = DATA_ROOT / 'maintenance'
CATALOG_PATH = DATA_ROOT / 'catalog.sqlite'

def known_folder(identifier, fallback):
    if os.name == 'nt':
        pointer = ctypes.c_void_p()
        guid = (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(identifier).bytes_le)
        shell = ctypes.WinDLL('shell32')
        shell.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        shell.SHGetKnownFolderPath.restype = ctypes.c_long
        if shell.SHGetKnownFolderPath(ctypes.byref(guid), 0x4000, None, ctypes.byref(pointer)) == 0:
            try:
                return Path(ctypes.wstring_at(pointer))
            finally:
                ctypes.windll.ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
                ctypes.windll.ole32.CoTaskMemFree(pointer)
    return Path.home() / fallback

DEFAULT_ALLOWED_ROOTS = [known_folder('374DE290-123F-4565-9164-39C4925E467B', 'Downloads'),
                         known_folder('FDD39AD0-238F-46AF-ADB4-6C85480369C7', 'Documents')]
DEFAULT_BLOCKED_ROOTS = [APP_ROOT, DATA_ROOT, Path.home() / '.codex', Path.home() / '.ssh', Path.home() / '.aws']
