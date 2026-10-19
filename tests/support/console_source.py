"""Read Console implementation sources for broad static contract tests."""
from pathlib import Path

def console_source(root):
    console = Path(root) / 'console'
    paths = sorted(console.glob('*.py')) + [console / 'static' / 'console.html']
    return "\n".join(path.read_text(encoding='utf-8') for path in paths)
