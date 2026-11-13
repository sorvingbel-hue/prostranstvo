"""Source launcher. No effects until explicitly run."""
from pathlib import Path
import sys

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    from learning_module.__main__ import main
    raise SystemExit(main())
