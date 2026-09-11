"""InProcessHostPort 的 task_* 消息通道锚定(2026-09-11 WP-G 顺带修复)。

修复前缺陷(2026-09-11 发现):``register_task`` / ``cancel_task`` 是**同步方法**
却调用 async 的 ``self._bus.handle(...)`` 且**未 await** → 协程对象被丢弃,
``task_register`` / ``task_cancel`` 消息**永不执行**(并产生 RuntimeWarning);
同文件 ``storage_*`` / ``invoke_llm`` 均为正确 async。

修复 = 改 ``async def`` + ``await``(与 storage_*/invoke_llm 同形)。
"""
import asyncio
import warnings

from teage_liu2.core.tasks import TaskRegistry
from teage_liu2.core.transport import TransportBus


def _port(tasks: TaskRegistry):
    bus = TransportBus(task_registry=tasks)
    bus.register_extension("audit", ["observe"])
    return bus.make_in_process_port("audit")


def test_register_task_actually_registers():
    """host_port.register_task 必须真正走到 TaskRegistry(修复前:协程被丢弃,never 执行)。"""
    tasks = TaskRegistry()
    port = _port(tasks)
    asyncio.run(port.register_task("t1", description="d"))
    assert tasks.registered_count == 1


def test_cancel_task_actually_cancels():
    """host_port.cancel_task 必须真正注销登记任务。"""
    tasks = TaskRegistry()
    port = _port(tasks)
    asyncio.run(port.register_task("t1"))
    assert tasks.registered_count == 1
    asyncio.run(port.cancel_task("t1"))
    assert tasks.registered_count == 0


def test_task_messages_leave_no_unawaited_coroutine():
    """修复判据:调用后不得残留 'never awaited' 警告(协程被丢弃的直接证据)。"""
    tasks = TaskRegistry()
    port = _port(tasks)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        asyncio.run(port.register_task("t1"))
        asyncio.run(port.cancel_task("t1"))
    leaked = [w for w in caught if "never awaited" in str(w.message)]
    assert not leaked, f"存在未 await 的协程: {[str(w.message) for w in leaked]}"
