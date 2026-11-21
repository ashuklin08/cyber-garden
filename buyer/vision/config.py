"""Local-only assets and pinned, reviewed model provenance."""
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL_ID = "nomic-ai/nomic-embed-vision-v1.5"
REVISION = "e3a725bce72db07ca4adb1d83da08903f3ee02f8"
WEIGHT_FILE = "onnx/model_int8.onnx"
WEIGHT_SHA256 = "ba9107df6e412828dae8c675096209aa39f6536de8ec8d9a872665b54dc750c3"
WEIGHT_BYTES = 96745606
PREPROCESSING = "nomic-v1.5-224x224-bicubic-rgb-cls-l2-v1"
MAX_BYTES = 5 * 1024 * 1024
MAX_SIDE = 2048
UNAVAILABLE = "Визуальный поиск пока недоступен для текущего каталога. Вы можете воспользоваться обычным поиском."
DEMO_IMAGES = {
    "table": ("queries/table.png", "Стол"),
    "chair": ("queries/chair.png", "Кресло"),
    "lamp": ("queries/lamp.png", "Лампа"),
    "landscape": ("queries/landscape.png", "Пейзаж · без совпадения"),
}


@dataclass(frozen=True)
class Config:
    visual: Path = ROOT / "data/visual"
    model: Path = ROOT / "models/vision/nomic-v1.5-int8.onnx"
    cache: Path = ROOT / "cache/visual"
    timeout: float = 30


def safe_path(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise ValueError("Invalid local image path")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Image path leaves visual directory")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("Image path leaves visual directory")
    return resolved
