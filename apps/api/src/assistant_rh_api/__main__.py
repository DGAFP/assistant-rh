"""Local command-line entry point for the API server."""

from __future__ import annotations

import os


def main() -> None:
    """Run the ASGI application with Uvicorn."""
    import uvicorn

    uvicorn.run(
        "assistant_rh_api.handlers.app:app",
        host=os.getenv("API_HOST", "0.0.0.0"),
        port=int(os.getenv("API_PORT", "8000")),
        proxy_headers=False,
        # Uvicorn waits for requests before lifespan shutdown. Bound that
        # wait so open SSE streams are cancelled and their cleanup is joined.
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
