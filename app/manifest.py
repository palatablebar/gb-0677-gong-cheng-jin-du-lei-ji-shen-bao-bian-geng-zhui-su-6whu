import hashlib,json,time
from datetime import datetime,timezone
from app.config import RULE_VERSION
def evidence_digest(evidence=None):
    """审批证据摘要。证据变化会产生新批次，旧证据不被覆盖。"""
    if evidence is None: return None
    canonical=json.dumps(evidence,sort_keys=True,ensure_ascii=False,default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()
def digest(raw,params=None,evidence=None):
    return hashlib.sha256(raw+json.dumps(params or {},sort_keys=True,ensure_ascii=False).encode()+RULE_VERSION.encode()+(evidence_digest(evidence) or "").encode()).hexdigest()
def manifest(raw,params,started,evidence=None):
    return {"input_sha256":hashlib.sha256(raw).hexdigest(),"rule_version":RULE_VERSION,"parameters":params,"change_evidence_sha256":evidence_digest(evidence),"generated_at":datetime.now(timezone.utc).isoformat(),"elapsed_ms":round((time.monotonic()-started)*1000,2)}
