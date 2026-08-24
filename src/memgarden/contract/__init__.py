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

Decider = Callable[[Any, tuple], dict]


def garden_language_cases() -> list[dict]:
    """语言判定的语料。每条带 ``why``，事故来的那几条还带 ``incident``。"""
    raw = resources.files(__package__).joinpath("gardens.jsonl").read_text("utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def basis_matches(got: str | None, want: str | None) -> bool:
    """依据对不对。

    ⚠️ ``fallback_N`` 里的 **N 不做严格比对**。语料里的槽位序号是内核自己的排法；
    宿主的证据链完全可能是另一套顺序。强行比对序号，测的就变成「你的取证顺序跟内核
    一样吗」—— 那不是契约该管的事，而且会逼宿主写一层假的序号映射来凑绿。

    真正要守住的是**档位**：这次结论是从桶来的、从兜底信号来的、还是纯默认值。
    尤其 ``existing_buckets`` 那档必须精确对上 —— 事故就出在那一档。
    """
    if not want:
        return True
    if want.startswith("fallback"):
        return bool(got and got.startswith("fallback"))
    return got == want


def run_garden_language_contract(decider: Decider, *, echo=print) -> tuple[int, list[str]]:
    """拿宿主的判定器跑内核的语料。返回 ``(通过数, 失败的 id 列表)``。

    ``decider(buckets, fallbacks)`` 要返回至少含 ``locale`` 的 dict；有 ``basis``
    的话会一并核对（按上面的宽严规则）。
    """
    cases = garden_language_cases()
    fails: list[str] = []
    incidents = 0

    for c in cases:
        got = decider(c.get("buckets", ""), tuple(c.get("fallbacks") or ()))
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
