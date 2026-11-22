import time
from app.config import ROOT,MODULES
from app.errors import DomainError
from app.values import scalar
from app.manifest import digest,manifest
from app import store,change_store,changes
from app.modules.m1_claims.rules import evaluate as stage1
from app.modules.m2_billitems.rules import evaluate as stage2
from app.modules.m3_changes.rules import evaluate as stage3
from app.modules.m4_increment.rules import evaluate as stage4
from app.modules.m5_deductions.rules import evaluate as stage5
from app.modules.m6_valuation.rules import evaluate as stage6
from app.modules.m7_certificates.rules import evaluate as stage7
STAGES=[stage1,stage2,stage3,stage4,stage5,stage6,stage7]
def load_sample(name):
    if name not in {"dirty.csv","clean.csv","legacy.csv"}: raise DomainError("BAD_SAMPLE","样例不存在",404)
    return (ROOT/"sample_data"/name).read_bytes()
def change_workspace(rows=None):
    """按审批回传证据重算变更裁决；rows 提供时附带该批次的即时影响。"""
    evidence=change_store.snapshot()
    return changes.workspace(evidence["events"],evidence["publications"],rows)
def calculate(raw,params=None,stop=6):
    params=params or {};started=time.monotonic();context={"parameters":params,"raw":raw,"quarantine":[]};stages=[];rows=[]
    evidence=change_store.snapshot()
    decision={"changes":None}
    for index,stage in enumerate(STAGES[:stop+1]):
        if index==2:
            # 定版裁决只依据两端审批证据，与报文到达顺序无关。
            # 注意：context["changes"] 留给 M3 阶段结果，裁决放在 change_workspace。
            context["change_workspace"]=changes.workspace(evidence["events"],evidence["publications"],rows)
        result=stage(raw if index==0 else rows,context)
        result=scalar(result);rows=result["rows"]
        context["quarantine"].extend(result.get("quarantine",[]))
        if index==0: context["intake"]=result["metrics"]
        result["cumulative_quarantine"]=list(context["quarantine"])
        context[MODULES[index][0]]=result
        stages.append(result)
        if index==2: decision["changes"]=context["change_workspace"]
    payload={"run_id":digest(raw,params,evidence)[:20],"stages":stages,"manifest":manifest(raw,params,started,evidence),"parameters":params}
    if stop>=2: payload["changes"]=decision["changes"]
    return payload
def execute(raw,params=None):
    result=calculate(raw,params);return store.save(result["run_id"],result)
def stage_preview(index,payload):
    if "rows" in payload and index>0:
        if not isinstance(payload["rows"],list) or any(not isinstance(row,dict) for row in payload["rows"]): raise DomainError("BAD_ROWS","记录需要对象列表")
        if not isinstance(payload.get("context",{}),dict): raise DomainError("BAD_CONTEXT","上下文需要对象")
        context=payload.get("context",{"parameters":{},"quarantine":[]})
        if index==2: context={**context,"change_workspace":change_workspace(payload["rows"])}
        return scalar(STAGES[index](payload["rows"],context))
    raw=payload.get("csv")
    raw=raw.encode() if isinstance(raw,str) else load_sample(payload.get("sample","dirty.csv"))
    return calculate(raw,payload.get("parameters",{}),index)["stages"][-1]
