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


#: 外壳包 —— **做 I/O 正是它们的职责**，所以不受判断层的无 I/O 约束。
#: 但它们受另一条更严的约束：不许自己实现判断（见 test_cli / test_mcp 里的守卫）。
SHELL_EXEMPT = ("cli/", "mcp/")


def test_judgment_modules_do_no_io():
    """判断部分不许碰网络/数据库/进程/线程。

    两类豁免，理由不同：
      ``stores/``     可选的参考实现，本来就是存储
      ``cli/`` ``mcp/`` 外壳，读文件、起子进程正是它们存在的意义

    豁免的不是「随便干什么」—— 外壳受另一条更严的约束：
    **不许自己实现判断**，只许调 GardenComponent（test_cli / test_mcp 守着）。
    """
    offenders = []
    for f in _modules(SRC):
        rel = str(f.relative_to(SRC))
        if any(rel.startswith(x) for x in STORE_EXEMPT + SHELL_EXEMPT):
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


# --------------------------------------------------------------------------- #
# 提示词语言：英文花园里不该冒出中文
# --------------------------------------------------------------------------- #

#: 英文提示词里**允许**出现的 CJK —— 它们是给模型看的**反例**，翻译了就教错：
#:   "Health/健康"   演示什么叫「双语斜杠串」（禁止的写法）
#:   「宠物」/"pets"  演示「跟着素材语言走」是什么意思（导入那条线用）
#:   「让我更新名字」 演示英文用户的思考滑进中文（self-thinking 坏例子）
_ALLOWED_CJK_IN_ENGLISH = {"健康", "宠物", "让我更新名字"}


def test_english_garden_prompts_carry_no_stray_chinese():
    """英文花园的提示词里不许有中文说明。

    **这是踩出来的。** 指令翻成英文之后，模板里还散着中文的举例、兜底占位符、
    和整段中文的「别叫用户」说明 —— 对英文用户毫无意义，而且是**混合语言信号**：
    实测最容易让模型顺着把卡也写成中文。

    白名单里的字面量都是反例，必须留中文才讲得清；除此之外一个都不该有。
    """
    import re

    from memgarden.prompts.capture import build_capture_prompt
    from memgarden.prompts.dream import build_dream_prompt
    from memgarden.prompts.migrate import build_migrate_prompt
    from agent_protocol_core import self_thinking

    prompts = {
        "capture": build_capture_prompt(
            ai_name="io", user_name="Alex", naming_rule=None, buckets="", threads="",
            identity="", window="hi", cards="", locale="en",
        ),
        "dream": build_dream_prompt(
            ai_name="io", user_name="Alex", cards="", recent_conversations="", locale="en",
        ),
        "migrate": build_migrate_prompt(
            ai_name="io", user_name="Alex", old_cards="c1", vocab="", locale="en",
        ),
        "self_thinking": self_thinking.instruction_for_language("en"),
    }
    leaked = {}
    for name, text in prompts.items():
        runs = set(re.findall(r"[一-鿿]+", text)) - _ALLOWED_CJK_IN_ENGLISH
        if runs:
            leaked[name] = sorted(runs)
    assert not leaked, f"英文提示词里漏了中文：{leaked}"


def test_chinese_garden_still_gets_chinese_bucket_names():
    """反过来也要守住：中文花园的桶名必须还是中文，不能被一起翻掉。"""
    from memgarden.prompts.capture import build_capture_prompt
    from memgarden.prompts.buckets import BUCKET_SETS

    text = build_capture_prompt(
        ai_name="io", user_name="老王", naming_rule=None, buckets="", threads="",
        identity="", window="hi", cards="", locale="zh-Hans",
    )
    assert BUCKET_SETS["zh-Hans"] in text, "中文花园拿不到中文桶名了"
    assert BUCKET_SETS["en"] not in text, "英文桶清单又被塞进中文花园"


