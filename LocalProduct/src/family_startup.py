"""Opt-in current-user startup for family messages; unrelated to recording."""
import winreg
from pathlib import Path
from paths import APP_ROOT

KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
NAME = 'ProstranstvoLocalBetaFamily'
TARGET = APP_ROOT / 'FamilyMessageAgent.exe'
COMMAND = '"' + str(TARGET) + '"'


def startup_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as key:
            return winreg.QueryValueEx(key, NAME)[0] == COMMAND
    except FileNotFoundError:
        return False


def set_startup_enabled(enabled):
    if enabled and (not TARGET.is_file() or TARGET.is_symlink()):
        raise ValueError('Модуль общения не найден в папке приложения. Переустановите Local Beta.')
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, KEY) as key:
        try:
            previous = winreg.QueryValueEx(key, NAME)[0]
        except FileNotFoundError:
            previous = None
        if previous not in (None, COMMAND):
            raise ValueError('Имя автозапуска занято другой программой.')
        if enabled:
            winreg.SetValueEx(key, NAME, 0, winreg.REG_SZ, COMMAND)
        elif previous is not None:
            winreg.DeleteValue(key, NAME)
    return startup_enabled()
