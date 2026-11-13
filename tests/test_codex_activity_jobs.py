"""Long Codex work releases Activity capacity and survives polling retries."""
import asyncio
import unittest


class ActivityJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_poll_does_not_restart_active_work(self):
        from lead_app.local_executor_client import ActivityJobs
        jobs = ActivityJobs()
        gate = asyncio.Event()
        calls = []

        async def work():
            calls.append(1)
            await gate.wait()
            return {"outcome": "completed"}

        first = await jobs.step("original-task", work)
        self.assertTrue(first["executor_pending"])
        await asyncio.sleep(0)
        self.assertTrue((await jobs.step("original-task", work))["executor_pending"])
        self.assertEqual(calls, [1])
        gate.set()
        await asyncio.sleep(0)
        self.assertEqual(await jobs.step("original-task", work), {"outcome": "completed"})

    async def test_exception_is_delivered_to_original_caller(self):
        from lead_app.local_executor_client import ActivityJobs
        jobs = ActivityJobs()

        async def fail():
            raise ValueError("original-error")

        await jobs.step("original-task", fail)
        await asyncio.sleep(0)
        with self.assertRaisesRegex(ValueError, "original-error"):
            await jobs.step("original-task", fail)

    async def test_cancel_stops_only_matching_work(self):
        from lead_app.local_executor_client import ActivityJobs
        jobs = ActivityJobs()
        gate = asyncio.Event()
        await jobs.step("a", gate.wait)
        await jobs.step("b", gate.wait)
        jobs.cancel("a")
        await asyncio.sleep(0)
        self.assertTrue((await jobs.step("b", gate.wait))["executor_pending"])
        jobs.cancel("b")


if __name__ == "__main__":
    unittest.main()
