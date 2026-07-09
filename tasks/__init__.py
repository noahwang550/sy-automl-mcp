"""Background task manager for long-running AutoGluon fit/predict calls.

AutoGluon's API is synchronous and blocking; a single ``fit`` can run for
minutes or hours. MCP tool calls must not block that long, so training tools
submit work to a :class:`TaskManager` and return a ``task_id`` immediately.

Design notes:
- Uses :class:`ThreadPoolExecutor` (not asyncio) because AutoGluon is sync;
  wrapping every call in ``run_in_executor`` adds complexity for no gain.
- Default ``max_workers=1`` (serial) — AutoGluon already saturates a machine
  and parallel training risks OOM. Configurable via ``MCP_MAX_WORKERS``.
- Cancellation is **soft**: Python cannot safely kill a thread. We set a flag
  and rely on AutoGluon's ``time_limit`` to bound runtime. Callers should
  always set ``time_limit`` on training jobs.
"""
from .manager import Task, TaskManager, get_task_manager

__all__ = ["Task", "TaskManager", "get_task_manager"]
