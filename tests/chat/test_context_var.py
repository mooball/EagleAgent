"""The ChatContext ContextVar: binding, nesting, and propagation.

The propagation cases matter because `includes/chat/middleware.py` and
`includes/agents/base.py` fire their notifications from retry paths and logging
handlers, which may not run on the task that bound the context.
"""

import asyncio

import pytest

from includes.chat.context import (
    bind_chat_context,
    chat_context,
    get_chat_context,
    reset_chat_context,
    try_get_chat_context,
)


def test_get_raises_when_nothing_is_bound():
    with pytest.raises(RuntimeError, match="No ChatContext bound"):
        get_chat_context()


def test_try_get_returns_none_when_nothing_is_bound():
    assert try_get_chat_context() is None


def test_bind_and_reset(chat_ctx):
    token = bind_chat_context(chat_ctx)
    try:
        assert get_chat_context() is chat_ctx
    finally:
        reset_chat_context(token)
    assert try_get_chat_context() is None


def test_context_manager_binds_and_unbinds(chat_ctx):
    with chat_context(chat_ctx) as bound:
        assert bound is chat_ctx
        assert get_chat_context() is chat_ctx
    assert try_get_chat_context() is None


def test_context_manager_unbinds_on_exception(chat_ctx):
    with pytest.raises(ValueError):
        with chat_context(chat_ctx):
            raise ValueError("boom")
    assert try_get_chat_context() is None


def test_nested_binds_restore_the_outer_context(make_chat_ctx):
    outer = make_chat_ctx(thread_id="outer")
    inner = make_chat_ctx(thread_id="inner")
    with chat_context(outer):
        assert get_chat_context().thread_id == "outer"
        with chat_context(inner):
            assert get_chat_context().thread_id == "inner"
        assert get_chat_context().thread_id == "outer"
    assert try_get_chat_context() is None


@pytest.mark.asyncio
async def test_propagates_into_create_task(make_chat_ctx):
    ctx = make_chat_ctx(thread_id="t-task")
    seen = {}

    async def _child():
        seen["thread_id"] = get_chat_context().thread_id

    with chat_context(ctx):
        # This is the `middleware.GeminiRetryNotifier` shape: it fires a
        # notification via loop.create_task from inside the run.
        await asyncio.create_task(_child())

    assert seen == {"thread_id": "t-task"}


@pytest.mark.asyncio
async def test_a_task_created_outside_the_bind_does_not_see_the_context(chat_ctx):
    seen = {}

    async def _child():
        seen["ctx"] = try_get_chat_context()

    with chat_context(chat_ctx):
        pass
    await asyncio.create_task(_child())

    assert seen == {"ctx": None}


@pytest.mark.asyncio
async def test_a_task_holds_its_own_copy_after_the_parent_resets(make_chat_ctx):
    """create_task snapshots the context, so a long task is not orphaned."""
    ctx = make_chat_ctx(thread_id="t-snapshot")
    started = asyncio.Event()
    release = asyncio.Event()
    seen = {}

    async def _child():
        started.set()
        await release.wait()
        seen["thread_id"] = get_chat_context().thread_id

    token = bind_chat_context(ctx)
    task = asyncio.create_task(_child())
    await started.wait()
    reset_chat_context(token)

    release.set()
    await task
    assert seen == {"thread_id": "t-snapshot"}
    assert try_get_chat_context() is None


@pytest.mark.asyncio
async def test_concurrent_tasks_each_see_their_own_context(make_chat_ctx):
    seen: dict[str, str] = {}

    async def _run(name):
        with chat_context(make_chat_ctx(thread_id=name)):
            await asyncio.sleep(0)
            seen[name] = get_chat_context().thread_id

    await asyncio.gather(_run("a"), _run("b"), _run("c"))
    assert seen == {"a": "a", "b": "b", "c": "c"}


@pytest.mark.asyncio
async def test_does_not_propagate_into_a_worker_thread(make_chat_ctx):
    """asyncio.to_thread copies the context; a raw thread does not.

    `middleware.py` fires from a logging handler. If that handler ever runs on
    a bare thread it must be given an explicit ctx captured at construction.
    """
    import threading

    ctx = make_chat_ctx(thread_id="t-thread")
    seen = {}

    def _in_thread(key):
        seen[key] = try_get_chat_context()

    with chat_context(ctx):
        await asyncio.to_thread(_in_thread, "to_thread")

        t = threading.Thread(target=_in_thread, args=("raw",))
        t.start()
        t.join()

    assert seen["to_thread"] is ctx
    assert seen["raw"] is None
