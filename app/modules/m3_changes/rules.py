from datetime import datetime
from app.values import number
from app.reasons import reject
from app.errors import DomainError

CHANGES={"C0":{"approved":True,"effective":"2026-01","price":"100.00"},"C1":{"approved":True,"effective":"2026-11","price":"120.00"},"DRAFT":{"approved":False,"effective":"2026-10","price":"140.00"}}

def _legacy_versions(context):
    """没有审批证据时的历史内联台账（保持旧批次兼容）。"""
    return context.get("parameters",{}).get("changes",CHANGES)

def _adjudication(change_id,context):
    """返回审批工作台给出的该变更状态；无工作台上下文时返回 None。"""
    workspace=context.get("change_workspace")
    if workspace is None:return None
    return next((s for s in workspace.get("changes",[]) if s["change_id"]==change_id),None)

def _blocked(state):
    settlement=state.get("settlement") or {}
    code=settlement.get("code") or state.get("reason_code") or "CHANGE_PENDING"
    reason=settlement.get("reason") or state.get("pending_reason") or "尚未形成唯一生效版本"
    return code,reason

def _effective_period_ok(row,effective):
    if not effective:return False
    try:return len(row.get("period",""))==7 and datetime.strptime(row["period"],"%Y-%m") is not None and row["period"]>=effective
    except (TypeError,ValueError):return False

def evaluate(rows,context):
    good=[];bad=[];versions=_legacy_versions(context)
    for row in rows:
        change_id=row.get("change_id")
        state=_adjudication(change_id,context)
        if state is None:
            rule=versions.get(change_id)
            if not rule or not rule.get("approved"):bad.append(reject(row,"UNAPPROVED_CHANGE","变更尚未批准"));continue
            price=number(rule["price"]);effective=rule["effective"];extra={}
        else:
            settlement=state.get("settlement") or {}
            if not settlement.get("usable"):
                code,reason=_blocked(state);bad.append(reject(row,code,reason));continue
            price=number(settlement["price"]);effective=settlement["effective"]
            extra={"change_status":state["status"],
                   "settlement_status":settlement["status"],
                   "effective_version":settlement["version"],
                   "applicable_price":settlement["price"]}
        if not _effective_period_ok(row,effective):
            # 结算沿用的旧版仍按其原生效周期判断，不因新版未发布而放宽。
            bad.append(reject(row,"CHANGE_NOT_EFFECTIVE",f"变更 {change_id} 自 {effective or '未定'} 生效"));continue
        try:
            if price<0:raise DomainError("BAD_PRICE","单价不能为负")
            good.append({**row,**extra,"unit_price":str(price),"approved_change":change_id})
        except DomainError as e:bad.append(reject(row,e.code,e.message))
    return {"rows":good,"quarantine":bad,"metrics":{"accepted":len(good),"versions":sorted(set(r["approved_change"] for r in good))}}
