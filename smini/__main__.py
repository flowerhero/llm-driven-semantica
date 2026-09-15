"""smini.__main__ — 允许 `python -m smini` 直接运行。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
