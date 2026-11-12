"""Installed as the reviewed entrypoint alongside attested private primitives."""
CHANNEL = "telegram"


async def run(binding):
    from lead_app.growth.executor import serve_browser

    from . import telegram
    async def native_receipt(actor,task_id):
        # This consumer preserves only already-submitted pre-migration operations.
        # The operation is fixed here; there is no dispatch/selection loop.
        from lead_app.growth.executor import native_receipt_guard, read_original_native

        from . import community_receipts, community_runtime
        community_runtime.guard = native_receipt_guard
        return await read_original_native(actor,task_id,community_receipts.reconcile)
    await serve_browser(binding,port=18092,telegram_primitives=telegram,native_receipt_reader=native_receipt)
