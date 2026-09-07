"""历史导出不能在 Store 读取阶段丢掉 superseded 卡。"""
import pytest

from memgarden.mounted import MountedGarden
from memgarden.service import Service
from memgarden.stores.memory import InMemoryStore
from memgarden.stores.sqlite import SqliteStore


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_history_pages_include_replaced_cards_without_leaking_scope(store_kind, tmp_path):
    store = (InMemoryStore() if store_kind == "memory"
             else SqliteStore(tmp_path / "history.db"))
    tenant, owner = "tenant", "owner"

    def add(record_id, *, target_tenant=tenant, target_owner=owner,
            mount="agent-private"):
        store.apply(target_tenant, [{"op": "add", "card": {
            "id": record_id, "summary": record_id, "content": "示例记忆正文",
            "mount": mount,
        }}], owner=target_owner, idempotency_key=f"add:{record_id}")

    for record_id in ("old", "archived", "removed"):
        add(record_id)
    add("other-owner", target_owner="other")
    add("other-tenant", target_tenant="other")
    add("other-mount", mount="family-shared")
    store.apply(tenant, [
        {"op": "supersede", "target_id": "old", "card": {
            "id": "new", "summary": "更新记忆", "content": "新的示例正文",
            "mount": "agent-private",
        }},
        {"op": "archive", "record_id": "archived"},
        {"op": "delete", "record_id": "removed"},
    ], owner=owner, idempotency_key="lifecycle")
    service = Service(MountedGarden(model=None, store=store))
    scope = {"tenant_id": tenant, "memory_owner_id": owner,
             "allowed_mounts": ["agent-private"]}

    def pages(method, **options):
        cursor, records, seen_cursors = "", [], set()
        for _ in range(10):
            response = service.handle({"id": "history", "method": method,
                "params": {"scope": scope, "limit": 1, "cursor": cursor,
                           **options}})
            assert response["ok"], response
            page = response["result"]
            items = page["items"]
            records.extend(items["records"] if method == "records.export" else items)
            cursor = page["next_cursor"]
            if not cursor:
                return records, page["total"]
            assert cursor not in seen_cursors, "pagination did not advance"
            seen_cursors.add(cursor)
        pytest.fail("history pagination did not terminate")

    # 默认导出与显式包含历史，都必须有归档卡和取代链；仍不能读其他作用域。
    for options in ({}, {"include_archived": True}):
        records, total = pages("records.export", **options)
        by_id = {card["id"]: card for card in records}
        assert total == len(records) == 3
        assert set(by_id) == {"old", "new", "archived"}
        assert by_id["old"]["superseded_by"] == "new"
        assert by_id["old"]["content"] == "示例记忆正文"

    records, total = pages("records.export", include_archived=False)
    assert total == 1
    assert [card["id"] for card in records] == ["new"]

    # Browse 的同名历史开关应使用相同读取语义，默认仍只展示有效卡。
    records, total = pages("records.browse", include_archived=True)
    assert total == len(records) == 3
    records, total = pages("records.browse")
    assert total == len(records) == 1
