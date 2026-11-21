"""恢复计划领域服务：管理按源分区阻塞明细生成的补写计划与并发去重。

核心约束：
- 写入项以「源表 + 分区值」为唯一键，同一个根因日期在所有未取消计划中
  只能存在一个写入项，多路径/多目标表共用根因时合并而不是重复补写；
- 计划的生成依据是分区就绪登记的实际状态，而非节点是否上线。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.engine.partition_dependency import partition_dependency_engine
from app.services.base import BaseModuleService
from app.store import store


class RecoveryPlanService(BaseModuleService):
    MODULE = "recovery_plan"
    ENTITY = "恢复计划"
    OPERATION_TERM = "调度"
    # 恢复计划含嵌套写入项，列表页展示关键标量字段；写入项明细走专用接口。
    LIST_FIELDS = ["计划编号", "目标表", "目标分区", "计划状态", "根因清单", "补偿原因", "创建时间"]
    REQUIRED_FIELDS = ["目标表", "目标分区"]
    STATUS_ORDER = ["待执行", "执行中", "已完成", "已取消"]
    # 约定与基类一致：value 是动作执行后的目标状态
    ACTION_RULES = {"开始补写": "执行中", "完成补写": "已完成", "取消计划": "已取消"}
    NEGATIVE_ACTIONS = ["取消计划"]

    def __init__(self) -> None:
        # 共享全进程引擎单例，保证阻塞快照与路由/调度服务互通
        self.engine = partition_dependency_engine

    # 列表/导出需要把嵌套结构拍平成标量，避免表格里出现 dict/list。
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = []
        for row in store.rows(self.MODULE):
            flat = dict(row)
            flat["根因清单"] = ",".join(row.get("根因清单", []))
            flat["写入项数"] = len(row.get("写入项", []))
            rows.append(flat)
        for field, expected in (filters or {}).items():
            if field in self.LIST_FIELDS:
                rows = [
                    row for row in rows
                    if expected.casefold() in str(row.get(field, "")).casefold()
                ]
        if keyword:
            rows = [
                row for row in rows
                if keyword.casefold() in str(row.get("计划编号", "")).casefold()
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def generate_plan(
        self,
        target_table: str,
        target_partition: str,
        *,
        requested_ranges: list[dict[str, str]] | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """依赖调度入口：读取源分区阻塞明细并生成去重后的补写计划。"""
        return self.engine.build_recovery_plan(
            target_table,
            target_partition,
            requested_ranges=requested_ranges,
            reason=reason,
        )

    def get_detail(self, plan_id: int) -> dict[str, Any] | None:
        return store.find(self.MODULE, plan_id)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, msg = super().run_action(entry_id, action)
        if entry:
            entry["计划状态"] = entry["status"]
            entry["last_operated_at"] = datetime.now().isoformat()
        return entry, msg
