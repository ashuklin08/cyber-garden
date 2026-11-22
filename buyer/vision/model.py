"""A verified local CPU model. This module never downloads or executes remote code."""
import hashlib
import time

from buyer.vision.config import WEIGHT_BYTES, WEIGHT_SHA256
from buyer.vision.embeddings import normalize, preprocess


def verify_weights(path):
    if not path.is_file():
        raise FileNotFoundError("missing_model")
    if path.stat().st_size != WEIGHT_BYTES:
        raise ValueError("model_integrity")
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != WEIGHT_SHA256:
            raise ValueError("model_integrity")


class VisionModel:
    def __init__(self, path, *, verified=False):
        started = time.perf_counter()
        if not verified:
            verify_weights(path)
        import onnxruntime as ort
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.enable_cpu_mem_arena = False
        options.log_severity_level = 3
        self.session = ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])
        self.load_ms = (time.perf_counter() - started) * 1000

    def embed(self, image):
        hidden = self.session.run(["last_hidden_state"], {"pixel_values": preprocess(image)})[0]
        return normalize(hidden[0, 0])
