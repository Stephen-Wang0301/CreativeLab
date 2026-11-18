"""Contract for a future image provider. The application never calls one by default."""

from dataclasses import dataclass
import importlib


@dataclass(frozen=True)
class ImageRequest:
    brief: str
    title: str
    concept: str
    elements: tuple[str, ...]
    refinement: str
    round_number: int
    request_id: str
    count: int = 2


@dataclass(frozen=True)
class ImageCandidate:
    content: bytes
    mime_type: str


def load_provider(target):
    if not target:
        return None
    module, separator, function = target.partition(':')
    if not separator or not module or not function:
        raise ValueError('图像插件格式为 Python模块:函数。')
    try:
        handler = getattr(importlib.import_module(module), function)
    except (ImportError, AttributeError) as error:
        raise ValueError('无法加载图像插件。请核对模块文件、函数名称和插件依赖。') from error
    if not callable(handler):
        raise ValueError('图像插件必须是可调用的函数。')
    return handler


def validate_candidates(candidates):
    extensions = {'image/png': ('png', b'\x89PNG\r\n\x1a\n'),
                  'image/jpeg': ('jpg', b'\xff\xd8\xff'),
                  'image/webp': ('webp', b'RIFF')}
    if not isinstance(candidates, (list, tuple)) or len(candidates) != 2:
        raise ValueError('每轮必须返回两张图片。')
    result = []
    for item in candidates:
        if not isinstance(item, ImageCandidate) or item.mime_type not in extensions:
            raise ValueError('仅接收 PNG、JPEG、WebP 图片。')
        ext, signature = extensions[item.mime_type]
        if (not isinstance(item.content, bytes) or not 16 <= len(item.content) <= 8_000_000
                or not item.content.startswith(signature)
                or (ext == 'webp' and item.content[8:12] != b'WEBP')):
            raise ValueError('图像文件格式或大小不符合要求。')
        result.append((ext, item.content))
    return result
