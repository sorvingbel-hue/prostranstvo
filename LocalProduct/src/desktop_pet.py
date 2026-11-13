"""Launch only our visible local desktop kitten. No AI, network or startup changes."""
import os
from pathlib import Path
import struct
import subprocess
import sys

from activity_core import no_links

APP_ROOT = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent


def checked_assets(root=None):
    root = Path(root if root is not None else APP_ROOT).absolute()
    executable = root / 'NativeDesktopPet.exe'
    image = root / 'assets' / 'kitten-sprites-v1.png'
    for path in (executable, image):
        no_links(path, True)
        if not path.is_file():
            raise ValueError('Файлы котёнка отсутствуют. Установите полную сборку приложения.')
    if not 1024 <= image.stat().st_size <= 20 * 1024**2:
        raise ValueError('Файл анимации котёнка повреждён.')
    with image.open('rb') as stream:
        header = stream.read(24)
    if len(header) != 24 or header[:8] != b'\x89PNG\r\n\x1a\n' or header[12:16] != b'IHDR':
        raise ValueError('Файл анимации котёнка повреждён.')
    width, height = struct.unpack('>II', header[16:24])
    if not 512 <= width <= 4096 or not 512 <= height <= 4096 or width != height:
        raise ValueError('Неподдерживаемый размер анимации котёнка.')
    return executable


def launch(root=None):
    if os.name != 'nt':
        raise ValueError('Настольный котёнок работает в Windows.')
    executable = checked_assets(root)
    try:
        return subprocess.Popen([str(executable)], cwd=executable.parent,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError:
        raise RuntimeError('Windows не смогла открыть котёнка. Проверьте установку приложения.') from None


def main(argv=None):
    if list(sys.argv[1:] if argv is None else argv):
        raise ValueError('Неизвестный режим котёнка.')
    return launch().wait()


if __name__ == '__main__':
    raise SystemExit(main())
