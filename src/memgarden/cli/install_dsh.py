"""``memgarden install-dsh`` —— 把 Adapter 装进一个 DSH profile。

## 为什么是 Python CLI 而不是 npm 包

见 :mod:`memgarden.adapters` 的说明：Adapter 和这个包共用同一套 wire 协议，
拆成两个版本号会让用户装出我们没测过的组合。

用户本来就要 ``pip install memgarden``（Adapter 靠它跑 ``memgarden serve``），
所以装 Adapter 这一步挂在同一个命令行工具下，是零额外成本。
"""
from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

from ..adapters.dsh import HERE as _ADAPTER_DIR

def _own_bin() -> str:
    """跑这条命令的那个解释器旁边的 memgarden。

    取不到就退回 PATH 上的名字 —— 那时至少配置是可读的，而不是一个
    指向别的安装的绝对路径。
    """
    import sys

    candidate = Path(sys.executable).parent / "memgarden"
    if candidate.exists():
        return str(candidate)
    return shutil.which("memgarden") or "memgarden"


_ENTRY = """- insert:
    - id: memgarden
      name: 'dsh-memgarden'
      inject: [tools, llm]
      config:
        bin: '{bin}'
        storage: 'sqlite:///{storage}'
        tenant: '{tenant}'
        memoryOwner: '{owner}'
        locale: '{locale}'
        stateDir: '{state_dir}'
"""


def add_parser(sub) -> None:
    p = sub.add_parser(
        "install-dsh",
        help="把 Memory Garden 装进一个 DeepSeek Harness profile")
    p.add_argument("--dsh-home", default=os.environ.get("DSH_HOME", ""),
                   help="DSH_HOME 目录（默认读环境变量）")
    p.add_argument("--profile", default="sdk-minimal")
    p.add_argument("--tenant", required=True,
                   help="账户 / 组织 / 部署的安全边界")
    # 🔴 没有默认值是有意的：随便给一个会让同一个租户下的不同用户共用
    # 一座花园，而且不报错。
    p.add_argument("--owner", required=True,
                   help="这座花园的稳定所有者（必填，不能省）")
    # 🔴 默认指向**正在跑这条命令的那个 memgarden**，不是 PATH 上的。
    #
    # 用 shutil.which() 找出来的可能是系统上另一个（更旧的）安装 ——
    # 实测踩到过：装了新 wheel 的 venv 里跑 install-dsh，写进配置的却是
    # /opt/homebrew/bin/memgarden，那一版还没有 `serve` 子命令。
    # 症状是 dsh 起来了、对话正常、记忆一条都不进，日志里才有
    # 「invalid choice: 'serve'」—— 用户根本不会去看那个日志。
    p.add_argument("--bin", default=_own_bin())
    p.add_argument("--storage", default="")
    p.add_argument("--locale", default="zh-Hans")
    p.add_argument("--state-dir", default="",
                   help="落卡待办本放哪（崩溃后靠它把那一轮补回来）")
    p.set_defaults(func=run)


def run(args) -> int:
    home = Path(args.dsh_home).expanduser().resolve() if args.dsh_home else None
    if not home:
        print("缺少 --dsh-home（或环境变量 DSH_HOME）")
        return 2

    profile_dir = home / "profiles" / args.profile
    if not profile_dir.is_dir():
        print(f"找不到 profile 目录：{profile_dir}\n"
              f"先初始化一次：DSH_HOME={home} npx dsh "
              f"--profile {args.profile} --dump-default-config")
        return 2

    # 把随包发布的 Adapter 拷进 profile 的 node_modules。
    # 拷贝而不是 symlink：symlink 指向 site-packages，用户升级 / 重建 venv
    # 之后会变成断链，而断链的表现是 dsh 启动时报「找不到包」——
    # 看不出跟升级有关。
    target = profile_dir / "node_modules" / "dsh-memgarden"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("plugin.mjs", "package.json"):
        shutil.copyfile(_ADAPTER_DIR / name, target / name)

    storage = args.storage or str(home / "memgarden.db")
    state_dir = args.state_dir or str(home / "memgarden-state")
    patch = profile_dir / "cordis.patch.yml"
    existing = patch.read_text("utf-8") if patch.exists() else ""
    if "id: memgarden" in existing:
        print(f"ℹ️  {patch} 里已经有 memgarden 了，只更新了插件文件。")
    else:
        # ⚠️ dsh 生成的默认 patch 文件里有一个空数组字面量 `[]`。直接追加会得到
        # 「一个文档里既有 flow 序列又有 block 序列」的非法 YAML，dsh 启动时
        # 报 "end of the stream or a document separator is expected" ——
        # 完全看不出跟装记忆插件有关。所以先把那个占位删掉。
        cleaned = "\n".join(line for line in existing.splitlines()
                            if line.strip() != "[]").rstrip()
        patch.write_text((cleaned + "\n" if cleaned else "")
                         + _ENTRY.format(bin=args.bin, storage=storage,
                                         tenant=args.tenant, owner=args.owner,
                                         locale=args.locale,
                                         state_dir=state_dir),
                         encoding="utf-8")
        print(f"✅ 已写入 {patch}")
    print(f"✅ 插件已装到 {target}")
    print(f"\n下一步：照常起 dsh。记忆库在 {storage}")
    return 0
