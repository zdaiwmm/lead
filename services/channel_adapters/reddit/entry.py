"""Run the existing Reddit delivery queues through the attested common worker."""

from lead_app import worker

CHANNEL = "reddit"


async def run(binding):
    await worker.main()
