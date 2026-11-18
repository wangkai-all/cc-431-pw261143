"""Serve the analysis console and proxy the three services to one origin.

The browser only ever talks to this gateway; each backend stays independently
runnable on its own port.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

STATIC = Path(__file__).parent / "static"


def create_app(
    observations_url: str,
    pairing_url: str,
    reports_url: str,
    *,
    demo_data_dir: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="Paired analysis console", version="1.0")
    clients = {
        "observations": httpx.Client(base_url=observations_url.rstrip("/"), timeout=30.0),
        "pairing": httpx.Client(base_url=pairing_url.rstrip("/"), timeout=30.0),
        "reports": httpx.Client(base_url=reports_url.rstrip("/"), timeout=30.0),
    }

    def _forward(client: httpx.Client, method: str, path: str, request: Request, body: bytes | None):
        headers = {}
        if request.headers.get("content-type"):
            headers["content-type"] = request.headers["content-type"]
        try:
            response = client.request(method, path, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=503, detail=f"upstream unavailable: {exc}") from exc
        return Response(
            content=response.content,
            status_code=response.status_code,
            media_type=response.headers.get("content-type", "application/json"),
        )

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/static/{name}")
    def static_file(name: str) -> FileResponse:
        candidate = (STATIC / name).resolve()
        if STATIC.resolve() not in candidate.parents or not candidate.is_file():
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(candidate)

    @app.get("/api/health")
    def health() -> dict:
        out = {}
        for name, client in clients.items():
            try:
                out[name] = client.get("/health").json()
            except httpx.HTTPError as exc:
                out[name] = {"ok": False, "error": str(exc)}
        return out

    async def proxy(service: str, path: str, request: Request, method: str):
        body = await request.body() if method in ("POST", "PUT", "PATCH") else None
        return _forward(clients[service], method, path, request, body)

    @app.api_route("/obs/{path:path}", methods=["GET", "POST"])
    async def obs(path: str, request: Request):
        return await proxy("observations", f"/api/{path}", request, request.method)

    @app.api_route("/pair/{path:path}", methods=["GET", "POST"])
    async def pair(path: str, request: Request):
        return await proxy("pairing", f"/api/{path}", request, request.method)

    @app.api_route("/report/{path:path}", methods=["GET", "POST"])
    async def report(path: str, request: Request):
        return await proxy("reports", f"/api/{path}", request, request.method)

    @app.post("/api/demo/seed")
    def seed_demo() -> dict:
        """Convenience for the bundled synthetic dataset; not used by analyses."""
        from ..common.seeder import seed

        result = seed(observations_url)
        return result

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Web console gateway")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--observations-url", default="http://127.0.0.1:8101")
    parser.add_argument("--pairing-url", default="http://127.0.0.1:8102")
    parser.add_argument("--reports-url", default="http://127.0.0.1:8103")
    args = parser.parse_args(argv)
    uvicorn.run(
        create_app(args.observations_url, args.pairing_url, args.reports_url),
        host=args.host,
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
