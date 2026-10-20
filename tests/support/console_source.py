"""Read Console implementation sources for broad static contract tests."""
from pathlib import Path

def console_source(root):
    console = Path(root) / 'console'
    from console.page import HTML_TEMPLATE
    paths = sorted(console.glob('*.py'))
    return "\n".join(path.read_text(encoding='utf-8') for path in paths) + '\n' + HTML_TEMPLATE


def module_body(name):
    """Isolate a capability for existing focused tests with explicit fake inputs."""
    path = Path(__file__).resolve().parents[2] / 'console/static/console/scripts' / (name + '.js')
    source = path.read_text(encoding='utf-8')
    import re
    start = re.search(r'\n\}\)\{\n', source)
    return (source[start.end():] if start else source.split('\n', 2)[2]).rsplit('\nreturn {', 1)[0]
