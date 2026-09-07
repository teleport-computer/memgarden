"""七个 mutation 落到「一堆卡」上的**唯一**一份实现。

## 为什么要抽出来

两个官方 Store 各写一份的直接后果是行为漂移，而漂移不报错：
接入方在 InMemory 上测通，换 SQLite 上线，``archive`` 那条静默变成了别的语义。
sevenfloor 2026-09-06 复现的正是这个形状 —— 声明里有多个 op，
两个 Store 实际只执行其中一部分，其余抛 ``unknown op``。

所以：**声明了几个 op，这里就必须执行几个**，Store 只负责怎么存。

## 生命周期语义（七个 op 必须一致）

    add        新卡进来，active
    update     就地改字段。**不能改 id / 归属 / 生命周期状态** ——
               那三样要走专门的 op，否则「改一个字段」可以偷偷换掉卡的归属
    archive    不再参与召回，内容还在、可追溯（integrity: 历史可见）
    promote    换挂载点。**和 update 分开**：update 禁改 mount，因为那条路
               绕过授权检查
    supersede  N 张旧卡收敛成 1 张新卡，旧卡留着并指回新卡
    delete     真删。用户要求或合规，**正文不再可读**
    no_op      什么都不做，但要留痕 —— 「看过了、结论是不用改」和「漏了」
               必须分得开
"""
from __future__ import annotations

from typing import Callable

from ..storage import MutationRejected

#: ``update`` 不许碰的字段。改这些等于换一张卡，必须走专门的 op。
_IMMUTABLE = frozenset({"id", "owner", "tenant", "archived", "superseded_by",
                        "deleted",
                        # 创建来源身份只能由可信 Scope 在 add/supersede 时写入；
                        # 普通 update 若能改它，provenance 就只是可伪造的标签。
                        "source_actor", "source", "source_material_kind",
                        # 🔴 mount 也在里面。少了它就是一条**提权路径**：
                        # MountedGarden._apply 只校验 mutation 顶层的 mount，
                        # 而 changes={"mount": "family-shared"} 会绕过那道检查
                        # 直接写进卡 —— 只有 agent-private 权限的调用，
                        # 因此可以把一张私密卡提升成共享的。
                        # 提升挂载点必须走宿主授权的专门路径，不能是「改个字段」。
                        "mount"})




