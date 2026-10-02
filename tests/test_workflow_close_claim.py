import importlib
import multiprocessing as mp
from types import SimpleNamespace
import pytest


def module():
    assert importlib.util.find_spec('herdr.workflow_close'), 'durable close ownership missing'
    return importlib.import_module('herdr.workflow_close')


def test_completed_retry_and_dry_run(tmp_path):
    m=module(); store=SimpleNamespace(db_path=tmp_path/'state.db')
    with m.workflow_close_claim(store,'wf',dry_run=True) as c: assert c.state=='dry_run'
    with m.workflow_close_claim(store,'wf') as c:
        assert c.state=='owner'; oid=c.operation_id; c.complete({'tabs_closed':['t']})
    with m.workflow_close_claim(store,'wf') as c:
        assert c.state=='completed' and c.operation_id==oid and c.receipt['tabs_closed']==['t']


def test_crash_resume_skips_foreign_identity(tmp_path):
    m=module(); store=SimpleNamespace(db_path=tmp_path/'state.db'); calls=[]
    with m.workflow_close_claim(store,'wf') as c:
        oid=c.operation_id
        with pytest.raises(RuntimeError): c.action('pane:p',{'run':'old'},lambda identity:True,lambda:(_ for _ in ()).throw(RuntimeError('crash')))
    with m.workflow_close_claim(store,'wf') as c:
        assert c.operation_id==oid
        assert c.action('pane:p',{'run':'old'},lambda identity:False,lambda:calls.append('deleted'))['status']=='skipped_foreign'
    assert calls==[]


def hold(db,entered,release,q):
    from herdr.workflow_close import workflow_close_claim
    with workflow_close_claim(SimpleNamespace(db_path=db),'wf') as c:
        q.put(c.state); entered.set(); assert release.wait(10); c.complete({'done':True})


def compete(db,entered,q):
    from herdr.workflow_close import workflow_close_claim
    assert entered.wait(10)
    with workflow_close_claim(SimpleNamespace(db_path=db),'wf') as c:q.put(c.state)


def test_independent_process_unique_owner(tmp_path):
    module(); ctx=mp.get_context('spawn'); entered=ctx.Event(); release=ctx.Event(); q=ctx.Queue(); db=str(tmp_path/'state.db')
    a=ctx.Process(target=hold,args=(db,entered,release,q)); b=ctx.Process(target=compete,args=(db,entered,q)); a.start(); b.start()
    try: assert {q.get(timeout=10),q.get(timeout=10)}=={'owner','in_progress'}
    finally: release.set(); a.join(10); b.join(10)
    assert a.exitcode==b.exitcode==0


def crash(db,q):
    import os
    from herdr.workflow_close import workflow_close_claim
    with workflow_close_claim(SimpleNamespace(db_path=db),'wf') as c:
        q.put(c.operation_id); q.close(); q.join_thread(); os._exit(0)


def test_process_death_releases_lock_same_operation(tmp_path):
    m=module(); ctx=mp.get_context('spawn'); q=ctx.Queue(); db=str(tmp_path/'state.db'); p=ctx.Process(target=crash,args=(db,q)); p.start(); oid=q.get(timeout=10); p.join(10); assert p.exitcode==0
    with m.workflow_close_claim(SimpleNamespace(db_path=db),'wf') as c: assert c.state=='owner' and c.operation_id==oid


def test_actions_not_repeated_and_options_stable(tmp_path):
    m=module(); store=SimpleNamespace(db_path=tmp_path/'state.db'); calls=[]
    with m.workflow_close_claim(store,'wf',options={'purge':False}) as c:
        c.action('pane:p',{'run':'r'},lambda i:True,lambda:calls.append(1))
    with m.workflow_close_claim(store,'wf',options={'purge':False}) as c:
        c.action('pane:p',{'run':'r'},lambda i:True,lambda:calls.append(2))
    assert calls==[1]
    with pytest.raises(ValueError,match='options differ'):
        with m.workflow_close_claim(store,'wf',options={'purge':True}):pass


def test_reopen_preserves_old_action_identity_and_new_operation(tmp_path):
    from herdr.state_store import SQLiteStateStore
    m=module(); store=SQLiteStateStore(tmp_path/'state.db');store.save_workflow({'workflow_id':'wf','status':'running'})
    calls=[]
    with m.workflow_close_claim(store,'wf') as c:
        old=c.operation_id
        c.action('pane:p',{'run':'old'},lambda i:True,lambda:calls.append('old'))
        c.complete({'closed':'old'})
    store.save_workflow({'workflow_id':'wf','status':'in_progress','reopened_at':'new-generation'})
    with m.workflow_close_claim(store,'wf') as c:
        assert c.state=='owner' and c.operation_id!=old
        c.action('pane:p',{'run':'new'},lambda i:True,lambda:calls.append('new'))
        c.complete({'closed':'new'})
    import sqlite3,json
    with sqlite3.connect(store.db_path) as db:
        rows=db.execute('SELECT operation_id,identity FROM workflow_close_actions').fetchall()
    assert len(rows)==2 and any(oid==old and json.loads(identity)=={'run':'old'} for oid,identity in rows)
    assert calls==['old','new']


def test_legacy_close_journal_migrates_without_losing_receipt(tmp_path):
    import sqlite3,json
    m=module(); store=SimpleNamespace(db_path=tmp_path/'state.db')
    with sqlite3.connect(store.db_path) as db:
        db.execute('CREATE TABLE workflow_close_operations (workflow_id TEXT PRIMARY KEY,operation_id TEXT UNIQUE NOT NULL,state TEXT NOT NULL,options TEXT NOT NULL,receipt TEXT)')
        db.execute('INSERT INTO workflow_close_operations VALUES (?,?,?,?,?)',('wf','old-operation','completed','{}',json.dumps({'old':True})))
    with m.workflow_close_claim(store,'wf') as c:
        assert c.state=='completed' and c.operation_id=='old-operation' and c.receipt=={'old':True}
    with sqlite3.connect(store.db_path) as db:
        assert db.execute('SELECT operation_id FROM workflow_close_operations_legacy').fetchone()[0]=='old-operation'
