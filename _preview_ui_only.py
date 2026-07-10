"""Serve ONLY the dashboard (no scheduler, browser, or orchestrator) for UI verification.

Real `_preview_server.py` boots the whole ServiceManager, which launches Playwright and the
continuous watches — far too heavy just to look at a settings panel. This stubs the manager so
the FastAPI app and its static UI come up on their own port, against the real Ollama.
"""
import types

import uvicorn

from web_watcher.dashboard import server as S

mgr = types.SimpleNamespace(
    get_job_info=lambda: [],
    oversight_snapshot=lambda **k: {"entries": []},
    status=lambda: {},
)

if __name__ == "__main__":
    uvicorn.run(S.create_app(mgr), host="127.0.0.1", port=7899, log_level="warning")