def test_no_host_specific_env_var_names_in_the_kernel():
    """公共包里不许**读取**产品专属的环境变量名。

    别人装了 `pip install memgarden`，要调一个阈值得去设 `FEEDLING_DREAM_FUSE_RATIO`
    —— 而他们根本不知道 Feedling 是什么。这跟写死中文桶名、写死宿主的枚举
    是同一类问题：**通用规则里夹带了宿主专有的具体值。**

    sevenfloor 评审 §11 指出：原来的纯净度测试只查 import 和少量文本模式，
    拦不住这一类。所以补上。

    ⚠️ **只查真正读 env 的字符串，不查文档和注释。**
    第一版是全文搜前缀，结果把「旧名已弃用」这句说明也判成违规 ——
    那会逼着大家把弃用说明删掉，而那正是别人最需要看到的一句话。
    守卫应该拦住行为，不该拦住解释行为的文字。

    唯一豁免 ``config.py`` —— 它刻意保留旧前缀的读取以免「设了不生效」。
    """
    import ast
    import pathlib as _p

    HOST_PREFIXES = ("FEEDLING_", "IO_", "PHALA_")
    root = _p.Path(__file__).resolve().parent.parent / "src" / "memgarden"
    offenders = []

    for f in root.rglob("*.py"):
        if f.name == "config.py":
            continue
        tree = ast.parse(f.read_text("utf-8"))
        # 文档字符串单独收集，扫描时跳过它们。
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docstrings.add(doc)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if node.value in docstrings:
                continue
            for prefix in HOST_PREFIXES:
                if node.value.startswith(prefix):
                    offenders.append(f"{f.relative_to(root)}:{node.lineno} {node.value}")

    assert not offenders, (
        "内核里读了宿主专属的环境变量，应该走 config.py 的 MEMGARDEN_* 命名空间："
        + ", ".join(offenders)
    )


# --------------------------------------------------------------------------- #
# 接入面：陌生 Runtime 不该需要认识内部模块
# --------------------------------------------------------------------------- #

def test_the_ten_minute_example_never_reaches_into_internals():
    """「十分钟接入」的示例只许 import 顶层。

    它是这个包**对外承诺的样子**：接入方要认识的东西越少，
    Garden 内部越能自由改。示例一旦开始深挖 ``prompts.capture``，
    抄它的人就会照做 —— 而那正是宿主 io 现在的处境
    （23 个文件、15 个子模块、约 30 个符号直接依赖内部路径）。

    例外：``memgarden.selection`` 的挑卡段是**公开插口**，
    换挑卡策略本来就要用它们。
    """
    import ast
    import pathlib

    example = pathlib.Path(__file__).resolve().parent.parent / "examples" / "mount_in_ten_minutes.py"
    tree = ast.parse(example.read_text("utf-8"))

    ALLOWED_SUBMODULES = {"memgarden.selection"}
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("memgarden"):
            if node.module != "memgarden" and node.module not in ALLOWED_SUBMODULES:
                offenders.append(node.module)
    assert not offenders, (
        f"示例深挖了内部模块 {offenders} —— 抄它的人会照做。"
        f"该用的是 from memgarden import GardenComponent, ..."
    )


def test_the_top_level_actually_exports_something():
    """v0.1.0 到 v0.2.0 四个版本，``__init__.py`` 的导出一直是空的 ——
    宿主想用只能直接 import 内部模块。这条守住它不再退回去。"""
    import memgarden

    assert getattr(memgarden, "__all__", None), "顶层没有导出任何东西"
    assert "GardenComponent" in memgarden.__all__


def test_the_readme_install_command_matches_how_we_actually_publish():
    """README 教的装法必须真的能用。

    这条是拿实际问题换来的：README 一度写着 ``pip install memgarden``，
    而这个包当时**根本没发到 PyPI**（连它依赖的 agent-protocol-core 也没有）。
    照着做的人第一步就失败 —— 而这恰好是别人对这个项目的第一印象。

    0.12.2 起两个包都在 PyPI 上了，所以现在断言的是正过来的那一面：
    README 必须教 ``pip install memgarden``，别再退回 Release wheel 的
    长 URL（那串带死版本号，改版本就烂）。
    """
    import pathlib
    import re

    readme = (pathlib.Path(__file__).resolve().parent.parent / "README.md").read_text("utf-8")
    install_block = re.search(r"```bash\n(.*?)```", readme, re.S)
    assert install_block, "README 里没有安装说明"
    body = install_block.group(1)

    assert "pip install memgarden" in body, (
        "两个包都在 PyPI 上了，README 的第一条装法就该是 pip install memgarden"
    )
    assert "releases/latest/download" not in body, (
        "别把 Release wheel 的长 URL 当主装法 —— 里面带死版本号，改版本就烂"
    )


