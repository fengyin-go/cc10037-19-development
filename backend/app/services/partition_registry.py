"""分区就绪登记领域服务：管理每个表分区的实际产出状态。

这是「分区数据确已就绪」的唯一事实来源：
节点已启用 / 管线已上线都不能代替分区就绪标记，
分区依赖根因引擎只承认本表中 分区状态=已就绪 且 是否就绪=True 的记录。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class PartitionRegistryService(BaseModuleService):
    MODULE = "partition_registry"
    ENTITY = "分区登记"
    OPERATION_TERM = "登记"
    LIST_FIELDS = ["表名", "分区字段", "分区值", "分区状态", "是否就绪", "产出任务", "阻塞原因", "更新时间"]
    REQUIRED_FIELDS = ["表名", "分区值", "分区状态"]
    STATUS_ORDER = ["缺失", "未产出", "产出中", "已就绪"]
    # 约定与基类一致：value 是动作执行后的目标状态
    ACTION_RULES = {"标记产出中": "产出中", "确认就绪": "已就绪", "确认就绪-缺失补登": "已就绪"}
    NEGATIVE_ACTIONS = []

    def query_by_table(self, table_name: str) -> list[dict[str, Any]]:
        """按表名检索分区登记，供值班人员核对该表各分区产出情况。"""
        result: list[dict[str, Any]] = []
        for row in store.rows(self.MODULE):
            if table_name.casefold() in str(row.get("表名", "")).casefold():
                result.append(dict(row))
        return result

    def get_partition(self, table_name: str, partition_value: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if row.get("表名") == table_name and str(row.get("分区值")) == partition_value:
                return row
        return None

    def is_ready(self, table_name: str, partition_value: str) -> bool:
        """分区就绪判断：只看分区登记事实，不参考任何节点状态。"""
        row = self.get_partition(table_name, partition_value)
        if row is None:
            return False
        return str(row.get("分区状态")) == "已就绪" and bool(row.get("是否就绪"))

    def list_blocking(self, partition_value: str | None = None) -> list[dict[str, Any]]:
        """列出未就绪（阻塞）分区，可按业务日期过滤。"""
        result = []
        for row in store.rows(self.MODULE):
            if not bool(row.get("是否就绪")):
                if partition_value and str(row.get("分区值")) != partition_value:
                    continue
                result.append(dict(row))
        return result

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, msg = super().run_action(entry_id, action)
        if entry:
            entry["last_operated_at"] = datetime.now().isoformat()
            if entry.get("status") == "已就绪":
                entry["分区状态"] = "已就绪"
                entry["是否就绪"] = True
                entry["阻塞原因"] = ""
                entry["更新时间"] = entry["last_operated_at"]
        return entry, msg
