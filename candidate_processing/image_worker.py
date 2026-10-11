"""Decode candidate images in a disposable child process, never in the API process.

Only decode_rgb() imports Pillow. The parent can read policy constants without
loading a codec. This file also runs directly under Python's isolated (-I) mode.
"""

from io import BytesIO
import json
import sys
import warnings

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_SIDE = 10_000
CPU_LIMIT_SECONDS = 10
ADDRESS_SPACE_BYTES = 2 * 1024**3

INVALID_MESSAGES = {
    "empty_image": "No image bytes were supplied.",
    "image_too_large": "The image exceeds the 10 MiB byte limit.",
    "unsupported_image_format": "Only still JPEG and PNG images are supported.",
    "corrupt_image": "The image is corrupt, truncated, or has an invalid end marker.",
    "pixel_limit": "The image exceeds 20 million pixels or 10,000 pixels per side.",
    "animated_image": "Animated or multi-frame images are not supported.",
}


class InvalidImage(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(INVALID_MESSAGES[code])


def check_dimensions(width: int, height: int):
    if min(width, height) < 1 or max(width, height) > MAX_SIDE or width * height > MAX_PIXELS:
        raise InvalidImage("pixel_limit")


def decode_rgb(data: bytes):
    """Return an owned RGB image and detected MIME; call only inside the worker.

    Tests may call this helper with synthetic fixtures. Untrusted caller bytes
    must enter through IsolatedImageValidator to receive process limits.
    """
    from PIL import Image, ImageFile, ImageOps, UnidentifiedImageError

    if not data:
        raise InvalidImage("empty_image")
    if len(data) > MAX_IMAGE_BYTES:
        raise InvalidImage("image_too_large")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        expected_format, mime = "PNG", "image/png"
        complete = data.endswith(b"\x00\x00\x00\x00IEND\xaeB\x60\x82")
    elif data.startswith(b"\xff\xd8"):
        expected_format, mime = "JPEG", "image/jpeg"
        complete = data.endswith(b"\xff\xd9")
    else:
        raise InvalidImage("unsupported_image_format")
    if not complete:
        raise InvalidImage("corrupt_image")

    # These process-local settings also make direct synthetic-fixture tests
    # restore Pillow's globals afterwards.
    previous_pixels = Image.MAX_IMAGE_PIXELS
    previous_truncated = ImageFile.LOAD_TRUNCATED_IMAGES
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data), formats=[expected_format]) as image:
                check_dimensions(*image.size)
                if getattr(image, "n_frames", 1) != 1 or getattr(image, "is_animated", False):
                    raise InvalidImage("animated_image")
                image.verify()
            # verify() is not a full decode. Reopen and load every pixel.
            with Image.open(BytesIO(data), formats=[expected_format]) as image:
                check_dimensions(*image.size)
                image.load()
                with ImageOps.exif_transpose(image) as oriented:
                    check_dimensions(*oriented.size)
                    if "A" in oriented.getbands() or "transparency" in oriented.info:
                        with oriented.convert("RGBA") as rgba:
                            rgb = Image.new("RGB", rgba.size, "white")
                            with rgba.getchannel("A") as alpha:
                                rgb.paste(rgba, mask=alpha)
                    else:
                        rgb = oriented.convert("RGB")
                    rgb.info.clear()
                    return rgb, mime
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise InvalidImage("pixel_limit") from error
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError) as error:
        if isinstance(error, InvalidImage):
            raise
        raise InvalidImage("corrupt_image") from error
    finally:
        Image.MAX_IMAGE_PIXELS = previous_pixels
        ImageFile.LOAD_TRUNCATED_IMAGES = previous_truncated


def apply_resource_limits():
    import resource

    def cap(kind, value):
        _, hard = resource.getrlimit(kind)
        limit = value if hard == resource.RLIM_INFINITY else min(value, hard)
        resource.setrlimit(kind, (limit, limit))

    cap(resource.RLIMIT_CORE, 0)
    cap(resource.RLIMIT_CPU, CPU_LIMIT_SECONDS)
    if sys.platform.startswith("linux"):
        cap(resource.RLIMIT_AS, ADDRESS_SPACE_BYTES)
    elif sys.platform != "darwin":
        raise RuntimeError("Unsupported worker platform")


def main():
    analyze = sys.argv[1:] == ["--analyze"]
    data = b""
    try:
        apply_resource_limits()
    except (ImportError, OSError, ValueError, RuntimeError):
        report = {"status": "error", "code": "worker_limits_unavailable"}
    else:
        try:
            data = sys.stdin.buffer.read(MAX_IMAGE_BYTES + 1)
            rgb, mime = decode_rgb(data)
            with rgb:
                if analyze:
                    # -I excludes the working directory. Add only this trusted
                    # repository path for the existing detector/checkpoint code.
                    from pathlib import Path
                    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
                    from candidate_processing.image_processing import analyze_rgb
                    report = analyze_rgb(rgb, mime, len(data))
                else:
                    report = {"status": "ok", "mimeType": mime, "width": rgb.width, "height": rgb.height}
        except InvalidImage as error:
            report = {"status": "invalid", "code": error.code}
        except MemoryError:
            report = {"status": "error", "code": "image_memory_limit"}
        except Exception:
            # Do not send decoder exceptions, filenames, environment or bytes
            # through the result channel.
            report = {"status": "error", "code": "image_decode_failed"}
    if analyze and "analysis" not in report:
        invalid = report["status"] == "invalid"
        report = {
            "analysis": {"status": report["status"], "valid": False if invalid else None, "byteLength": len(data)},
            "crops": [],
            "errors": [{"stage": "validation" if invalid else "image", "code": report["code"],
                        "message": INVALID_MESSAGES[report["code"]] if invalid else "Image processing could not be completed.",
                        "retryable": False}],
        }
    print(json.dumps(report, allow_nan=False))


if __name__ == "__main__":
    main()
