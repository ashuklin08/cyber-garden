"""The pinned official CLIPImageProcessor configuration, without Transformers."""


def preprocess(image):
    import numpy as np
    from PIL import Image
    # Official size is height=width=224, NOT shortest_edge=224. Center crop is
    # consequently a no-op. resample=3 is Pillow bicubic, rescale_factor=1/255.
    pixels = np.asarray(image.resize((224, 224), Image.Resampling.BICUBIC), dtype=np.float32) / 255
    mean = np.array([.48145466, .4578275, .40821073], dtype=np.float32)
    std = np.array([.26862954, .26130258, .27577711], dtype=np.float32)
    return np.ascontiguousarray(((pixels - mean) / std).transpose(2, 0, 1)[None])


def normalize(vector):
    import numpy as np
    vector = np.asarray(vector, dtype=np.float32)
    norm = np.linalg.norm(vector)
    if vector.ndim != 1 or not np.isfinite(vector).all() or not norm > 0:
        raise ValueError("Invalid image embedding")
    return vector / norm
