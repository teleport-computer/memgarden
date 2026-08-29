"""存储 —— 一个 JSON 文件。**Garden 不碰它。**

真实项目里换成你的 Postgres / SQLite / 别的什么。Garden 只告诉你
「该这么改」，落库是这一层的事 —— 它也不知道你有没有加密。
"""
from __future__ import annotations

import json
import pathlib
from datetime import datetime, timezone


class JsonStore:
    def __init__(self, path: str = "memory.json") -> None:
        self.path = pathlib.Path(path)
        self.cards: dict[str, dict] = {}
        self.ledger = {"signature": "", "seed_card_count": 0}
        if self.path.exists():
            data = json.loads(self.path.read_text("utf-8"))
            self.cards = data.get("cards", {})
            self.ledger = data.get("ledger", self.ledger)

    def apply(self, mutations: list[dict]) -> list[str]:
        """执行 Garden 给的改动指令。"""
        written = []
        for m in mutations:
            op = m.get("op")
            if op == "add":
                cid = self._new_id()
                self.cards[cid] = {"id": cid, "created_at": self._now(), **m["card"]}
                written.append(cid)
            elif op == "supersede":
                cid = self._new_id()
                self.cards[cid] = {"id": cid, "created_at": self._now(), **m["card"]}
                old = m.get("target_id", "")
                if old in self.cards:
                    self.cards[old]["superseded_by"] = cid
                written.append(cid)
        self._save()
        return written

    def active(self) -> list[dict]:
        """还在用的卡。**生命周期过滤是宿主的事** —— Garden 不认识
        你的 superseded_by / is_archived 字段。"""
        return [c for c in self.cards.values() if not c.get("superseded_by")]

    def all(self) -> list[dict]:
        return list(self.cards.values())

    def remember_dream(self, signature: str, seed_count: int) -> None:
        """整理完把账本存回来 —— 没有它，每次都会重整一遍。"""
        self.ledger = {"signature": signature, "seed_card_count": seed_count}
        self._save()

    # -- 内部 --
    def _new_id(self) -> str:
        # 不用「当前总数 + 1」：删一条之后计数回退，新 id 会撞上已存在的卡。
        n = 1 + max((int(k.split("_")[1]) for k in self.cards if "_" in k), default=0)
        return f"m_{n}"

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _save(self) -> None:
        self.path.write_text(json.dumps(
            {"cards": self.cards, "ledger": self.ledger},
            ensure_ascii=False, indent=2), "utf-8")
