import logging
import chainlit as cl
from typing import Optional, Any

async def handle_deleteall_command(user_id: str, store: Any, pg_pool: Any) -> None:
    """Perform a hard wipe of all user data, threads, and memory from the database."""
    if not user_id:
        return

    # 1. Delete cross-thread memory from LangGraph Store
    try:
        if store:
            await store.adelete(("users",), user_id)
    except Exception as e:
        logging.error(f"Error deleting user from LangGraph Store: {e}")
    
    # 2. Perform a hard database wipe of all threads and LangGraph checkpoints
    try:
        cl_user = await cl.data._data_layer.get_user(user_id) if getattr(cl.data, "_data_layer", None) else None
        if pg_pool:
            async with pg_pool.connection() as conn:
                # Find all threads belonging to this user
                query_params = [user_id]
                query = 'SELECT id FROM threads WHERE "userIdentifier" = %s'
                if cl_user:
                    query += ' OR "userId" = %s'
                    query_params.append(cl_user.id)
                    
                res = await conn.execute(query, tuple(query_params))
                tids = [row[0] for row in await res.fetchall()]
                
                # Hard delete all related data to ensure UI and checkpointer are wiped
                for tid in tids:
                    await conn.execute('DELETE FROM feedbacks WHERE "threadId" = %s', (tid,))
                    await conn.execute('DELETE FROM elements WHERE "threadId" = %s', (tid,))
                    await conn.execute('DELETE FROM steps WHERE "threadId" = %s', (tid,))
                    await conn.execute('DELETE FROM threads WHERE id = %s', (tid,))
                    
                    await conn.execute("DELETE FROM checkpoints WHERE thread_id = %s", (tid,))
                    await conn.execute("DELETE FROM checkpoint_blobs WHERE thread_id = %s", (tid,))
                    await conn.execute("DELETE FROM checkpoint_writes WHERE thread_id = %s", (tid,))
    except Exception as e:
        logging.error(f"Error during exhaustive database delete for {user_id}: {e}")
