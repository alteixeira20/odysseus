import asyncio

import pytest

from src.agent_tools.document_tools import (
    get_active_document,
    get_active_model,
    set_active_document,
    set_active_model,
)


@pytest.mark.asyncio
async def test_document_and_model_context_is_isolated_between_agent_tasks():
    set_active_document("parent-document")
    set_active_model("parent-model")
    release = asyncio.Event()
    ready = [asyncio.Event(), asyncio.Event()]

    async def worker(index, document_id, model):
        set_active_document(document_id)
        set_active_model(model)
        ready[index].set()
        await release.wait()
        return get_active_document(), get_active_model()

    tasks = [
        asyncio.create_task(worker(0, "document-a", "model-a")),
        asyncio.create_task(worker(1, "document-b", "model-b")),
    ]
    await asyncio.gather(*(event.wait() for event in ready))
    release.set()

    assert await asyncio.gather(*tasks) == [
        ("document-a", "model-a"),
        ("document-b", "model-b"),
    ]
    assert get_active_document() == "parent-document"
    assert get_active_model() == "parent-model"

    set_active_document(None)
    set_active_model(None)
