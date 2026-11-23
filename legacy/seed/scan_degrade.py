"""Turn a clean vector drawing PDF into a realistic bad office scan: an image-only PDF (no text layer).

Per page: render at 150 dpi, grayscale, slight rotation (0.4-2.0 deg), paper tint, uneven lighting,
sensor noise, a little blur, then JPEG compression at low quality. Deterministic per seed.
"""

import io
import random
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image, ImageFilter


def degrade(src: Path, dst: Path, seed: str, dpi: int = 150) -> dict[str, float]:
    rng = random.Random(seed)
    nrng = np.random.default_rng(rng.randint(0, 2**31))
    angle = rng.choice([-1, 1]) * rng.uniform(0.4, 2.0)
    quality = rng.randint(25, 45)
    doc_in = pymupdf.open(src)
    doc_out = pymupdf.open()
    for page in doc_in:
        pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
        img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
        img = img.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=245)
        a = np.asarray(img, dtype=np.float64)
        h, w = a.shape
        light = np.linspace(0.92, 1.0, w)[None, :] * np.linspace(1.0, 0.95, h)[:, None]  # uneven lighting
        a = a * light * 0.97 + 6  # grey paper, never pure white
        a += nrng.normal(0, 9, a.shape)  # sensor noise
        img = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.6))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        out = doc_out.new_page(width=page.rect.width, height=page.rect.height)
        out.insert_image(out.rect, stream=buf.getvalue())
    doc_out.set_metadata({"producer": "Office scanner", "creator": "Scan to PDF"})
    doc_out.save(dst, garbage=3, deflate=True)
    return {"rotation_deg": round(angle, 2), "jpeg_quality": quality, "dpi": dpi}