def apply_ops(
    staged: dict[str, dict],
    mutations: list[dict],
    *,
    new_id: Callable[[], str],
) -> list[dict]:
    """在 ``staged``（会被就地修改的卡表）上执行一批 mutation。

    调用方负责原子性：传进来的应当是一份副本，全部成功后才落回去。
    任何一条失败就抛，绝不留半成品 —— 半成功的表现是「旧卡还活着、新卡也
    活着」的双活状态，事后极难查。
    """
    results: list[dict] = []
    for m in mutations:
        op = str(m.get("op") or "add")

        if op == "add":
            card = dict(m.get("card") or {})
            # 🔴 调用方自带 id 时必须查重。
            #
            # 写入是 upsert，撞上已有 id 就是**静默覆盖掉那张旧卡** ——
            # 不报错，总数还不变。同一批里两个 add 用同一个 id 也一样，
            # 前一张凭空消失。
            #
            # 允许自带 id 本身是对的（宿主可能有自己的 ULID/业务号），
            # 但「我要新增一张」和「我要覆盖那一张」是两个意思，
            # 后者应当走 update。
            supplied = str(card.get("id") or "").strip()
            if supplied and supplied in staged:
                raise MutationRejected(
                    f"add 的 id 已存在: {supplied}（要改已有的卡请用 update）")
            if not supplied:
                card["id"] = _fresh_id(staged, new_id)
            staged[card["id"]] = card
            results.append({"id": card["id"], "status": "written"})

        elif op == "update":
            target = _target(m, "record_id")
            current = staged.get(target)
            if current is None:
                raise MutationRejected(f"update target not found: {target}")
            changes = dict(m.get("changes") or {})
            touched = _IMMUTABLE & set(changes)
            if touched:
                # 允许改 owner 就等于允许「把别人的卡改成我的」——
                # 这是一条越权路径，不是字段校验的小事。
                raise MutationRejected(
                    f"update may not change {sorted(touched)}; "
                    "归属和生命周期状态要走专门的 op")
            staged[target] = {**current, **changes}
            results.append({"id": target, "status": "updated"})

        elif op == "archive":
            target = _target(m, "record_id")
            current = staged.get(target)
            if current is None:
                raise MutationRejected(f"archive target not found: {target}")
            staged[target] = {**current, "archived": True,
                              "archive_reason": str(m.get("reason") or "")}
            results.append({"id": target, "status": "archived"})

        elif op == "supersede":
            # 一张新卡可以取代**多张**旧卡 —— 整理(merge/thicken)就是这个形状。
            targets = _targets(m)
            if not targets:
                raise MutationRejected("supersede without a target")
            for old_id in targets:
                # 一张找不到就整条失败 —— 半成功会留下双活状态。
                if old_id not in staged:
                    raise MutationRejected(f"supersede target not found: {old_id}")
            new_card = dict(m.get("card") or {})
            # 同上；而且 supersede 这里更严重：新卡 id 若等于某个 target，
            # 「旧卡归档并指向新卡」的链条会被新卡直接盖掉，
            # 历史就此消失 —— 而 supersede 的全部意义就是保住那条链。
            supplied = str(new_card.get("id") or "").strip()
            if supplied and supplied in staged:
                raise MutationRejected(
                    f"supersede 的新卡 id 已存在: {supplied}"
                    "（新卡必须是新的，否则会盖掉它要取代的历史）")
            if not supplied:
                new_card["id"] = _fresh_id(staged, new_id)
            for old_id in targets:
                staged[old_id] = {**staged[old_id],
                                  "superseded_by": new_card["id"],
                                  "archived": True}
            staged[new_card["id"]] = new_card
            results.append({
                "id": new_card["id"], "status": "superseded",
                "replaced": targets[0] if len(targets) == 1 else list(targets),
            })

        elif op == "delete":
            # 🔴 字段名是 record_id（``Delete`` dataclass 就是这么定义的）。
            # 旧实现读的是 target_id，于是走 typed 路径下来的删除**永远删不掉
            # 任何东西**，还照常回一句 status=deleted —— 用户看到「已删除」，
            # 库里原封不动。这是「绿着的假成功」最典型的一例。
            target = _target(m, "record_id")
            if target not in staged:
                raise MutationRejected(f"delete target not found: {target}")
            staged.pop(target, None)
            results.append({"id": target, "status": "deleted"})

        elif op == "promote":
            # 换挂载点。授权在上一层（MountedGarden.promote）检查过了 ——
            # 这里只负责改，因为存储层看不到 scope。
            target = _target(m, "record_id")
            current = staged.get(target)
            if current is None:
                raise MutationRejected(f"promote target not found: {target}")
            to_mount = str(m.get("to_mount") or "").strip()
            if not to_mount:
                raise MutationRejected("promote without to_mount")
            staged[target] = {**current, "mount": to_mount}
            results.append({"id": target, "status": "promoted",
                            "mount": to_mount})

        elif op == "no_op":
            results.append({"id": "", "status": "no_op",
                            "reason": str(m.get("reason") or "")})

        else:
            raise MutationRejected(f"unknown op: {op}")

    return results


def _fresh_id(staged: dict[str, dict], new_id: Callable[[], str]) -> str:
    """从 Store 分配器取得一个确实未占用的 ID，且失败必须有界。

    外部导入允许预置 ID，所以分配器的下一号可能早已存在。直接 upsert 会把
    用户的旧卡静默覆盖；无限循环则会让坏适配器卡死整个 worker。
    """
    for _attempt in range(10_000):
        candidate = str(new_id() or "").strip()
        if candidate and candidate not in staged:
            return candidate
    raise MutationRejected("store id allocator did not produce a fresh id")


def new_seed_mounts(
    mutations: list[dict], *, before: dict[str, dict] | None = None,
    staged: dict[str, dict] | None = None,
) -> list[str]:
    """返回这批新产生的非 Dream 卡所在 mount；一项代表一个 seed。"""
    out: list[str] = []
    for mutation in mutations:
        op = str(mutation.get("op") or "add")
        if op == "promote":
            target = str(mutation.get("record_id") or mutation.get("target_id")
                         or "").strip()
            destination = str(mutation.get("to_mount") or "").strip()
            prior = str(((before or {}).get(target) or {}).get("mount")
                        or "agent-private")
            after = str(((staged or {}).get(target) or {}).get("mount")
                        or "agent-private")
            if destination and after == destination and prior != after:
                out.append(destination)
            continue
        if op not in {"add", "supersede"}:
            continue
        card = dict(mutation.get("card") or {})
        if str(card.get("source") or "") == "memory_dream":
            continue
        out.append(str(mutation.get("mount") or card.get("mount")
                       or "agent-private"))
    return out


def _target(m: dict, primary: str) -> str:
    """取目标 id。兼容老调用方写的 ``target_id``，但以 typed 字段为准。"""
    for field in (primary, "target_id"):
        value = str(m.get(field) or "").strip()
        if value:
            return value
    raise MutationRejected(f"{m.get('op')} without {primary}")


def _targets(m: dict) -> list[str]:
    out: list[str] = []
    for candidate in [m.get("target_id"), *list(m.get("target_ids") or ())]:
        value = str(candidate or "").strip()
        if value and value not in out:
            out.append(value)
    return out
