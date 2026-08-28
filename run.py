import uvicorn

from backend.config import ROOT, get_settings


if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "backend.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.app_reload,
        reload_dirs=[str(ROOT / "backend")] if settings.app_reload else None,
    )

