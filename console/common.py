import json, os, subprocess, sys
from pathlib import Path

def _resolve_herdr_root():
    env = os.environ.get('HERDR_ROOT')
    if env and Path(env).exists():
        return Path(env)
    candidate = Path(__file__).resolve().parent.parent
    if (candidate / "herdr" / "__init__.py").exists() and (candidate / "bin").is_dir():
        return candidate
    if (HOME / "HAFlow" / "herdr" / "__init__.py").exists() and (HOME / "HAFlow" / "bin").is_dir():
        return HOME / "HAFlow"
    return candidate

def load_json(path,default):
    try:return json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception:return default

def save_json(path,data):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix(path.suffix+'.tmp'); tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8'); tmp.replace(path)

def run(cmd,timeout=20,check=False):
    try:r=subprocess.run(cmd,text=True,capture_output=True,timeout=timeout)
    except subprocess.TimeoutExpired as e: raise RuntimeError('命令超时: '+' '.join(cmd)) from e
    if check and r.returncode!=0: raise RuntimeError(r.stderr.strip() or r.stdout.strip() or '命令失败')
    return r

def run_json(cmd,timeout=20):
    r=run(cmd,timeout,True)
    try:return json.loads(r.stdout)
    except Exception as e: raise RuntimeError('无法解析 Herdr JSON') from e

def tail_log(kind='controller',n=180):
    mp={'controller':LOG_DIR/'controller.out.log','controller_err':LOG_DIR/'controller.err.log','notifier':LOG_DIR/'notifier.out.log','sentinel':LOG_DIR/'sentinel.out.log'}; p=mp.get(kind)
    if not p or not p.exists():return ''
    # Read only a bounded suffix; never split the entire controller log in RAM.
    budget=256*1024
    with p.open('rb') as handle:
        size=handle.seek(0,os.SEEK_END)
        length=min(size,budget)
        handle.seek(size-length)
        raw=handle.read(length)
    truncated=size>length
    if truncated:
        boundary=raw.find(b'\n')
        raw=raw[boundary+1:] if boundary>=0 else b''
    lines=raw.decode('utf-8',errors='ignore').splitlines()
    count=max(10,min(n,1000))
    text='\n'.join(lines[-count:])
    if truncated and len(lines)<count:
        text='[日志尾部已截断，较早内容超出读取上限]'+('\n'+text if text else '')
    return text

HOME=Path.home()
HERDR_ROOT=_resolve_herdr_root()
ROOT=HOME/'.herdr-controller'
sys.path.insert(0, str(HERDR_ROOT))
PROJECTS_FILE=ROOT/'projects.json'; WORKFLOWS_FILE=ROOT/'workflows.json'; TASKS_FILE=ROOT/'tasks.json'; POOLS_FILE=ROOT/'agent-pools.json'; SLOTS_FILE=ROOT/'pane-slots.json'; LOG_DIR=ROOT/'logs'
CONSOLE_STATIC_DIR=Path(__file__).resolve().parent/'static'
HOST='127.0.0.1'; PORT=int(os.environ.get('HERDR_CONSOLE_PORT','8765'))
CONSOLE_REQUEST_HEADER='X-HAFlow-Console-Request'
def console_allowed_hosts(port):
    port=int(port); hosts={f'127.0.0.1:{port}', f'localhost:{port}'}
    if port==80: hosts|={'127.0.0.1','localhost'}
    return hosts
def console_request_rejection(method,headers,port):
    """Return why a request is refused, or None when it comes from the local console UI.

    Host blocks DNS rebinding; Origin blocks cross-site POSTs; the custom header forces
    a browser POST through a CORS preflight, which the console never answers.
    """
    seen={str(k).lower():str(v).strip() for k,v in headers.items()}
    if seen.get('host','').lower() not in console_allowed_hosts(port): return 'host_not_allowed'
    if method=='POST':
        if seen.get(CONSOLE_REQUEST_HEADER.lower())!='1': return 'console_request_header_required'
        origin=seen.get('origin','').lower()
        if origin and origin not in {f'http://{h}' for h in console_allowed_hosts(port)}: return 'origin_not_allowed'
    return None
PRODUCT_NAME='HAFlow'; PRODUCT_TAGLINE='让人和多个 AI Agent 一起把事情做完'
HERDR_TASK=HERDR_ROOT/'bin'/'herdr-task'
ACTIVE={'pending','dispatched','working','blocked','agent_done','rework','completed','committed','integrated','cleanup_ready'}
