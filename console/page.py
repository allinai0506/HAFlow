"""Compose and brand the Console's ordered, bundled page resources.

Includes are trusted repository assets, expanded before serving. Keeping scripts
inline preserves startup, global HTML event handlers, and atomic page delivery.
"""
from pathlib import Path
import re

from console import common

_STATIC = Path(__file__).resolve().parent / 'static'
_INCLUDE = re.compile(r'<!-- console:include (console/[a-z0-9_/-]+\.(?:css|js|html)) -->')


def compose_template():
    source = (_STATIC / 'console.html').read_text(encoding='utf-8')
    return _INCLUDE.sub(
        lambda match: (_STATIC / match[1]).read_text(encoding='utf-8'), source,
    )


HTML_TEMPLATE = compose_template()
HTML = HTML_TEMPLATE.replace('__PRODUCT_NAME__', common.PRODUCT_NAME).replace(
    '__PRODUCT_TAGLINE__', common.PRODUCT_TAGLINE,
)
