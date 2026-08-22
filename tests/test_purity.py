"""内核的纯度：只依赖标准库和同源的 agent-protocol-core。

这条以前住在宿主 io 的仓库里（它当时有内核的源码副本）。2026-08-23 io 改成装外部
包之后，它读不到内核源码了 —— 纯度该由内核自己证明，这是它最核心的承诺：

    不 import 任何宿主模块、不碰网络、不碰数据库、不碰文件系统

一旦破了，「可独立发布 / 可被任意宿主嵌入」就都不成立。
"""
from __future__ import annotations

import ast
import pathlib
import sys

import pytest

# 跟 test_store_contract 一样自己接 sys.path：pytest 的 rootdir 处理下，
# 装进 venv 的包不一定在 import 路径上。
for _p in (
    str(pathlib.Path(__file__).resolve().parent.parent / "src"),
    str(pathlib.Path(__file__).resolve().parent.parent
        / "packages" / "agent-protocol-core" / "src"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "memgarden"
PROTOCOL = (pathlib.Path(__file__).resolve().parent.parent
            / "packages" / "agent-protocol-core" / "src" / "agent_protocol_core")

#: 唯一允许的非标准库依赖 —— 同源发布、同样是纯的。
ALLOWED_THIRD_PARTY = frozenset({"agent_protocol_core"})

#: 内核不许碰的东西。纯函数意味着：给同样的输入，永远得到同样的输出。
FORBIDDEN = frozenset({
    "socket", "http", "urllib", "requests", "httpx", "asyncio",
    "sqlite3", "psycopg", "psycopg2", "sqlalchemy",
    "subprocess", "threading", "multiprocessing",
})
#: 例外：参考存储实现本来就要用 sqlite3 和文件系统，它们是**可选**的示例，
#: 不属于判断内核。宿主用自己的存储时这些代码一行都不会跑。
STORE_EXEMPT = ("stores/",)


def _modules(path: pathlib.Path):
    for p in sorted(path.rglob("*.py")):
        yield p


def _top_level_imports(src: str) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                out.add(node.module.split(".")[0])
    return out


def test_source_tree_is_not_empty():
    """守卫本身要有牙 —— 路径写错就变成空扫，永远绿。"""
    assert len(list(_modules(SRC))) > 15
    assert len(list(_modules(PROTOCOL))) >= 2


@pytest.mark.parametrize("kind", ["kernel", "protocol"])
def test_no_host_or_io_imports(kind):
    root = SRC if kind == "kernel" else PROTOCOL
    selfname = "memgarden" if kind == "kernel" else "agent_protocol_core"
    import sys
    stdlib = set(sys.stdlib_module_names)
    offenders = []
    for f in _modules(root):
        rel = str(f.relative_to(root))
        for mod in _top_level_imports(f.read_text(encoding="utf-8")):
            if mod in stdlib or mod == selfname or mod in ALLOWED_THIRD_PARTY:
                continue
            offenders.append(f"{rel}: {mod}")
    assert not offenders, "内核 import 了不该 import 的东西：\n" + "\n".join(offenders)


def test_judgment_modules_do_no_io():
    """判断部分不许碰网络/数据库/进程/线程。stores/ 是可选的参考实现，豁免。"""
    offenders = []
    for f in _modules(SRC):
        rel = str(f.relative_to(SRC))
        if any(rel.startswith(x) for x in STORE_EXEMPT):
            continue
        hit = _top_level_imports(f.read_text(encoding="utf-8")) & FORBIDDEN
        if hit:
            offenders.append(f"{rel}: {sorted(hit)}")
    assert not offenders, "判断内核里出现了 I/O：\n" + "\n".join(offenders)


def test_protocol_core_does_not_depend_on_the_kernel():
    """依赖方向单向：内核可以用协议原语，反过来不行。"""
    for f in _modules(PROTOCOL):
        assert "memgarden" not in _top_level_imports(f.read_text(encoding="utf-8")), f.name


# --------------------------------------------------------------------------- #
# 静态守卫：包内部也不许漏传 signals
# --------------------------------------------------------------------------- #


def test_no_internal_call_drops_the_signals():
    """包内部转调闸函数时必须把 signals 传下去。

    **这条是踩出来的，而且是最贵的一次。** parse_capture_cards / 
    parse_dream_consolidations 内部调 card_text_rejection 时没传 signals，于是
    宿主传进来的识别器在**主落卡路径上被静默丢弃** —— 宿主以为设了闸，实际
    parser 用的是通用集。宿主侧再怎么扫自己的调用点也发现不了，因为漏传发生在
    包里面。

    (codex code_review 2026-08-23 抓到；宿主 io 的实测：事故串直接落成卡的摘要，
    而改动前是被拒的。)
    """
    import ast

    GUARDS = {
        "card_text_rejection", "sanitize_card_labels",
        "hard_field_pollution_reason", "field_pollution_reason",
        "bucket_pollution_reason",
    }
    DEFINITIONS = {"card_guard.py", "card_text.py"}
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name in DEFINITIONS:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (fn.attr if isinstance(fn, ast.Attribute)
                    else fn.id if isinstance(fn, ast.Name) else None)
            if name in GUARDS and "signals" not in ast.unparse(node):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno} {name}")
    assert not offenders, (
        "这些内部调用没把 signals 传下去 —— 宿主的识别器会在这里被静默丢弃：\n  "
        + "\n  ".join(offenders)
    )


def test_host_signals_actually_reach_the_capture_parser():
    """行为断言：宿主传进来的识别器，必须真的作用到落卡结果上。

    上面那条是静态的（防结构回归），这条是行为的（防「传了但没生效」）。
    两条都要 —— 静态那条挡不住「传了个参数但内部又覆盖掉」。
    """
    import re

    from memgarden.prompts.capture import parse_capture_cards
    from memgarden.text.leak_signals import GENERIC_SIGNALS, LeakSignals, combine

    marker = re.compile(r"\bto=functions\.\w+")
    host = combine(
        GENERIC_SIGNALS,
        LeakSignals(strong=(lambda t: "host_marker" if marker.search(t) else None,)),
    )
    raw = (
        '{"cards":[{"action":"add","type":"fact",'
        '"summary":"analysis to=functions.memory_write",'
        '"content":"a perfectly normal body of text goes here",'
        '"bucket":"Work","threads":["overtime"],"importance":0.5,"pulse":0.3}]}'
    )
    cards, err = parse_capture_cards(raw, policy="conversation_capture",
                                     strict=False, signals=host)
    assert not cards and err, "宿主的识别器没作用到 parser 上"
