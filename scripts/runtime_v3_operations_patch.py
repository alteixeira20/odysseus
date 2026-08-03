from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one anchor, found {count}: {old[:160]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "src/agent/runtime_v3/operations.py",
    '''        with self.ledger._tx() as db:
            db.executescript(
''',
    '''        # sqlite3.executescript() manages its own transaction boundary.
        with self.ledger._lock:
            self.ledger._conn.executescript(
''',
)

replace_once(
    "app.py",
    '''    "/api/chat",            # streaming
''',
    '''    "/api/chat",            # streaming
    "/api/agent-runs",      # durable SSE replay and cancellation
''',
)
replace_once(
    "app.py",
    '''app.include_router(setup_chat_routes(
    session_manager, chat_handler, chat_processor,
    memory_manager, research_handler, upload_handler,
    memory_vector=memory_vector,
    webhook_manager=webhook_manager,
    skills_manager=skills_manager,
))

# Research (background deep-research tasks)
''',
    '''app.include_router(setup_chat_routes(
    session_manager, chat_handler, chat_processor,
    memory_manager, research_handler, upload_handler,
    memory_vector=memory_vector,
    webhook_manager=webhook_manager,
    skills_manager=skills_manager,
))

# Durable Agent Runtime V3 operations
from routes.agent_runtime_routes import setup_agent_runtime_routes
app.include_router(setup_agent_runtime_routes())

# Research (background deep-research tasks)
''',
)
replace_once(
    "app.py",
    '''@app.get("/tasks")
async def serve_tasks(request: Request):
    return await serve_index(request)

@app.get("/library")
''',
    '''@app.get("/tasks")
async def serve_tasks(request: Request):
    return await serve_index(request)

@app.get("/agent-runtime")
async def serve_agent_runtime(request: Request):
    return serve_html_with_nonce(request, abs_join(BASE_DIR, "static/agent-runtime.html"))

@app.get("/library")
''',
)

print("Runtime V3 operations integration applied")
