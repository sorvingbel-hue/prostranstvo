"""User-requested mode switch; a closed visible agent can be started from the UI."""
import subprocess,time
from paths import APP_ROOT
TARGET=APP_ROOT/'ActivityAgent.exe'
def set_mode(store,enabled,start_timeout=12):
 if not enabled:return store.set_config(enabled=False)
 if not store.status().get('running'):
  if not TARGET.is_file() or TARGET.is_symlink():raise RuntimeError('Модуль записи не найден в папке приложения. Переустановите Local Beta.')
  subprocess.Popen([str(TARGET)],cwd=TARGET.parent,creationflags=subprocess.CREATE_NO_WINDOW)
  deadline=time.monotonic()+start_timeout
  while time.monotonic()<deadline:
   if store.status().get('running'):break
   time.sleep(.15)
  else:raise RuntimeError('Модуль не ответил. Запись осталась выключенной.')
 return store.set_config(enabled=True)
