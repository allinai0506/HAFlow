"""Test-only transparent facade spies; record actual originating call sites."""
import ast,functools,inspect,json,os,pathlib,sys,textwrap
import pytest
ROOT=pathlib.Path(__file__).resolve().parents[1]
MATRIX=json.loads((ROOT/'tests/fixtures/state_db_patch_sites.json').read_text())
HITS={};SITES={};ORDINALS={}
OUTPUT=None
state_db = None
def pytest_collection_finish(session):
 global state_db, OUTPUT
 output_dir=os.environ.get('HERDR_PATCH_PROBE_DIR')
 if not output_dir:
  raise pytest.UsageError('HERDR_PATCH_PROBE_DIR must be a fresh isolated directory')
 candidate=pathlib.Path(output_dir).resolve()/'patch-real-hits.json'
 if candidate.exists():
  raise pytest.UsageError('prior receipt exists; refusing cross-round accumulation')
 OUTPUT=candidate
 OUTPUT.parent.mkdir(parents=True,exist_ok=True)
 from herdr import state_db
 for index,row in enumerate(MATRIX['internal']):
  name=row['scope'][0];symbol=row['symbol'];function=getattr(state_db,name)
  key=(name,symbol);ordinal=ORDINALS.get(key,0);ORDINALS[key]=ordinal+1
  tree=ast.parse(textwrap.dedent(inspect.getsource(function)))
  refs=sorted((n for n in ast.walk(tree) if (isinstance(n,ast.Name) and isinstance(n.ctx,ast.Load) and n.id==symbol) or (isinstance(n,ast.Attribute) and n.attr==symbol)),key=lambda n:(n.lineno,n.col_offset))
  node=refs[ordinal];line=inspect.getsourcelines(function)[1]+node.lineno-1
  SITES.setdefault((str(pathlib.Path(inspect.getsourcefile(function)).resolve()),line,symbol),[]).append(index)

def mark(symbol,frame,test):
 key=(str(pathlib.Path(frame.f_code.co_filename).resolve()),frame.f_lineno,symbol)
 for index in SITES.get(key,[]):HITS.setdefault(str(index),set()).add(test)

@pytest.fixture(autouse=True)
def facade_real_call_spies(monkeypatch,request):
 for symbol in sorted({r['symbol'] for r in MATRIX['internal']} - {'CONTROLLER_DIR'}):
  original=getattr(state_db,symbol)
  def make(original,symbol):
   @functools.wraps(original)
   def spy(*args,**kwargs):
    mark(symbol,sys._getframe(1),request.node.nodeid)
    return original(*args,**kwargs)
   return spy
  monkeypatch.setattr(state_db,symbol,make(original,symbol))
 original=state_db.CONTROLLER_DIR
 class TracedPath(type(original)):
  def __truediv__(self,other):
   mark('CONTROLLER_DIR',sys._getframe(1),request.node.nodeid)
   return super().__truediv__(other)
 monkeypatch.setattr(state_db,'CONTROLLER_DIR',TracedPath(original))

def pytest_sessionfinish(session,exitstatus):
 if OUTPUT is None:
  return
 hits={key:sorted(tests) for key,tests in HITS.items()}
 OUTPUT.write_text(json.dumps(hits,indent=2)+'\n')
 missing=sorted(set(map(str,range(len(MATRIX['internal']))))-set(hits),key=int)
 print('\nREAL_PATCH_SITES',f"{len(hits)}/{len(MATRIX['internal'])}")
 if missing:
  print('MISSING_PATCH_SITES',','.join(missing))
  session.exitstatus=pytest.ExitCode.TESTS_FAILED
