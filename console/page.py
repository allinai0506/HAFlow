"""Load and brand the canonical desktop Console page template."""
from pathlib import Path

from console import common

HTML_TEMPLATE = (Path(__file__).resolve().parent / 'static' / 'console.html').read_text(encoding='utf-8')
HTML = HTML_TEMPLATE.replace('__PRODUCT_NAME__', common.PRODUCT_NAME).replace(
    '__PRODUCT_TAGLINE__', common.PRODUCT_TAGLINE,
)
