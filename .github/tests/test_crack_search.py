# crack_search.py 的测试（审计 2026-11-18）。
#
# 跑法（在仓库根）：
#   python3 -I -B -m unittest discover -s .github/tests
# （不加 -t .：.github 不是合法的包名，加了 unittest 会报 Start directory is not importable。）
#
# 只用标准库；不联网：get 换成桩，断网也能跑。HOME、XDG_*、MIYU_* 都指到临时目录。
# 放在 .github/tests/ 而不是 tests/：打包会跳过 .github，测试不会进包。

import importlib.util
import os
import pathlib
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "crack_search.py"


def load():
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("crack_search_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Sandbox(unittest.TestCase):
    """每条测试一个临时目录，环境里去掉所有 MIYU_*，网络函数先换成直接失败的桩。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("MIYU_", "XDG_"))}
        env.update(HOME=str(root / "home"), XDG_CONFIG_HOME=str(root / "cfg"),
                   XDG_CACHE_HOME=str(root / "xdg-cache"), XDG_STATE_HOME=str(root / "xdg-state"),
                   XDG_DATA_HOME=str(root / "xdg-data"))
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = root
        self.mod = load()
        self.calls = []

        def no_network(url, headers=None):
            self.calls.append(url)
            raise AssertionError(f"测试里不该联网：{url}")

        self.mod.get = no_network


class PostgrestQuoting(Sandbox):
    # 审计 #174：isitcracked() 把 query 只做百分号编码就拼进 or=(title.ilike.…,scene_group.ilike.…)。
    # PostgREST 解析的是解码后的串，逗号、括号是它的语法符号，按它的文档要用双引号把值包起来。
    def or_clause(self, query):
        def fake_get(url, headers=None):
            self.calls.append(url)
            return "[]"

        self.mod.get = fake_get
        self.assertEqual(self.mod.isitcracked(query, 5), [])
        self.assertEqual(len(self.calls), 1, self.calls)
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(self.calls[0]).query)
        return params["or"][0]

    def test_or_values_are_double_quoted(self):
        q = "Warhammer 40,000: Space Marine (2)"
        quoted = f'"*{q}*"'
        self.assertEqual(self.or_clause(q), f"(title.ilike.{quoted},scene_group.ilike.{quoted})")


if __name__ == "__main__":
    unittest.main()
