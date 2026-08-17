"""The per-thread_id run lock in includes/chat/runner.py.

Two concurrent runs on one thread corrupt the checkpoint, so run_turn()
serialises them: reject for user-typed messages, queue for button clicks.
"""

import asyncio

import pytest

from includes.chat import runner as mod
from includes.chat.runner import RunInProgress, run_turn


class SlowGraph:
    """A graph whose stream blocks until released, so runs can overlap."""

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.runs = 0
        self.concurrent = 0
        self.max_concurrent = 0

    async def aget_state(self, config):
        return None

    async def aupdate_state(self, config, values):  # pragma: no cover - unused
        pass

    def astream_events(self, inputs, config=None, version=None):
        async def _gen():
            self.runs += 1
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
            self.started.set()
            try:
                await self.release.wait()
            finally:
                self.concurrent -= 1
            return
            yield  # make this an async generator

        return _gen()


class BoomGraph:
    async def aget_state(self, config):
        return None

    def astream_events(self, inputs, config=None, version=None):
        async def _gen():
            raise RuntimeError("graph exploded")
            yield

        return _gen()


@pytest.fixture(autouse=True)
def clean_locks():
    mod._run_locks.clear()
    yield
    mod._run_locks.clear()


async def _run(ctx, graph, **kwargs):
    await run_turn("hello", ctx, graph=graph, **kwargs)


# --- reject ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_second_run_is_rejected_while_the_first_is_active(chat_ctx):
    graph = SlowGraph()
    first = asyncio.create_task(_run(chat_ctx, graph))
    await graph.started.wait()

    with pytest.raises(RunInProgress) as exc:
        await _run(chat_ctx, graph, on_busy="reject")
    assert exc.value.thread_id == chat_ctx.thread_id

    graph.release.set()
    await first
    assert graph.runs == 1


# --- wait -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_second_run_queues_under_wait(chat_ctx):
    graph = SlowGraph()
    first = asyncio.create_task(_run(chat_ctx, graph))
    await graph.started.wait()

    second = asyncio.create_task(_run(chat_ctx, graph, on_busy="wait"))
    await asyncio.sleep(0)
    assert not second.done(), "second run should be waiting on the lock"

    graph.release.set()
    await asyncio.gather(first, second)

    assert graph.runs == 2
    assert graph.max_concurrent == 1, "runs must not overlap on one thread"


@pytest.mark.asyncio
async def test_wait_gives_up_after_busy_timeout(chat_ctx):
    graph = SlowGraph()
    first = asyncio.create_task(_run(chat_ctx, graph))
    await graph.started.wait()

    with pytest.raises(RunInProgress):
        await _run(chat_ctx, graph, on_busy="wait", busy_timeout=0.01)

    graph.release.set()
    await first


# --- isolation ------------------------------------------------------------

@pytest.mark.asyncio
async def test_different_threads_run_in_parallel(make_chat_ctx):
    graph = SlowGraph()
    a = make_chat_ctx(thread_id="thread-a")
    b = make_chat_ctx(thread_id="thread-b")

    t1 = asyncio.create_task(_run(a, graph))
    t2 = asyncio.create_task(_run(b, graph))
    await asyncio.sleep(0.01)

    graph.release.set()
    await asyncio.gather(t1, t2)

    assert graph.runs == 2
    assert graph.max_concurrent == 2


# --- lifecycle ------------------------------------------------------------

@pytest.mark.asyncio
async def test_lock_is_released_when_the_graph_raises(chat_ctx):
    # run_turn swallows graph errors into a user-facing apology
    await _run(chat_ctx, BoomGraph())
    assert "unexpected error" in chat_ctx.messages[0].content

    # The lock must be free for the next turn
    await _run(chat_ctx, BoomGraph())
    assert mod._run_locks == {}


@pytest.mark.asyncio
async def test_locks_do_not_accumulate(make_chat_ctx):
    graph = SlowGraph()
    graph.release.set()
    for i in range(5):
        await _run(make_chat_ctx(thread_id=f"thread-{i}"), graph)
    assert mod._run_locks == {}


@pytest.mark.asyncio
async def test_waiter_keeps_the_lock_entry_alive(chat_ctx):
    """The entry must not be popped while someone is still queued on it."""
    graph = SlowGraph()
    first = asyncio.create_task(_run(chat_ctx, graph))
    await graph.started.wait()
    second = asyncio.create_task(_run(chat_ctx, graph, on_busy="wait"))
    await asyncio.sleep(0)

    graph.release.set()
    await asyncio.gather(first, second)

    assert mod._run_locks == {}
    assert graph.runs == 2


@pytest.mark.asyncio
async def test_run_turn_is_never_re_entered_from_within_a_run(chat_ctx):
    """Cheap insurance: re-entry would deadlock, since the lock is not reentrant."""
    graph = SlowGraph()
    graph.release.set()
    depth = {"max": 0, "current": 0}
    original = mod._run_turn_locked

    async def _tracked(*args, **kwargs):
        depth["current"] += 1
        depth["max"] = max(depth["max"], depth["current"])
        try:
            return await original(*args, **kwargs)
        finally:
            depth["current"] -= 1

    mod._run_turn_locked = _tracked
    try:
        await _run(chat_ctx, graph)
    finally:
        mod._run_turn_locked = original

    assert depth["max"] == 1
