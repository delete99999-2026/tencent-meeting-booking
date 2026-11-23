# -*- coding: utf-8 -*-
"""服务层完整闭环：HTTP 提交 -> worker 真实预订(booked) -> 删除 -> cancel。"""
import json, os, sys, time, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timedelta
BASE = "http://127.0.0.1:8000"

def call(method, path, data=None, token=None):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
    headers = {"Content-Type":"application/json"}
    if token: headers["Authorization"]="Bearer "+token
    req = urllib.request.Request(BASE+path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))

# 1) 登录
st, r = call("POST","/api/login",{"username":"admin","password":"Admin@123456"})
print("LOGIN:",st, flush=True)
if st!=200: print(r); sys.exit(1)
token = r["token"]

# 2) 提交会议（30 分钟后开始，30 分钟预设档；提前足够 lead）
subject = "服务层闭环验证"
start = (datetime.now() + timedelta(minutes=30)).replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")
st, r = call("POST","/api/meetings",{"subject":subject,"start_at":start,"duration_min":30},token)
print("BOOK POST:",st,r, flush=True)
if st!=201: sys.exit(2)
mid = r["id"] if isinstance(r,dict) and "id" in r else None
print("meeting id:",mid, flush=True)

# 3) 轮询直到 booked / failed
status=None; tm_id=None
for i in range(90):
    time.sleep(2)
    st, lst = call("GET","/api/meetings?subject=%s"%urllib.parse.quote(subject),token=token)
    items = lst.get("items",[]) if isinstance(lst,dict) else lst
    m = next((x for x in items if x.get("id")==mid),None)
    if not m:
        print("meeting vanished",flush=True); break
    status=m.get("status"); tm_id=m.get("tm_meeting_id")
    if i%5==0: print("  poll %ds status=%s tm_id=%s err=%s"%(i*2,status,tm_id,m.get("tm_error")),flush=True)
    if status in ("booked","failed"): break
print("FINAL STATUS:",status,"tm_id:",tm_id, flush=True)
if status!="booked":
    print("BOOK NOT CONFIRMED",flush=True); sys.exit(3)

# 4) 删除 -> 触发 cancel
st, r = call("DELETE","/api/meetings/%s"%mid,token=token)
print("DELETE:",st,r, flush=True)

# 5) 轮询队列确认 cancel 任务完成
for i in range(60):
    time.sleep(2)
    st,q = call("GET","/api/queue",token=token)
    items = q.get("items",[]) if isinstance(q,dict) else q
    active=[t for t in items if t.get("meeting_id")==mid and t.get("status") in ("queued","running")]
    if i%5==0: print("  queue poll %ds active=%d"%(i*2,len(active)),flush=True)
    if not active:
        print("CANCEL TASK DONE",flush=True); break
print("SERVICE E2E COMPLETE",flush=True)
