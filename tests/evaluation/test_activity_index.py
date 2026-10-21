from datetime import datetime,timezone
import json
import time
from runtime.activity_index import trace_activity


def test_incremental_trace_index_and_partial_line(tmp_path):
    path=tmp_path/"traces.jsonl";db=tmp_path/"index.db"
    event=json.dumps({"timestamp":datetime.now(timezone.utc).isoformat(),"status":"ok","tool_name":"search"})
    path.write_text(event+"\n"+event,encoding="utf-8")
    one=trace_activity(path,time.time()-60,database=db)
    assert one["successful"]==1
    two=trace_activity(path,time.time()-60,database=db)
    assert two["successful"]==1 and two["index_read_bytes"]==0
    with path.open("a",encoding="utf-8") as stream:stream.write("\n")
    assert trace_activity(path,time.time()-60,database=db)["successful"]==2


def test_in_place_rewrite_detected_and_replay_not_counted_twice(tmp_path):
    path=tmp_path/'trace.jsonl';db=tmp_path/'index.db'
    at=datetime.now(timezone.utc).isoformat()
    one=json.dumps({'timestamp':at,'status':'ok','tool_name':'first','trace_id':'first'})+'\n'
    two=json.dumps({'timestamp':at,'status':'ok','tool_name':'second','trace_id':'second'})+'\n'
    path.write_text(one,encoding='utf-8')
    assert trace_activity(path,time.time()-60,database=db)['successful']==1
    path.write_text(two+one,encoding='utf-8')
    result=trace_activity(path,time.time()-60,database=db)
    assert result['successful']==2 and result['index_read_bytes']>0
    assert trace_activity(path,time.time()-60,database=db)['index_read_bytes']==0
