"""分区阻塞根因 API：按目标表 + 业务日期检索未齐路径、聚合根因并编排恢复。

路由分组：
- GET  /api/blocking/analyze   按目标表、日期核对每条依赖路径的源分区阻塞明细
- GET  /api/blocking/roots     只返回聚合后的根因记录（含两条路径与待齐源）
- POST /api/blocking/recovery/plan   依赖调度读取阻塞明细生成去重补写计划
- GET  /api/blocking/recovery/plans  恢复批次列表，可核对实际输入分区
- POST /api/blocking/partitions/ready  登记上游分区补齐事实
- POST /api/blocking/recheck   补齐后回查目标日，返回解除/仍阻塞的加工请求
- GET  /api/blocking/runnable  列出该日期输入已齐、可启动的日表加工
"""
from __future__ import annotations

import threading
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.engine.partition_dependency import partition_dependency_engine
from app.services import service_for
from app.store import store

router = APIRouter(prefix="/api/blocking", tags=["分区阻塞根因"])

# 全进程共享引擎单例（含阻塞快照）；计划提交用锁保证并发下同一根因只落一个写入项。
_engine = partition_dependency_engine
_plan_lock = threading.Lock()


def _require_target(body: dict[str, Any]) -> tuple[str, str]:
    target_table = str(body.get("target_table") or "").strip()
    target_partition = str(body.get("target_partition") or "").strip()
    if not target_table or not target_partition:
        raise HTTPException(status_code=400, detail="target_table 与 target_partition 为必填项")
    return target_table, target_partition


@router.get("/analyze")
def analyze_target(
    target_table: str = Query(..., description="目标表名，支持模糊匹配"),
    target_partition: str = Query(..., description="业务日期分区，例如 2026-11-18"),
    only_blocked: bool = Query(default=False, description="仅返回未齐路径"),
) -> dict[str, Any]:
    """按目标表、日期检索和筛选，定位每一条未齐路径对应的源分区阻塞明细。"""
    result = _engine.analyze(target_table, target_partition)
    if only_blocked:
        result["paths"] = [item for item in result["paths"] if not item["ready"]]
        result["path_count"] = len(result["paths"])
        result["blocked_path_count"] = len(result["paths"])
    return result


@router.get("/roots")
def root_causes(
    target_table: str = Query(...),
    target_partition: str = Query(...),
) -> dict[str, Any]:
    """返回聚合根因：每条根因保留受阻塞的多条依赖路径和仍待齐的源。"""
    analysis = _engine.analyze(target_table, target_partition)
    return {
        "target_table": analysis["target_table"],
        "target_partition": analysis["target_partition"],
        "root_cause_count": len(analysis["root_causes"]),
        "root_causes": analysis["root_causes"],
    }


@router.post("/recovery/plan")
def create_recovery_plan(body: dict[str, Any]) -> dict[str, Any]:
    """依赖调度读取源分区阻塞明细生成补写计划。

    body:
      target_table, target_partition, reason(可选),
      requested_ranges(可选): [{"source_table", "start_partition",
                                "end_partition", "partition_field"?}]
    并发提交时对同一根因（源表+分区）全局去重，只保留一个写入项。
    """
    target_table, target_partition = _require_target(body)
    ranges = body.get("requested_ranges") or []
    if not isinstance(ranges, list):
        raise HTTPException(status_code=400, detail="requested_ranges 必须是数组")
    with _plan_lock:
        # 锁内重新分析，避免两个并发请求同时看到根因未补而各写一份
        analysis = _engine.analyze(target_table, target_partition)
        plan = _engine.build_recovery_plan(
            target_table,
            target_partition,
            requested_ranges=ranges,
            reason=str(body.get("reason") or ""),
        )
    return {"ok": True, "analysis_summary": {
        "path_count": analysis["path_count"],
        "blocked_path_count": analysis["blocked_path_count"],
        "root_cause_count": len(analysis["root_causes"]),
    }, "recovery_plan": plan}


@router.get("/recovery/plans")
def list_recovery_plans(
    target_table: str | None = Query(default=None),
    target_partition: str | None = Query(default=None),
) -> dict[str, Any]:
    """恢复批次列表：核对每个批次实际包含的输入分区写入项。"""
    plans = []
    for row in store.rows("recovery_plan"):
        if target_table and target_table.casefold() not in str(row.get("目标表", "")).casefold():
            continue
        if target_partition and str(row.get("目标分区")) != target_partition:
            continue
        plans.append({
            "plan_id": row.get("id"),
            "plan_no": row.get("计划编号"),
            "target_table": row.get("目标表"),
            "target_partition": row.get("目标分区"),
            "status": row.get("计划状态"),
            "write_items": row.get("写入项", []),
            "dedup_skipped": row.get("去重跳过项", []),
            "root_causes": row.get("根因清单", []),
            "reason": row.get("补偿原因"),
            "created_at": row.get("创建时间"),
        })
    write_key_index: dict[str, str] = {}
    for plan in plans:
        for item in plan["write_items"]:
            key = f"{item.get('源表')}@{item.get('分区值')}"
            write_key_index.setdefault(key, plan["plan_no"])
    return {
        "total": len(plans),
        "plans": plans,
        # 每个根因分区当前唯一对应的恢复批次，供值班核对没有重复写入
        "write_key_owner": write_key_index,
    }


@router.post("/partitions/ready")
def mark_partition_ready(body: dict[str, Any]) -> dict[str, Any]:
    """登记上游分区补齐：只有显式标记后分区才算数据就绪，节点上线不改变就绪态。"""
    table = str(body.get("table") or "").strip()
    partition_value = str(body.get("partition_value") or "").strip()
    if not table or not partition_value:
        raise HTTPException(status_code=400, detail="table 与 partition_value 为必填项")
    return {
        "ok": True,
        "partition": _engine.mark_partition_ready(
            table, partition_value, producer=str(body.get("producer") or ""),
        ),
    }


@router.post("/recheck")
def recheck_target(body: dict[str, Any]) -> dict[str, Any]:
    """上游补齐后重新查询目标日，核对解除阻塞的加工请求与原缺失源分区。"""
    target_table, target_partition = _require_target(body)
    return _engine.recheck_blocked_requests(target_table, target_partition)


@router.get("/runnable")
def runnable_targets(target_partition: str = Query(...)) -> dict[str, Any]:
    """列出该业务日期所有输入分区齐全、可以启动日加工的目标表。"""
    items = _engine.list_runnable_targets(target_partition)
    return {"target_partition": target_partition, "total": len(items), "items": items}


@router.get("/blocking-partitions")
def blocking_partitions(target_partition: str | None = Query(default=None)) -> dict[str, Any]:
    """源分区阻塞明细清单，可按日期筛选，供恢复计划和值班排障共用。"""
    service = service_for("partition_registry")
    rows = service.list_blocking(target_partition)
    return {
        "total": len(rows),
        "items": [
            {
                "table": row.get("表名"),
                "partition_field": row.get("分区字段"),
                "partition_value": row.get("分区值"),
                "status": row.get("分区状态"),
                "producer": row.get("产出任务"),
                "reason": row.get("阻塞原因"),
            }
            for row in rows
        ],
    }
