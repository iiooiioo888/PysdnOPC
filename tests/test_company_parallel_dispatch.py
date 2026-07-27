"""公司模式並行限流與 LLMProvider 並行安全測試。

覆蓋兩個「最小安全包」改動：
1. dispatch 循環的 max_parallel_workers 信號量限流
   （_create_claimed_work_item_task 以 _dispatch_semaphore 節流執行）。
2. LLMProvider 計數器 / 響應快取的執行緒安全鎖。
"""
from __future__ import annotations

import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from opc.core.config import LLMConfig, ParallelPolicyConfig, TaskModeConfig
from opc.layer2_organization._company_executor_dispatch import (
    CompanyExecutorDispatchMixin,
)
from opc.llm.provider import LLMProvider


class _StubDispatchExecutor(CompanyExecutorDispatchMixin):
    """僅提供 _create_claimed_work_item_task 所需屬性的最小樁。"""

    def __init__(self, semaphore: asyncio.Semaphore | None) -> None:
        self.active_task_run_registry = None
        self._dispatch_semaphore = semaphore
        self._concurrent = 0
        self.max_observed_concurrency = 0

    async def _run_claimed_work_item(self, member_session, task, task_by_projection_id):
        self._concurrent += 1
        self.max_observed_concurrency = max(self.max_observed_concurrency, self._concurrent)
        await asyncio.sleep(0.02)
        self._concurrent -= 1
        return None


def _fake_task(task_id: str) -> SimpleNamespace:
    return SimpleNamespace(project_id="proj", id=task_id, metadata={})


class TestDispatchSemaphoreThrottling(unittest.TestCase):
    def _run_items(self, semaphore: asyncio.Semaphore | None, count: int) -> int:
        async def scenario() -> int:
            executor = _StubDispatchExecutor(semaphore)
            work_item_tasks = [
                executor._create_claimed_work_item_task(MagicMock(), _fake_task(f"t{i}"), {})
                for i in range(count)
            ]
            await asyncio.gather(*work_item_tasks)
            return executor.max_observed_concurrency

        return asyncio.run(scenario())

    def test_semaphore_caps_concurrent_work_items(self) -> None:
        async def scenario() -> int:
            semaphore = asyncio.Semaphore(2)
            executor = _StubDispatchExecutor(semaphore)
            work_item_tasks = [
                executor._create_claimed_work_item_task(MagicMock(), _fake_task(f"t{i}"), {})
                for i in range(6)
            ]
            await asyncio.gather(*work_item_tasks)
            return executor.max_observed_concurrency

        self.assertLessEqual(asyncio.run(scenario()), 2)

    def test_no_semaphore_runs_all_concurrently(self) -> None:
        observed = self._run_items(semaphore=None, count=6)
        self.assertEqual(observed, 6)

    def test_all_work_items_complete_under_throttle(self) -> None:
        async def scenario() -> list:
            executor = _StubDispatchExecutor(asyncio.Semaphore(3))
            work_item_tasks = [
                executor._create_claimed_work_item_task(MagicMock(), _fake_task(f"t{i}"), {})
                for i in range(9)
            ]
            return await asyncio.gather(*work_item_tasks)

        results = asyncio.run(scenario())
        self.assertEqual(len(results), 9)


class TestParallelWorkerConfig(unittest.TestCase):
    def test_task_mode_config_default_company_max_parallel_workers(self) -> None:
        self.assertEqual(TaskModeConfig().company_max_parallel_workers, 20)

    def test_parallel_policy_default_max_workers(self) -> None:
        self.assertEqual(ParallelPolicyConfig().max_workers, 20)


class TestLLMProviderThreadSafety(unittest.TestCase):
    def _provider(self) -> LLMProvider:
        return LLMProvider(LLMConfig(default_model="openai/gpt-4o", api_key="sk-test"))

    def test_record_usage_is_thread_safe(self) -> None:
        provider = self._provider()
        iterations = 2000
        workers = 8

        def hammer() -> None:
            for _ in range(iterations):
                provider._record_usage(1, 2, 0.001)

        threads = [threading.Thread(target=hammer) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stats = provider.stats
        self.assertEqual(stats["tokens_in"], iterations * workers)
        self.assertEqual(stats["tokens_out"], 2 * iterations * workers)
        self.assertAlmostEqual(stats["estimated_cost"], 0.001 * iterations * workers, places=6)

    def test_record_usage_returns_running_total_cost(self) -> None:
        provider = self._provider()
        self.assertAlmostEqual(provider._record_usage(1, 1, 0.5), 0.5)
        self.assertAlmostEqual(provider._record_usage(1, 1, 0.25), 0.75)

    def test_cache_put_get_and_counters(self) -> None:
        provider = self._provider()
        self.assertIsNone(provider._cache_get("missing"))
        provider._cache_put("key-a", {"content": "a"})
        self.assertEqual(provider._cache_get("key-a"), {"content": "a"})
        stats = provider.get_cache_stats()
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["misses"], 1)

    def test_cache_lru_eviction_respects_max_size(self) -> None:
        provider = self._provider()
        provider._cache_max_size = 3
        for i in range(5):
            provider._cache_put(f"key-{i}", {"content": str(i)})
        self.assertEqual(len(provider._response_cache), 3)
        # 最舊的條目被移除
        self.assertIsNone(provider._cache_get("key-0"))
        self.assertEqual(provider._cache_get("key-4"), {"content": "4"})

    def test_concurrent_cache_access_does_not_corrupt(self) -> None:
        provider = self._provider()
        provider._cache_max_size = 50

        def hammer(worker_id: int) -> None:
            for i in range(500):
                key = f"w{worker_id}-k{i % 60}"
                provider._cache_put(key, {"content": key})
                provider._cache_get(key)

        threads = [threading.Thread(target=hammer, args=(w,)) for w in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertLessEqual(len(provider._response_cache), 50)


if __name__ == "__main__":
    unittest.main()
