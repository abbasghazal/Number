"""Context-preserving thread offload, including on Python 3.8 deployments."""
import asyncio
import contextvars
import functools


async def to_thread(func, *args, **kwargs):
    call = functools.partial(contextvars.copy_context().run, func, *args, **kwargs)
    return await asyncio.get_running_loop().run_in_executor(None, call)
