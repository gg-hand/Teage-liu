"""后台任务注册表(E4,计划 §4.2):core 提供的最小后台任务编排。

两类任务(§transport T-4):
- 宿主侧后台任务:枝干 setup 注册,shutdown 统一取消,关闭不泄漏(~30 行)
- 扩展侧登记任务:经 transport ``task_register`` 消息登记(任务归属扩展进程,
  宿主只登记/可观测/协调取消,不承载执行);扩展 teardown 自取消
  (宿主无法终止扩展进程内任务,``cancel_all`` 对登记任务只清登记表)。
  宿主侧任务的 ``cancel_all`` 兜底**仅在宿主整体 shutdown**;热重载重建
  **不调用**(无链粒度会误取消新链任务,§lifecycle L-4 / §transport T-4)。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Coroutine, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class RegisteredTask:
    """扩展侧登记任务(宿主只登记,任务归属扩展进程,T-4)。"""

    task_id: str
    description: str = ""
    owner: str = ""
    status: str = "registered"


class TaskRegistry:
    """后台任务注册表:create_task 注册,shutdown 统一取消(幂等)。"""

    def __init__(self) -> None:
        self._tasks: Set[asyncio.Task] = set()
        #: 扩展侧登记任务表(task_id -> RegisteredTask)
        self._registered: Dict[str, RegisteredTask] = {}

    def create_task(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
        """注册并启动一个后台任务(完成后自动移出注册表)。"""
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: asyncio.Task) -> None:
        """任务收尾:移出注册表 + **回收异常**（2026-09-11 交叉评审）。

        此前 done callback 只做 ``discard``：任务异常会变成 GC 期的
        "Task exception was never retrieved" 噪声（非 error 级、无码），
        与 §errors R-1"捕获点必须带日志"相悖。
        """
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("后台任务异常(已回收): %s", exc)

    # ------------------------------------------------------------------
    # 扩展侧任务登记(§transport T-4:宿主只登记/协调取消,不承载执行)
    # ------------------------------------------------------------------
    def register_task(self, task_id: str, description: str = "", owner: str = "") -> None:
        """登记一个扩展进程内的后台任务(幂等:同 task_id 覆盖描述)。"""
        if not task_id or not isinstance(task_id, str):
            raise ValueError("task_id 必须是非空字符串")
        self._registered[task_id] = RegisteredTask(
            task_id=task_id, description=description, owner=owner
        )

    def cancel_task(self, task_id: str) -> bool:
        """标记取消一个登记任务(协调取消:实际执行终止由扩展进程负责)。

        P2(2026-09-11 综合评审):先改状态再出表 —— 此前先 pop 后改状态,
        状态变更对 ``list_registered`` 不可见(对象已不在登记表中,改动成死代码)。
        """
        task = self._registered.get(task_id)
        if task is None:
            return False
        task.status = "cancelled"
        self._registered.pop(task_id, None)
        return True

    def list_registered(self) -> List[RegisteredTask]:
        """列出全部登记任务(可观测)。"""
        return list(self._registered.values())

    @property
    def registered_count(self) -> int:
        return len(self._registered)

    async def cancel_all(self) -> None:
        """取消全部任务(幂等,可多次调用):

        ① 宿主侧后台任务(宿主整体 shutdown 兜底)并**等待取消真正完成**;
        ② 登记任务清空——仅清登记表,扩展进程内任务宿主**无法终止**,
        须由扩展 teardown 自取消。
        热重载重建**不调用**本方法(无链粒度,会误取消新链任务):
        见 §lifecycle L-4 / §transport T-4。
        """
        pending = [t for t in list(self._tasks) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            # 2026-09-11 交叉评审:此前 cancel() 后立即 clear,不等取消传播 ——
            # 关闭编排的后续步骤(teardown/close)可能与仍在运行的任务并发。
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        self._registered.clear()

    @property
    def count(self) -> int:
        return len(self._tasks)
