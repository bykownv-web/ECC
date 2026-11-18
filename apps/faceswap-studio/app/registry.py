"""Discovery of local checkpoint / LoRA files and safe name -> path resolution."""

from __future__ import annotations

from pathlib import Path

MODEL_EXTENSIONS = {".safetensors"}


def list_models(directory: Path) -> list[dict]:
    """Model files directly inside `directory`, sorted by name. Missing directory -> []."""
    if not directory.is_dir():
        return []
    items = []
    for path in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
        if path.is_file() and path.suffix.lower() in MODEL_EXTENSIONS:
            items.append({"name": path.name, "size_mb": round(path.stat().st_size / 2**20, 1)})
    return items


def resolve_model(directory: Path, name: str) -> Path | None:
    """Return the path only if `name` is exactly one of the listed files (no traversal)."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    candidate = directory / name
    if candidate.suffix.lower() not in MODEL_EXTENSIONS or not candidate.is_file():
        return None
    if candidate.resolve().parent != directory.resolve():
        return None
    return candidate
