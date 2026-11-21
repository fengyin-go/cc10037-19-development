"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。

blocking 必须在 resource 之前注册：resource 含通配路径 /api/{module}/{entry_id}，
后置会把 /api/blocking/analyze 误匹配成模块 blocking 的 entry_id。
"""
from __future__ import annotations

from app.routers import blocking, business, resource

ROUTERS = [blocking, resource, business]
