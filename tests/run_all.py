"""跑全部离线测试（不连真机、不开摄像头）：

    python tests/run_all.py          # 只显示每个测试的结果
    python tests/run_all.py -v       # 同时显示每项检查

也可以用 pytest（pip install pytest）：pytest tests
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def scripts() -> list[Path]:
    return sorted(HERE.glob("sim_*.py"))


def run(script: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def main() -> int:
    verbose = "-v" in sys.argv[1:]
    failed = []
    for script in scripts():
        start = time.monotonic()
        result = run(script)
        ok = result.returncode == 0
        print(f"{'通过' if ok else '失败'}  {script.name}  ({time.monotonic() - start:.1f} s)")
        if verbose or not ok:
            print(result.stdout)
            if result.stderr.strip():
                print(result.stderr)
        if not ok:
            failed.append(script.name)
    print()
    print("全部通过" if not failed else f"{len(failed)} 个失败：{', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
