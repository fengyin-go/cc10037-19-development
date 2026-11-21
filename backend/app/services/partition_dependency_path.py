"""分区依赖路径领域服务：维护目标表到源表分区的多路径依赖配置。

一个目标表同一业务日期可以有多条依赖路径（例如销售日表的订单主链路与
支付核对链路），每条路径列出该路径需要全部就绪的源表清单。
分区依赖根因引擎沿这些路径逐分区核对，而不是只看直接上游节点状态。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class PartitionDependencyPathService(BaseModuleService):
    MODULE = "partition_dependency_path"
    ENTITY = "依赖路径"
    OPERATION_TERM = "配置"
    LIST_FIELDS = ["路径标识", "目标表", "目标分区", "分区字段", "源表清单", "依赖链路", "目标加工任务", "路径说明"]
    REQUIRED_FIELDS = ["路径标识", "目标表", "源表清单"]
    STATUS_ORDER = ["草稿", "活跃", "已停用"]
    # 约定与基类一致：value 是动作执行后的目标状态
    ACTION_RULES = {"启用路径": "活跃", "停用路径": "已停用"}
    NEGATIVE_ACTIONS = []

    def query_by_target(self, target_table: str, target_partition: str | None = None) -> list[dict[str, Any]]:
        """按目标表（可叠加业务日期）检索依赖路径配置。"""
        result: list[dict[str, Any]] = []
        for row in store.rows(self.MODULE):
            if target_table.casefold() not in str(row.get("目标表", "")).casefold():
                continue
            if target_partition and str(row.get("目标分区", "")).strip() not in ("", target_partition):
                continue
            result.append(dict(row))
        return result

    def source_tables_of(self, target_table: str, target_partition: str) -> list[str]:
        """汇总目标表在指定日期所有路径引用到的去重源表。"""
        tables: list[str] = []
        for row in self.query_by_target(target_table, target_partition):
            for item in str(row.get("源表清单", "")).split(","):
                name = item.strip()
                if name and name not in tables:
                    tables.append(name)
        return tables

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, msg = super().run_action(entry_id, action)
        if entry:
            entry["last_operated_at"] = datetime.now().isoformat()
        return entry, msg
