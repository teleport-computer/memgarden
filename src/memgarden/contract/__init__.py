"""可供宿主运行的契约 —— 语料随包分发，装了包就能跑。

## 为什么语料要进包

内核定「什么样的输入该得什么结论」，宿主提供「证据从哪来」。两边守的必须是**同一份
语料**，否则各自跑偏 —— 而这正是把判据搬进内核想避免的事。

语料留在仓库的 ``evals/`` 里做不到这一点：宿主的 CI 上没有内核的源码仓库，那条检查
只会打印「找不到语料，跳过」，看起来绿的，其实什么都没验。**装饰性的检查比没有检查
更糟** —— 它让人以为有防护。

所以语料作为**包数据**分发。宿主 ``pip install memgarden`` 之后就能跑：

    from memgarden.contract import run_garden_language_contract
    run_garden_language_contract(my_decider)     # 返回 (通过数, 失败列表)

## 这里放什么、不放什么

只放**跨宿主都成立**的语料 —— 「给定这些桶名，该判成哪种语言」。

不放跟某个宿主绑死的东西（io 的身份卡字段名、某个 provider 的泄漏指纹）。
判断标准跟包里其它模块一样：换个宿主还成立吗？
"""
from __future__ import annotations

import json
from importlib import resources
from typing import Any, Callable

#: 宿主的判定器。收一条**证据**（语料里那几个字段），返回至少含 ``locale`` 的 dict。
#:
#: ⚠️ 证据里**没有桶名**。这是设计 —— 桶名是 AI 的输出，且大量是人名/公司名这类
#: 不携带语言信息的专有名词。详见 ``memgarden.garden_language`` 的模块说明。
Decider = Callable[[dict], dict]


def garden_language_cases() -> list[dict]:
    """语言判定的语料。每条带 ``why``，事故来的那几条还带 ``incident``。"""
    raw = resources.files(__package__).joinpath("gardens.jsonl").read_text("utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def basis_matches(got: str | None, want: str | None) -> bool:
    """依据对不对。

    依据名是闭集（``explicit_preference`` / ``writing_language`` / ``client_locale``
    / ``default``），四档语义清楚、跨宿主一致，所以精确比对。

    比对依据而不只比对结论，是因为**结论对、依据错**是最难发现的一类问题：
    碰巧两条证据指向同一种语言时，用错证据也能得出正确答案 —— 直到有一天它们不一致。
    """
    if not want:
        return True
    return got == want


def run_garden_language_contract(decider: Decider, *, echo=print) -> tuple[int, list[str]]:
    """拿宿主的判定器跑内核的语料。返回 ``(通过数, 失败的 id 列表)``。

    ``decider(evidence)`` 收一个 dict（``explicit`` / ``written`` / ``locale``），
    返回至少含 ``locale`` 的 dict；有 ``basis`` 的话会一并核对。
    """
    cases = garden_language_cases()
    fails: list[str] = []
    incidents = 0

    for c in cases:
        got = decider({
            "explicit": c.get("explicit") or None,
            "written": c.get("written") or "",
            "locale": c.get("locale") or None,
        })
        ok = got.get("locale") == c["expect"] and basis_matches(
            got.get("basis"), c.get("expect_basis")
        )
        tag = "  ⚠事故" if c.get("incident") else ""
        if ok:
            echo(f"  ✓ {c['id']:38} → {got.get('locale'):8}{tag}")
        else:
            echo(f"  ✗ {c['id']:38} → {got.get('locale')!r}（期望 {c['expect']!r}）"
                 f" 依据={got.get('basis')!r}{tag}")
            echo(f"      语料写的理由：{c['why']}")
            fails.append(c["id"])
            if c.get("incident"):
                incidents += 1

    echo(f"\n  {len(cases) - len(fails)}/{len(cases)} 通过")
    if incidents:
        echo(f"  🔴 其中 {incidents} 条是**曾经真的发生过的事故**，回归了。")
    return len(cases) - len(fails), fails
