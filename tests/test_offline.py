"""pytest 入口：每个 sim_*.py 作为一个测试，在单独的进程里跑（它们是独立脚本）。"""

import pytest

from run_all import run, scripts


@pytest.mark.parametrize("script", scripts(), ids=lambda p: p.stem)
def test_simulation(script):
    result = run(script)
    assert result.returncode == 0, result.stdout + result.stderr
