"""PNG inspection and lossless create-only recompression; derived alpha audit from GREA."""
from .io import safe, sha
from .errors import fail
from .output_limits import digest_file


def inspect(path):
    from PIL import Image
    path = safe(path)
    before = digest_file(path)
    with Image.open(path) as image:
        if image.width * image.height > 20_000_000 or getattr(image, "n_frames", 1) != 1:
            fail("IMAGE_SCOPE", str(path), "Static image up to 20 million pixels required")
        rgba = image.convert("RGBA")
        alpha = rgba.getchannel("A")
        histogram = alpha.histogram()
        result = dict(path=str(path), sha256=before, bytes=path.stat().st_size,
                      width=image.width, height=image.height, format=image.format,
                      visibleBounds=alpha.getbbox(), alphaExtrema=alpha.getextrema(),
                      transparentPixels=histogram[0], partialAlphaPixels=sum(histogram[1:255]),
                      decodedRGBASha256=sha(rgba.tobytes()))
    if digest_file(path) != before:
        fail("SOURCE_CHANGED", str(path), "Source changed during image inspection")
    return result


def compress(source, output, apply=False, expected=None):
    from PIL import Image
    source, output = safe(source), safe(output)
    report = inspect(source)
    if source == output or output.exists():
        fail("OUTPUT_EXISTS", str(output), "Compression output must be a new independent file")
    if report["format"] != "PNG" or output.suffix.lower() != ".png":
        fail("IMAGE_FORMAT", str(source), "v1 compression accepts static PNG to PNG only")
    report.update(output=str(output), algorithm="Pillow PNG optimize/compress_level=9",
                  lossless="decoded RGBA", dimensionsPreserved=True, alphaPreserved=True,
                  metadata="ICC/EXIF/DPI preserved when present; text chunks are not copied",
                  status="planned")
    if not apply:
        return report
    if expected != report["sha256"]:
        fail("SOURCE_CHANGED", str(source), "--expected-source must equal the inspected source SHA256")
    # Encode into a private buffer first. Never leave an unverified or partial destination.
    from io import BytesIO
    stream = BytesIO()
    with Image.open(source) as image:
        if image.mode not in ("RGB", "RGBA", "L", "LA", "P", "1"):
            fail("IMAGE_MODE", str(source), "Unsupported PNG mode; no implicit quantization")
        options = {k: image.info[k] for k in ("icc_profile", "exif", "dpi", "transparency") if k in image.info}
        if "transparency" in options and image.mode in ("RGBA", "LA"):
            options.pop("transparency")
        image.save(stream, format="PNG", optimize=True, compress_level=9, **options)
    payload = stream.getvalue()
    with Image.open(BytesIO(payload)) as result:
        if sha(result.convert("RGBA").tobytes()) != report["decodedRGBASha256"]:
            fail("IMAGE_PIXELS_CHANGED", str(source), "Lossless verification failed")
    if digest_file(source) != expected:
        fail("SOURCE_CHANGED", str(source), "Source changed during compression")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as target:
        target.write(payload)
    report.update(status="compressed", outputSha256=sha(payload), outputBytes=len(payload),
                  savedBytes=report["bytes"] - len(payload), verifiedDecodedRGBA=True)
    return report
