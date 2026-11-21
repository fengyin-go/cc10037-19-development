"""依赖调度领域服务：管理管线调度配置、触发检测和执行记录。

上游：pipeline_def 产出管线 → depend_scheduler 编排调度
消费：watermark_tracker 消费调度事件推进水位
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class DependSchedulerService(BaseModuleService):
    MODULE = "depend_scheduler"
    ENTITY = "调度配置"
    OPERATION_TERM = "调度"
    LIST_FIELDS = ["配置名称", "目标管线", "触发方式", "上游依赖", "重试延迟", "最大重试次数", "告警策略"]
    REQUIRED_FIELDS = ["配置名称", "目标管线", "触发方式"]
    STATUS_ORDER = ["草稿", "已调度", "执行中", "已暂停", "已失败"]
    ACTION_RULES = {"启动调度": "草稿", "暂停调度": "已调度", "标记失败": "执行中"}
    NEGATIVE_ACTIONS = []

    def query_by_pipeline(self, pipeline_name: str) -> list[dict[str, Any]]:
        """按目标管线查询调度配置。

        从 store 中筛选指定管线的调度记录，
        返回配置列表供排产逻辑消费。
        """
        result: list[dict[str, Any]] = []
        for row in store.rows(self.MODULE):
            if pipeline_name.casefold() in str(row.get("目标管线", "")).casefold():
                result.append(dict(row))
        return result

    def check_dependencies_ready(self, config_name: str) -> dict[str, Any]:
        """检查上游依赖是否就绪。

        根据调度配置的上游依赖字段，遍历 store 中的所有管线记录，
        判断依赖是否完成，返回就绪状态和解锁时间。
        """
        config = None
        for row in store.rows(self.MODULE):
            if row.get("配置名称") == config_name:
                config = row
                break
        if config is None:
            return {"error": f"调度配置 {config_name} 不存在"}

        upstream_raw = str(config.get("上游依赖", ""))
        upstream_list = [u.strip() for u in upstream_raw.split(",") if u.strip()]
        dep_status: dict[str, dict[str, Any]] = {}
        all_ready = True

        for dep in upstream_list:
            found = False
            for sched_row in store.rows(self.MODULE):
                if sched_row.get("目标管线") == dep:
                    sched_status = sched_row.get("status", "")
                    dep_status[dep] = {
                        "status": sched_status,
                        "ready": sched_status in ("已调度", "已暂停"),
                    }
                    if not dep_status[dep]["ready"]:
                        all_ready = False
                    found = True
                    break
            if not found:
                dep_status[dep] = {"status": "未找到", "ready": False}
                all_ready = False

        return {
            "config": config_name,
            "pipeline": config.get("目标管线"),
            "dependencies": dep_status,
            "all_ready": all_ready,
            "can_trigger": all_ready and config.get("status") == "已调度",
            "checked_at": datetime.now().isoformat(),
        }

    def compute_next_run_time(self, config_name: str) -> dict[str, Any]:
        """计算下次调度执行时间。

        根据触发方式计算下一次调度的计划时间，
        输出调度窗口供运维日历消费。
        """
        config = None
        for row in store.rows(self.MODULE):
            if row.get("配置名称") == config_name:
                config = row
                break
        if config is None:
            return {"error": f"调度配置 {config_name} 不存在"}

        trigger = str(config.get("触发方式", "手动"))
        retry_delay = int(config.get("重试延迟", "300"))
        max_retries = int(config.get("最大重试次数", "3"))

        now = datetime.now()
        schedule_windows = {
            "每小时": 3600,
            "每天": 86400,
            "每周": 604800,
            "每月": 2592000,
        }

        interval = schedule_windows.get(trigger, 0)
        next_run = None
        schedule_type = "手动触发"

        if interval > 0:
            next_run = datetime.now().isoformat()
            schedule_type = trigger

        return {
            "config": config_name,
            "trigger": trigger,
            "schedule_type": schedule_type,
            "next_run_estimate": next_run,
            "retry_delay_sec": retry_delay,
            "max_retries": max_retries,
            "computed_at": now.isoformat(),
        }

    def get_failed_schedules(self) -> list[dict[str, Any]]:
        """获取所有失败的调度记录。

        筛选 store 中状态为"已失败"的调度配置，
        返回失败列表供重跑编排补偿使用。
        """
        result: list[dict[str, Any]] = []
        for row in store.rows(self.MODULE):
            if row.get("status") == "已失败":
                result.append({
                    "config": row.get("配置名称"),
                    "pipeline": row.get("目标管线"),
                    "trigger": row.get("触发方式"),
                    "retries_exhausted": True,
                    "detected_at": datetime.now().isoformat(),
                })
        return result

    # ---- 分区依赖驱动的恢复编排 ----

    def build_recovery_from_blocking(
        self,
        target_table: str,
        target_partition: str,
        *,
        requested_ranges: list[dict[str, str]] | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """依赖调度读取源分区阻塞明细生成补写计划。

        不再以节点「已启用」判断上游就绪，而是委托分区依赖引擎
        按 partition_registry 的实际分区状态核对每条依赖路径，
        并对同一根因分区去重，保证一个根因日期只有一个写入项。
        """
        from app.engine.partition_dependency import partition_dependency_engine

        engine = partition_dependency_engine
        analysis = engine.analyze(target_table, target_partition)
        plan = engine.build_recovery_plan(
            target_table,
            target_partition,
            requested_ranges=requested_ranges,
            reason=reason,
        )
        return {"analysis": analysis, "recovery_plan": plan}

    def recheck_unlockable_jobs(
        self, target_table: str, target_partition: str
    ) -> dict[str, Any]:
        """上游分区补齐后回查目标日，返回真正能解锁的日加工任务。

        只有被阻塞路径上的源分区全部在 partition_registry 中就绪，
        对应加工请求才解除阻塞；同时输出前后差集，便于核对
        原来哪个源分区没有产出。
        """
        from app.engine.partition_dependency import partition_dependency_engine

        engine = partition_dependency_engine
        return engine.recheck_blocked_requests(target_table, target_partition)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, msg = super().run_action(entry_id, action)
        if entry:
            entry["last_operated_at"] = datetime.now().isoformat()
        return entry, msg