def test_the_demo_agent_only_touches_the_top_level():
    """demo agent 是「陌生 Runtime 接得上」的活证明 —— 它一旦开始深挖内部模块，
    这个证明就作废了，而且抄它的人会照做。

    例外和「十分钟接入」那条一样：``memgarden.selection`` 的挑卡段是公开插口。
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "examples" / "demo-agent"
    allowed = {"memgarden", "memgarden.selection"}
    offenders = []
    for f in sorted(root.glob("*.py")):
        for node in ast.walk(ast.parse(f.read_text("utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("memgarden"):
                if node.module not in allowed:
                    offenders.append(f"{f.name}: {node.module}")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("memgarden") and a.name not in allowed:
                        offenders.append(f"{f.name}: {a.name}")
    assert not offenders, f"demo agent 深挖了内部模块：{offenders}"


def test_the_demo_agent_holds_the_api_key_not_the_kernel():
    """key 必须在 demo 自己手里 —— 这正是「模型由宿主提供」的意思。

    ⚠️ 查的是**代码**，不是文本。第一版按字符串扫，被三处误报打回：
    ``OpenAI`` 出现在「专有名词桶名不该当语言证据」的举例里，
    ``deepseek`` 出现在一次事故的说明里 —— 那些是文档，不是依赖。
    按文本扫会逼着后来的人把有用的注释删掉来凑绿，那是反效果。
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "src" / "memgarden"
    PROVIDER_SDKS = {"openai", "anthropic", "httpx", "requests", "urllib"}
    KEYISH = ("api_key", "apikey", "_token", "secret")

    offenders = []
    for f in root.rglob("*.py"):
        tree = ast.parse(f.read_text("utf-8"))
        for node in ast.walk(tree):
            # ① import 了某家 provider 的 SDK 或 HTTP 客户端
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] in PROVIDER_SDKS:
                        offenders.append(f"{f.name}: import {a.name}")
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                if (node.module or "").split(".")[0] in PROVIDER_SDKS:
                    offenders.append(f"{f.name}: from {node.module}")
            # ② 读了看起来像凭据的环境变量
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                low = node.value.lower()
                if low.startswith(("sk-", "sk_")) or (
                    any(k in low for k in KEYISH) and node.value.isupper()
                ):
                    offenders.append(f"{f.name}: 常量 {node.value!r}")

    assert not offenders, (
        "内核碰了模型凭据或某家 provider 的 SDK：\n" + "\n".join(offenders)
    )


def test_no_host_user_identifiers_leak_into_the_public_package():
    """公开包里不许出现真实用户 id。

    这个包是 `pip install` 装的、源码任何人可读。把事故编号写成宿主的用户
    标识（``usr_`` 前缀 + 十六进制）等于把它带进公共分发物 —— 就算截断了
    也不该。引用事故用「日期 + 症状」，一样可追溯，还更好读。

    （这条守卫扫的是字面形状，所以本文件里也不能出现那个形状的例子。）
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    pattern = re.compile(r"usr_[0-9a-f]{4,}")
    offenders = []
    for path in list(root.glob("src/**/*.py")) + list(root.glob("tests/**/*.py")) \
            + list(root.glob("examples/**/*.py")) + list(root.glob("*.md")):
        for i, line in enumerate(path.read_text("utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(root)}:{i}")
    assert not offenders, (
        "这些地方带着宿主的用户 id —— 换成「日期 + 症状」：\n  " + "\n  ".join(offenders)
    )


def test_the_kernel_reads_no_host_specific_env_prefix():
    """内核只认 ``MEMGARDEN_*``，不许回退到宿主专属的前缀。

    曾经为了稳妥保留过 ``FEEDLING_`` 回退。它违反公共包的边界要求，而且
    核实过宿主一处都没设 —— 留着不解决任何问题。宿主要沿用旧名，在自己
    那边读 env、把值当参数传进来。
    """
    import pathlib
    import re

    src = (pathlib.Path(__file__).resolve().parent.parent / "src").rglob("*.py")
    offenders = []
    for path in src:
        for i, line in enumerate(path.read_text("utf-8").splitlines(), 1):
            stripped = line.strip()
            # 只看**代码**，注释里解释「为什么不用它」是应该保留的
            if stripped.startswith("#") or stripped.startswith('"'):
                continue
            if re.search(r'["\']FEEDLING_?', line):
                offenders.append(f"{path.name}:{i} {stripped[:70]}")
    assert not offenders, "内核代码里出现了宿主专属前缀：\n  " + "\n  ".join(offenders)
