"""自動循環引擎測試 — 獨立運行，不依賴 litellm。"""

import asyncio
import sys
import importlib
import importlib.util
from pathlib import Path

# 導入 auto_loop 模塊（避開 engine/__init__.py 的 litellm 依賴）
def _load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

# 先載入依賴
auto_loop = _load_module("auto_loop_test", "opc/engine/auto_loop.py")
AutoLoopManager = auto_loop.AutoLoopManager
format_loop_stats = auto_loop.format_loop_stats


def test_retry_loop():
    """測試重試循環。"""
    asyncio.run(_test_retry_loop())


async def _test_retry_loop():
    """測試重試循環。"""
    manager = AutoLoopManager()
    call_count = 0

    async def flaky_task():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise Exception(f"Attempt {call_count} failed")
        return "success!"

    print("=== Retry Loop Test ===")
    run = await manager.retry_loop(
        task_id="test-1",
        execute_fn=flaky_task,
        role="tester",
        max_retries=3,
        retry_delay=0.05,
    )
    assert run.status.value == "success", f"Expected success, got {run.status.value}"
    assert run.attempt == 3
    assert run.result == "success!"
    assert len(run.error_history) == 2
    print(f"  ✅ Status: {run.status.value} | Attempts: {run.attempt} | Result: {run.result}")


def test_retry_exhausted():
    """測試重試耗盡。"""
    asyncio.run(_test_retry_exhausted())


async def _test_retry_exhausted():
    """測試重試耗盡。"""
    manager = AutoLoopManager()

    async def always_fail():
        raise Exception("Always fails")

    print("\n=== Retry Exhausted Test ===")
    run = await manager.retry_loop(
        task_id="test-2",
        execute_fn=always_fail,
        max_retries=2,
        retry_delay=0.05,
    )
    assert run.status.value == "max_retries", f"Expected max_retries, got {run.status.value}"
    assert run.attempt == 2
    print(f"  ✅ Status: {run.status.value} | Attempts: {run.attempt}")


def test_quality_gate():
    """測試質量門禁。"""
    asyncio.run(_test_quality_gate())


async def _test_quality_gate():
    """測試質量門禁。"""
    manager = AutoLoopManager()
    quality_score = 0.3

    async def produce(previous_output=None, feedback=None):
        return f"output_v{1 if not previous_output else 2}"

    async def review(output):
        nonlocal quality_score
        quality_score += 0.3
        return quality_score, "needs improvement"

    print("\n=== Quality Gate Test ===")
    run = await manager.quality_gate_loop(
        task_id="test-3",
        produce_fn=produce,
        review_fn=review,
        role="writer",
        max_rounds=4,
        quality_threshold=0.8,
        retry_delay=0.05,
    )
    assert run.status.value == "success", f"Expected success, got {run.status.value}"
    print(f"  ✅ Status: {run.status.value} | Attempts: {run.attempt}")


def test_self_heal():
    """測試自癒循環。"""
    asyncio.run(_test_self_heal())


async def _test_self_heal():
    """測試自癒循環。"""
    manager = AutoLoopManager()
    heal_attempts = 0

    async def detect():
        nonlocal heal_attempts
        heal_attempts += 1
        return None if heal_attempts > 2 else "bug detected"

    async def fix(issue):
        pass

    async def verify():
        return heal_attempts > 2

    print("\n=== Self-Heal Test ===")
    run = await manager.self_heal_loop(
        task_id="test-4",
        detect_fn=detect,
        fix_fn=fix,
        verify_fn=verify,
        role="developer",
        max_cycles=3,
        retry_delay=0.05,
    )
    assert run.status.value == "success", f"Expected success, got {run.status.value}"
    print(f"  ✅ Status: {run.status.value} | Attempts: {run.attempt}")


def test_stats():
    """測試統計功能。"""
    asyncio.run(_test_stats())


async def _test_stats():
    """測試統計功能。"""
    manager = AutoLoopManager()

    async def ok_task():
        return "ok"

    async def fail_task():
        raise Exception("fail")

    # 運行一些循環
    await manager.retry_loop("t1", ok_task, max_retries=1, retry_delay=0.01)
    await manager.retry_loop("t2", fail_task, max_retries=1, retry_delay=0.01)

    print("\n=== Stats Test ===")
    stats = manager.get_stats()
    assert stats["total_runs"] == 2
    assert stats["success"] == 1
    assert stats["failed"] == 1
    print(format_loop_stats(stats))
    print("  ✅ Stats are correct")


async def run_all():
    print("🧪 AutoLoopManager 測試\n")
    await test_retry_loop()
    await test_retry_exhausted()
    await test_quality_gate()
    await test_self_heal()
    await test_stats()
    print("\n✅ 全部測試通過!")


if __name__ == "__main__":
    asyncio.run(run_all())
