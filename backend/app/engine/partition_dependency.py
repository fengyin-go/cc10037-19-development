"""分区依赖根因引擎：按目标表 + 业务日期追溯未齐路径，聚合源分区阻塞根因。

与 task_node.analyze_dependencies 的关键区别：
- 节点分析只看直接上游节点的「已启用」状态，节点上线不代表分区数据确已产出；
- 本引擎只承认 partition_registry 里的分区就绪事实（ready=True），
  沿 partition_dependency_path 配置的多跳路径逐分区核对，
  输出每一条未齐路径对应的源分区阻塞明细，并把同根因的多条路径聚到一条根因记录。

恢复计划（recovery）同样以「源表 + 分区」为写入单位去重：
同一个根因分区即使被多条路径、多个目标表引用，也只生成一个写入项，
避免两个范围重叠的恢复批次重复补写同一个实际分区。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.store import store

# 分区登记中视为「数据确已就绪」的状态；节点状态不参与判断。
READY_PARTITION_STATUS = "已就绪"


@dataclass
class SourcePartitionBlock:
    """单个源分区的阻塞明细。"""

    source_table: str
    partition_value: str
    partition_field: str
    status: str                      # registry 中的实际状态（缺失/未产出/...）
    ready: bool
    blocking: bool                   # 是否构成阻塞
    reason: str
    registry_id: int | None = None


@dataclass
class PathCheck:
    """一条目标表依赖路径的核对结果。"""

    path_id: str
    target_table: str
    target_partition: str
    path_chain: list[str]
    sources: list[SourcePartitionBlock]
    ready: bool                      # 路径所有源分区都齐才为 True

    @property
    def blocking_sources(self) -> list[SourcePartitionBlock]:
        return [item for item in self.sources if item.blocking]


@dataclass
class RootCause:
    """聚合后的根因：一个缺失源分区对应一条根因，保留受其阻塞的全部路径。"""

    root_key: str                    # f"{source_table}@{partition_value}"
    source_table: str
    partition_value: str
    partition_field: str
    status: str
    reason: str
    blocked_paths: list[PathCheck] = field(default_factory=list)
    # 仍待齐的源：每条路径上除本根因外其它未齐输入，按路径保留
    pending_sources_by_path: dict[str, list[SourcePartitionBlock]] = field(default_factory=dict)

    @property
    def affected_targets(self) -> list[str]:
        seen: list[str] = []
        for path in self.blocked_paths:
            if path.target_table not in seen:
                seen.append(path.target_table)
        return seen


class PartitionDependencyEngine:
    """分区依赖核对与恢复计划编排引擎，全部数据来自内存 store。"""

    def __init__(self) -> None:
        # analyze 时固化的阻塞快照，供补齐分区后的 recheck 做前后差集，
        # 从而核对「原来哪个源分区没有产出」。
        self._blocker_snapshots: dict[tuple[str, str], dict[str, list[str]]] = {}

    # ---- 基础读取 ----

    def _registry_index(self) -> dict[tuple[str, str], dict[str, Any]]:
        index: dict[tuple[str, str], dict[str, Any]] = {}
        for row in store.rows("partition_registry"):
            index[(str(row.get("表名")), str(row.get("分区值")))] = row
        return index

    def is_partition_ready(self, table: str, partition_value: str) -> bool:
        """只以分区登记事实判断就绪，不用节点/管线状态代替。"""
        row = self._registry_index().get((table, partition_value))
        if row is None:
            return False
        return str(row.get("分区状态")) == READY_PARTITION_STATUS and bool(row.get("是否就绪"))

    def _check_source(
        self,
        source_table: str,
        partition_value: str,
        partition_field: str,
        registry: dict[tuple[str, str], dict[str, Any]],
    ) -> SourcePartitionBlock:
        row = registry.get((source_table, partition_value))
        if row is None:
            return SourcePartitionBlock(
                source_table=source_table,
                partition_value=partition_value,
                partition_field=partition_field,
                status="未登记",
                ready=False,
                blocking=True,
                reason="分区登记中不存在该分区，无法证明数据已产出",
            )
        ready = str(row.get("分区状态")) == READY_PARTITION_STATUS and bool(row.get("是否就绪"))
        if ready:
            return SourcePartitionBlock(
                source_table=source_table,
                partition_value=partition_value,
                partition_field=partition_field,
                status=str(row.get("分区状态")),
                ready=True,
                blocking=False,
                reason="分区数据已就绪",
                registry_id=row.get("id"),
            )
        return SourcePartitionBlock(
            source_table=source_table,
            partition_value=partition_value,
            partition_field=partition_field,
            status=str(row.get("分区状态")),
            ready=False,
            blocking=True,
            reason=str(row.get("阻塞原因") or f"分区状态为{row.get('分区状态')}，数据未齐"),
            registry_id=row.get("id"),
        )

    # ---- 路径核对 ----

    def _paths_for(self, target_table: str, target_partition: str) -> list[dict[str, Any]]:
        result = []
        target_key = target_table.casefold()
        for row in store.rows("partition_dependency_path"):
            if target_key not in str(row.get("目标表", "")).casefold():
                continue
            if str(row.get("目标分区", "")).strip() not in ("", target_partition):
                continue
            result.append(row)
        return result

    def check_target(self, target_table: str, target_partition: str) -> list[PathCheck]:
        """核对目标表在指定日期的每一条依赖路径，返回逐路径的源分区阻塞明细。"""
        registry = self._registry_index()
        checks: list[PathCheck] = []
        for path_row in self._paths_for(target_table, target_partition):
            partition_field = str(path_row.get("分区字段", "dt"))
            sources_raw = [
                item.strip()
                for item in str(path_row.get("源表清单", "")).split(",")
                if item.strip()
            ]
            sources = [
                self._check_source(source_table, target_partition, partition_field, registry)
                for source_table in sources_raw
            ]
            chain = [
                str(part).strip()
                for part in str(path_row.get("依赖链路", "")).split("->")
                if str(part).strip()
            ]
            checks.append(PathCheck(
                path_id=str(path_row.get("路径标识")),
                target_table=str(path_row.get("目标表")),
                target_partition=target_partition,
                path_chain=chain or sources_raw,
                sources=sources,
                ready=all(item.ready for item in sources) if sources else False,
            ))
        return checks

    def aggregate_root_causes(self, checks: list[PathCheck]) -> list[RootCause]:
        """把未齐路径按「缺失源分区」聚合为根因，同根因保留多条依赖路径。"""
        roots: dict[str, RootCause] = {}
        for check in checks:
            if check.ready:
                continue
            for block in check.blocking_sources:
                key = f"{block.source_table}@{block.partition_value}"
                root = roots.get(key)
                if root is None:
                    root = RootCause(
                        root_key=key,
                        source_table=block.source_table,
                        partition_value=block.partition_value,
                        partition_field=block.partition_field,
                        status=block.status,
                        reason=block.reason,
                    )
                    roots[key] = root
                if check not in root.blocked_paths:
                    root.blocked_paths.append(check)
                others = [
                    item for item in check.blocking_sources
                    if f"{item.source_table}@{item.partition_value}" != key
                ]
                root.pending_sources_by_path.setdefault(check.path_id, [])
                existing = root.pending_sources_by_path[check.path_id]
                for item in others:
                    if f"{item.source_table}@{item.partition_value}" not in [
                        f"{e.source_table}@{e.partition_value}" for e in existing
                    ]:
                        existing.append(item)
        return list(roots.values())

    def analyze(self, target_table: str, target_partition: str) -> dict[str, Any]:
        """完整根因分析：路径明细 + 根因聚合 + 目标分区自身状态。"""
        checks = self.check_target(target_table, target_partition)
        roots = self.aggregate_root_causes(checks)
        registry = self._registry_index()
        target_row = registry.get((target_table, target_partition))
        # 固化补齐前的阻塞快照，recheck 时据此识别被解除的根因
        self._blocker_snapshots[(target_table, target_partition)] = {
            check.path_id: [
                f"{b.source_table}@{b.partition_value}"
                for b in check.blocking_sources
            ]
            for check in checks
        }
        return {
            "target_table": target_table,
            "target_partition": target_partition,
            "target_partition_status": (
                str(target_row.get("分区状态")) if target_row else "未登记"
            ),
            "path_count": len(checks),
            "blocked_path_count": sum(1 for item in checks if not item.ready),
            "all_paths_ready": all(item.ready for item in checks) if checks else False,
            "paths": [self._path_payload(item) for item in checks],
            "root_causes": [self._root_payload(item) for item in roots],
            "analyzed_at": datetime.now().isoformat(),
        }

    @staticmethod
    def _source_payload(block: SourcePartitionBlock) -> dict[str, Any]:
        return {
            "source_table": block.source_table,
            "partition_field": block.partition_field,
            "partition_value": block.partition_value,
            "status": block.status,
            "ready": block.ready,
            "blocking": block.blocking,
            "reason": block.reason,
            "registry_id": block.registry_id,
        }

    def _path_payload(self, check: PathCheck) -> dict[str, Any]:
        return {
            "path_id": check.path_id,
            "target_table": check.target_table,
            "target_partition": check.target_partition,
            "path_chain": check.path_chain,
            "ready": check.ready,
            "sources": [self._source_payload(item) for item in check.sources],
            "blocking_sources": [
                self._source_payload(item) for item in check.blocking_sources
            ],
        }

    def _root_payload(self, root: RootCause) -> dict[str, Any]:
        return {
            "root_key": root.root_key,
            "source_table": root.source_table,
            "partition_field": root.partition_field,
            "partition_value": root.partition_value,
            "status": root.status,
            "reason": root.reason,
            "blocked_path_count": len(root.blocked_paths),
            "blocked_path_ids": [item.path_id for item in root.blocked_paths],
            "affected_targets": root.affected_targets,
            # 根因记录保留两条依赖路径的完整明细
            "blocked_paths": [self._path_payload(item) for item in root.blocked_paths],
            # 每条路径上仍待齐的其它源分区
            "pending_sources_by_path": {
                path_id: [self._source_payload(item) for item in blocks]
                for path_id, blocks in root.pending_sources_by_path.items()
            },
        }

    # ---- 恢复计划：按源分区去重的写入项 ----

    def _existing_write_keys(self) -> set[str]:
        keys: set[str] = set()
        for plan in store.rows("recovery_plan"):
            if plan.get("计划状态") in ("已取消",):
                continue
            for item in plan.get("写入项", []):
                keys.add(f"{item.get('源表')}@{item.get('分区值')}")
        return keys

    def build_recovery_plan(
        self,
        target_table: str,
        target_partition: str,
        *,
        requested_ranges: list[dict[str, str]] | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """依据根因阻塞明细生成补写计划。

        并发/重复提交时，同一个根因（源表+分区）在所有未取消计划中只保留一个写入项；
        requested_ranges 用来模拟「同时发起其它范围的恢复」——若范围内分区已存在
        写入项，则只引用既有计划而不复制实际写入。
        """
        checks = self.check_target(target_table, target_partition)
        roots = self.aggregate_root_causes(checks)

        existing = self._existing_write_keys()
        write_items: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        seen: set[str] = set()

        def _add_write(source_table: str, partition_value: str, partition_field: str,
                       via_path_ids: list[str], origin: str) -> None:
            key = f"{source_table}@{partition_value}"
            if key in existing:
                skipped.append({
                    "source_table": source_table,
                    "partition_value": partition_value,
                    "reason": "该根因分区已有恢复写入项，本次不重复补写",
                    "dedup_key": key,
                })
                return
            if key in seen:
                skipped.append({
                    "source_table": source_table,
                    "partition_value": partition_value,
                    "reason": "同一提交内多路径共用根因，已合并为一个写入项",
                    "dedup_key": key,
                })
                return
            seen.add(key)
            write_items.append({
                "源表": source_table,
                "分区字段": partition_field,
                "分区值": partition_value,
                "来源根因": key,
                "关联路径": via_path_ids,
                "写入来源": origin,
            })

        # 1) 目标日根因：一个根因分区只产生一个写入项，但带上全部阻塞路径
        for root in roots:
            _add_write(
                root.source_table,
                root.partition_value,
                root.partition_field,
                [item.path_id for item in root.blocked_paths],
                origin="目标日根因",
            )

        # 2) 同批发起的其它范围恢复：命中既有写入项时只登记引用，不复制写入
        range_items: list[dict[str, Any]] = []
        for rng in requested_ranges or []:
            source_table = rng.get("source_table", "")
            partition_field = rng.get("partition_field", "dt")
            for partition_value in self._enumerate_partitions(
                rng.get("start_partition", ""), rng.get("end_partition", ""),
            ):
                range_items.append({
                    "source_table": source_table,
                    "partition_field": partition_field,
                    "partition_value": partition_value,
                })
                _add_write(
                    source_table, partition_value, partition_field,
                    via_path_ids=[], origin="附加恢复范围",
                )

        rows = store.rows("recovery_plan")
        plan_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        plan = {
            "id": plan_id,
            "status": "待执行",
            "pending": True,
            "abnormal": False,
            "计划编号": f"REC-{target_partition.replace('-', '')}-{plan_id:03d}",
            "目标表": target_table,
            "目标分区": target_partition,
            "计划状态": "待执行",
            "写入项": write_items,
            "去重跳过项": skipped,
            "附加范围": range_items,
            "根因清单": [root.root_key for root in roots],
            "补偿原因": reason or f"目标表 {target_table} 分区 {target_partition} 上游源分区未齐",
            "创建时间": datetime.now().isoformat(),
        }
        rows.append(plan)
        return {
            "plan_id": plan_id,
            "plan_no": plan["计划编号"],
            "write_item_count": len(write_items),
            "dedup_skipped_count": len(skipped),
            "write_items": write_items,
            "dedup_skipped": skipped,
            "root_causes": [self._root_payload(item) for item in roots],
            "created_at": plan["创建时间"],
        }

    @staticmethod
    def _enumerate_partitions(start: str, end: str) -> list[str]:
        from datetime import datetime as _dt, timedelta

        try:
            start_dt = _dt.strptime(start, "%Y-%m-%d")
            end_dt = _dt.strptime(end, "%Y-%m-%d")
        except ValueError:
            return [start] if start else []
        if end_dt < start_dt:
            return [start]
        out: list[str] = []
        cur = start_dt
        while cur <= end_dt:
            out.append(cur.strftime("%Y-%m-%d"))
            cur += timedelta(days=1)
        return out

    # ---- 分区补齐后的回查 ----

    def mark_partition_ready(
        self, table: str, partition_value: str, *, producer: str = ""
    ) -> dict[str, Any]:
        """登记上游分区补齐事实。只有这里能把分区置为就绪，节点上线不会改它。"""
        registry = self._registry_index()
        row = registry.get((table, partition_value))
        if row is None:
            rows = store.rows("partition_registry")
            row = {
                "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
                "表名": table,
                "分区字段": "dt",
                "分区值": partition_value,
            }
            rows.append(row)
        row["分区状态"] = READY_PARTITION_STATUS
        row["是否就绪"] = True
        row["阻塞原因"] = ""
        row["产出任务"] = producer or row.get("产出任务", "手动补齐")
        row["更新时间"] = datetime.now().isoformat()
        return {
            "table": table,
            "partition_value": partition_value,
            "ready": True,
            "updated_at": row["更新时间"],
        }

    def recheck_blocked_requests(
        self, target_table: str, target_partition: str
    ) -> dict[str, Any]:
        """上游补齐后重新核对目标日：哪些加工请求解除阻塞、原来缺的是哪个源分区。"""
        before = self._blocker_snapshots.get(
            (target_table, target_partition),
            self._snapshot_blockers(target_table, target_partition),
        )
        checks = self.check_target(target_table, target_partition)
        unlocked: list[dict[str, Any]] = []
        still_blocked: list[dict[str, Any]] = []
        new_snapshot: dict[str, list[str]] = {}

        for check in checks:
            current = [
                f"{b.source_table}@{b.partition_value}" for b in check.blocking_sources
            ]
            new_snapshot[check.path_id] = current
            previous = before.get(check.path_id, [])
            # 本次回查相对上次核对新清除的根因
            cleared = [key for key in previous if key not in current]
            payload = self._path_payload(check)
            if check.ready:
                unlocked.append({
                    "path_id": check.path_id,
                    "path_chain": check.path_chain,
                    "cleared_root_causes": cleared,
                    # 上一次核对时仍被阻塞、本次才解除的路径
                    "newly_unlocked": bool(previous),
                    "detail": payload,
                })
            else:
                still_blocked.append({
                    "path_id": check.path_id,
                    "path_chain": check.path_chain,
                    "cleared_root_causes": cleared,
                    "remaining_blockers": current,
                    "detail": payload,
                })

        # 刷新快照，下一次回查只报告新一轮补齐解除的根因
        self._blocker_snapshots[(target_table, target_partition)] = new_snapshot

        return {
            "target_table": target_table,
            "target_partition": target_partition,
            "all_paths_ready": all(item.ready for item in checks) if checks else False,
            "unlocked_paths": unlocked,
            "newly_unlocked_paths": [
                item["path_id"] for item in unlocked if item["newly_unlocked"]
            ],
            "still_blocked_paths": still_blocked,
            "rechecked_at": datetime.now().isoformat(),
        }

    def _snapshot_blockers(
        self, target_table: str, target_partition: str
    ) -> dict[str, list[str]]:
        """没有 analyze 快照时的兜底：直接按当前路径配置计算阻塞键集合。"""
        snapshot: dict[str, list[str]] = {}
        for check in self.check_target(target_table, target_partition):
            snapshot[check.path_id] = [
                f"{b.source_table}@{b.partition_value}" for b in check.blocking_sources
            ]
        return snapshot

    # ---- 输入齐全、可以启动的日表 ----

    def list_runnable_targets(self, target_partition: str) -> list[dict[str, Any]]:
        """回查所有目标表在该日期的路径，列出输入已齐、可启动日加工的目标表。"""
        target_tables: list[str] = []
        for row in store.rows("partition_dependency_path"):
            table = str(row.get("目标表", ""))
            if table and table not in target_tables:
                target_tables.append(table)

        runnable: list[dict[str, Any]] = []
        for table in target_tables:
            checks = self.check_target(table, target_partition)
            if not checks:
                continue
            if all(item.ready for item in checks):
                runnable.append({
                    "target_table": table,
                    "target_partition": target_partition,
                    "path_count": len(checks),
                    "ready_inputs": sorted({
                        f"{s.source_table}@{s.partition_value}"
                        for check in checks for s in check.sources if s.ready
                    }),
                })
        return runnable


# 全进程单例：阻塞快照保存在实例上，路由、恢复计划服务、依赖调度服务
# 必须共用同一实例，才能让一个入口 analyze 固化的快照被另一个入口 recheck 取到。
partition_dependency_engine = PartitionDependencyEngine()
