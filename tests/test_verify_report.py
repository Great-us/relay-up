#!/usr/bin/env python3
"""TASK-016G verify_report.py 安装深度探测单测（仅标准库）。

运行：
    python tests/test_verify_report.py
覆盖：浅层安装（<根>/tools/verify_report.py）与深层安装（<根>/tools/<子目录>/）
两种布局下 PROJECT_ROOT 均正确指向含 relay/ 的项目根。
"""

import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
import unittest


def load_module(root, subdir):
    """把 verify_report.py 复制到临时布局并按该位置加载。"""
    tools = os.path.join(root, subdir)
    os.makedirs(tools)
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "tools", "verify_report.py")
    dst = os.path.join(tools, "verify_report.py")
    shutil.copyfile(src, dst)
    spec = importlib.util.spec_from_file_location("verify_report_under_test", dst)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class DepthDetection(unittest.TestCase):
    def test_shallow_layout(self):
        root = tempfile.mkdtemp(prefix="vr-shallow-")
        os.makedirs(os.path.join(root, "relay"))
        mod = load_module(root, "tools")
        self.assertEqual(Path(mod.PROJECT_ROOT), Path(root))

    def test_deep_layout(self):
        root = tempfile.mkdtemp(prefix="vr-deep-")
        os.makedirs(os.path.join(root, "relay"))
        mod = load_module(root, os.path.join("tools", "task-010"))
        self.assertEqual(Path(mod.PROJECT_ROOT), Path(root))


if __name__ == "__main__":
    unittest.main(verbosity=2)
