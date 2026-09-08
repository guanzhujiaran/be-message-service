#!/usr/bin/env python3
"""统一 seed 脚本入口（纯 HTTP 接口调用版）。

用法（与分包前完全一致）：

    uv run python scripts/seed_cli.py                  # 全互动联调 + 大数据灌数
    uv run python scripts/seed_cli.py --skip-bulk       # 仅全互动联调
    uv run python scripts/seed_cli.py --skip-full       # 仅大数据灌数
    uv run python scripts/seed_cli.py --dry-run         # 只打印计划

实现按功能分包在 ``scripts/seed/``（CLI 与两阶段编排在 ``seed/cli.py``，
场景在 ``seed/scenarios/``，大数据灌数在 ``seed/bulk/``，RPA 在 ``seed/rpa/``）；
本文件只做 sys.path 注入与入口转发，不放业务逻辑。
整体说明见 ``scripts/seed/__init__.py`` 与计划书 §5.7。
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# 注入项目根目录（app / bili_common 可导入）
sys.path.insert(0, str(_HERE.parent))
# 注入 scripts/ 目录（seed 包可导入）
sys.path.insert(0, str(_HERE))

from seed.cli import main

if __name__ == "__main__":
    main()
