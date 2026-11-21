"""Bounded decoding of untrusted bytes. No uploads are written to disk."""
from io import BytesIO
import warnings

from buyer.vision.config import MAX_BYTES, MAX_SIDE


class InvalidImage(ValueError):
    pass


def decode_image(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_BYTES:
        raise InvalidImage("Выберите одно изображение JPEG, PNG или WebP размером до 5 МБ.")
    from PIL import Image, ImageOps
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as probe:
                if probe.format not in {"JPEG", "PNG", "WEBP"}:
                    raise InvalidImage("Поддерживаются только JPEG, PNG и WebP.")
                if max(probe.size) > MAX_SIDE or min(probe.size) < 1:
                    raise InvalidImage("Максимальная сторона изображения — 2048 пикселей.")
                if getattr(probe, "n_frames", 1) != 1:
                    raise InvalidImage("Выберите неподвижное изображение.")
                probe.verify()
            with Image.open(BytesIO(raw)) as image:
                image.load()
                return ImageOps.exif_transpose(image).convert("RGB")
    except InvalidImage:
        raise
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise InvalidImage("Не удалось прочитать изображение. Файл повреждён или имеет неподдерживаемый формат.") from None
