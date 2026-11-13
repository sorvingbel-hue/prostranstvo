"""Standalone UI or bounded JSON-line stdio process; no listening socket."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .service import LearningService, MAX_REQUEST_BYTES


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate field")
        value[key] = item
    return value


def serve(service, source, destination):
    while True:
        line = source.readline(MAX_REQUEST_BYTES + 2)
        if not line:
            return 0
        if len(line) > MAX_REQUEST_BYTES or not line.endswith(b"\n"):
            # Do not keep reading an unbounded hostile line.
            return 2
        try:
            request = json.loads(line.decode("utf-8"), object_pairs_hook=_object,
                                 parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite number")))
        except (ValueError, UnicodeError, RecursionError):
            request = None
        response = service.handle(request)
        body = json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(body) > 65536:
            response = service._response(response.get("request_id"), "unavailable", {"code": "response_limit", "text_ru": "Ответ превышает лимит прототипа. Выбери одно занятие.", "text_en": "Response exceeds the prototype limit. Select one session."})
            body = json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n"
        destination.write(body)
        destination.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local fraction tutor prototype")
    parser.add_argument("--stdio", action="store_true", help="Versioned JSON-line transport, no GUI")
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ProstranstvoLearning")
    parser.add_argument("--language", choices=("ru", "en"), default="ru")
    args = parser.parse_args(argv)
    try:
        service = LearningService(args.data_dir)
    except Exception:
        if args.stdio:
            return 3
        from tkinter import messagebox
        messagebox.showerror("Учебный модуль", "Не удалось открыть локальную историю. Проверь доступ к папке данных.")
        return 3
    try:
        if args.stdio:
            return serve(service, sys.stdin.buffer, sys.stdout.buffer)
        from .ui import LearningWindow
        window = LearningWindow(service, language=args.language)
        window.mainloop()
        return 0
    finally:
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
