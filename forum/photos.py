"""
Check and clean an uploaded photo before anything is stored.

Every photo is decoded and saved again from its pixels, so what ends up on
disk is only the picture:
- EXIF, including the GPS position phones record, is not written back. On a
  travel forum the first and last photos of a trip are often taken at home.
- XMP and comments, which can also hold a location, are dropped too.
- Anything that isn't really a JPEG, PNG or WebP image is refused, whatever
  its file name or the browser says. Re-encoding also defeats files that are
  an image and something else at once.

This runs during form validation, before the database transaction, so a bad
photo becomes a form error and nothing is half-saved.
"""

from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Final
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import UploadedFile
from PIL import Image, ImageOps, UnidentifiedImageError

# Pillow's format name -> the extension stored files get.
ALLOWED_FORMATS: Final[dict[str, str]] = {'JPEG': 'jpg', 'PNG': 'png', 'WEBP': 'webp'}
# Cameras (and some phones) write .JPG files that hold more than one image: the photo plus a
# large preview. Pillow calls those MPO. They are JPEGs, so treat them as one and keep the
# main image; the extra frames, and whatever metadata they carry, are left behind.
FORMAT_ALIASES: Final[dict[str, str]] = {'MPO': 'JPEG'}
MAX_UPLOAD_BYTES: Final[int] = 10 * 1024 * 1024  # room for camera JPEGs; the stored copy is re-encoded smaller
MAX_PIXELS: Final[int] = 50_000_000              # 50 megapixels; refuses "decompression bombs"
THUMBNAIL_WIDTHS: Final[tuple[int, ...]] = (800, 1600)
# Profile photos are shown at 88 px at the largest, so this covers even a high-density screen.
AVATAR_SIZE: Final[int] = 256
ENCODE_OPTIONS: Final[dict[str, dict[str, Any]]] = {
    'JPEG': {'quality': 85, 'optimize': True, 'progressive': True},
    'PNG': {'optimize': True},
    'WEBP': {'quality': 85},
}


@dataclass(frozen=True)
class PreparedPhoto:
    """A cleaned photo, ready to save: the full-size image and its smaller copies."""

    original: ContentFile
    width: int
    height: int
    thumbnails: dict[int, ContentFile] = field(default_factory=dict)  # by width; only those smaller than the photo


def prepare_photo(upload: UploadedFile) -> PreparedPhoto:
    image, image_format = _cleaned_image(upload)
    stem = uuid4().hex  # a random name: members' own file names can give away more than they mean to
    extension = ALLOWED_FORMATS[image_format]
    return PreparedPhoto(
        original=_encode(image, image_format, f'{stem}.{extension}'),
        width=image.width,
        height=image.height,
        thumbnails={
            target: _encode(_scaled_to_width(image, target), image_format, f'{stem}-{target}.{extension}')
            for target in THUMBNAIL_WIDTHS if image.width > target
        },
    )


def prepare_avatar(upload: UploadedFile) -> ContentFile:
    """One small square photo for a member's avatar, cleaned the same way as any other.

    Avatars are shown in a square everywhere, so the middle of the picture is cut
    out here rather than squashed by the browser. A picture already smaller than
    AVATAR_SIZE is cut square but never enlarged.
    """
    image, image_format = _cleaned_image(upload)
    side = min(AVATAR_SIZE, image.width, image.height)
    square = ImageOps.fit(image, (side, side), Image.Resampling.LANCZOS)
    return _encode(square, image_format, f'{uuid4().hex}.{ALLOWED_FORMATS[image_format]}')


def _cleaned_image(upload: UploadedFile) -> tuple[Image.Image, str]:
    """Check that the upload really is a photo, and give it back decoded and upright.

    Nothing of the original file survives this: the caller saves the pixels again,
    so EXIF (with its GPS position), XMP and comments are left behind.
    """
    name = upload.name or 'photo'
    if upload.size is not None and upload.size > MAX_UPLOAD_BYTES:
        raise ValidationError(f'{name} is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.')

    try:
        # First pass reads only the header: format and dimensions, without decoding the pixels.
        with Image.open(upload) as probe:
            image_format = FORMAT_ALIASES.get(probe.format or '', probe.format or '')
            width, height = probe.size
            if image_format not in ALLOWED_FORMATS:
                raise ValidationError(f'{name} isn’t a JPEG, PNG or WebP photo.')
            if width * height > MAX_PIXELS:
                raise ValidationError(f'{name} is too large ({width} × {height} pixels).')
            probe.verify()  # catches truncated or corrupt files; the image can't be used after this

        upload.seek(0)
        with Image.open(upload) as opened:
            opened.load()
            image = ImageOps.exif_transpose(opened)  # turn it the way the camera was held
    except ValidationError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise ValidationError(f'{name} couldn’t be read as a photo.')

    return image, image_format


def _scaled_to_width(image: Image.Image, width: int) -> Image.Image:
    if image.mode == 'P':
        image = image.convert('RGBA')  # palette images resize badly
    height = max(1, round(image.height * width / image.width))
    return image.resize((width, height), Image.Resampling.LANCZOS)


def _encode(image: Image.Image, image_format: str, name: str) -> ContentFile:
    options = dict(ENCODE_OPTIONS[image_format])
    # Keep only the colour profile, so colours look right. Everything else the original
    # carried (EXIF with GPS, XMP, comments) is dropped by starting from empty metadata.
    icc_profile = image.info.get('icc_profile')
    image.info = {}
    if icc_profile:
        options['icc_profile'] = icc_profile
    if image_format == 'JPEG' and image.mode not in ('RGB', 'L'):
        image = image.convert('RGB')  # CMYK and similar become plain RGB
    buffer = BytesIO()
    image.save(buffer, image_format, **options)
    return ContentFile(buffer.getvalue(), name=name)
