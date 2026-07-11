"""repo-intel service entrypoint.

Container CMD is ``python main.py`` (see Dockerfile). Mirrors the launcher
pattern used by the other engines' ``app/main.py`` __main__ block: import the
FastAPI ``app`` and serve it with uvicorn on the configured engine port.
"""
import uvicorn

from app.config import settings
from app.main import app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.engine_port)
