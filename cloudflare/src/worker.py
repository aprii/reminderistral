import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app

try:
    from workers import asgi
    Default = asgi.entrypoint(app)
except Exception:
    Default = None

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8787")))
