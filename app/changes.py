"""变更单定版裁决。

技术核定(technical)与商务定价(commercial)由两个部门独立回传。裁决只依据各端
业务修订号(revision)与变更文件版本(change_version)，与报文到达顺序无关，因此
网络延迟造成的乱序不会改变结论。同一变更在任一时刻至多形成一个唯一生效版本：

* 在两端最终态度均为“通过”的版本中，取业务修订号最高者；
* 商务定价给出该版本单价与生效周期；两端若同时给出，必须一致；
* 更高版本只到单端、被否决或被撤回时，不推翻仍有效的旧版本，只作待对齐提示；
* 两端均无可用版本时不产生生效版本（待定 / 已撤回）。

审批事件只追加不删除：撤回后仍保留完整回传轨迹，且撤回影响始终可追溯。
生效版本还必须显式“发布”才允许进入本期计量；发布失败时旧发布指针保持
不变，旧版继续适用，新版本不得入账。
"""
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal

from app.errors import DomainError
from app.values import money, number

TECHNICAL = "technical"
COMMERCIAL = "commercial"
ENDPOINTS = (TECHNICAL, COMMERCIAL)
ENDPOINT_LABELS = {TECHNICAL: "技术核定", COMMERCIAL: "商务定价"}

DECISIONS = {"approved", "rejected", "withdrawn"}
DECISION_LABELS = {"approved": "通过", "rejected": "否决", "withdrawn": "撤回"}

STATUS_LABELS = {"pending": "待定", "effective": "已生效", "withdrawn": "已撤回"}

# 未形成生效版本时的原因码，供各阶段隔离行复用。
PENDING_NO_EVENTS = "UNAPPROVED_CHANGE"
PENDING_HALF = "CHANGE_PENDING"
PENDING_VERSION_MISMATCH = "CHANGE_VERSION_MISMATCH"
REJECTED = "CHANGE_REJECTED"
WITHDRAWN = "CHANGE_WITHDRAWN"
UNPUBLISHED = "CHANGE_UNPUBLISHED"
STALE_PUBLICATION = "CHANGE_PUBLICATION_STALE"

FAILED_PUBLICATION = "PUBLICATION_FAILED"
STALE_VERSION = "STALE_VERSION"

_ENDPOINT_ALIASES = {
    "technical": TECHNICAL, "tech": TECHNICAL, "技术核定": TECHNICAL, "技术": TECHNICAL,
    "commercial": COMMERCIAL, "business": COMMERCIAL, "商务定价": COMMERCIAL,
    "商务": COMMERCIAL, "定价": COMMERCIAL,
}
_DECISION_ALIASES = {
    "approved": "approved", "approve": "approved", "通过": "approved", "同意": "approved",
    "rejected": "rejected", "reject": "rejected", "否决": "rejected",
    "退回": "rejected", "不通过": "rejected",
    "withdrawn": "withdrawn", "withdraw": "withdrawn", "撤回": "withdrawn", "撤销": "withdrawn",
}


def valid_period(value):
    try:
        return len(value) == 7 and datetime.strptime(value, "%Y-%m") is not None
    except (TypeError, ValueError):
        return False


def _alias(value, aliases, field):
    text = str(value or "").strip()
    if not text:
        raise DomainError("BAD_APPROVAL", f"缺少{field}")
    return aliases.get(text, aliases.get(text.lower(), text))


def normalize_event(raw):
    """把一条审批回传规范化为可比较、可重放的固定结构。

    event_id 为幂等键；未提供时按业务内容生成，“同一审批重试”因此天然去重。
    修改已提交审批必须提升 revision，否则视为内容冲突。
    """
    if not isinstance(raw, dict):
        raise DomainError("BAD_APPROVAL", "审批回传需要对象")
    change_id = str(raw.get("change_id") or "").strip()
    if not change_id:
        raise DomainError("BAD_APPROVAL", "缺少变更单号 change_id")
    endpoint = _alias(raw.get("endpoint"), _ENDPOINT_ALIASES, "审批端别 endpoint")
    if endpoint not in ENDPOINTS:
        raise DomainError("BAD_ENDPOINT", "审批端别必须为 technical 或 commercial")
    decision = _alias(raw.get("decision"), _DECISION_ALIASES, "审批结论 decision")
    if decision not in DECISIONS:
        raise DomainError("BAD_DECISION", "审批结论必须为 approved / rejected / withdrawn")
    revision = raw.get("revision")
    if isinstance(revision, bool):
        raise DomainError("BAD_REVISION", "审批修订号 revision 需要正整数")
    try:
        revision = int(revision)
    except (TypeError, ValueError):
        raise DomainError("BAD_REVISION", "审批修订号 revision 需要正整数")
    if revision < 1:
        raise DomainError("BAD_REVISION", "审批修订号 revision 需要正整数")
    change_version = str(raw.get("change_version") or raw.get("doc_version") or "").strip()
    if not change_version:
        raise DomainError("BAD_VERSION", "缺少变更文件版本 change_version")
    try:
        decided_at = datetime.fromisoformat(str(raw.get("decided_at") or "").replace("Z", "+00:00"))
        if decided_at.tzinfo is None:
            raise ValueError()
        decided_at = decided_at.astimezone(timezone.utc)
    except (TypeError, ValueError):
        raise DomainError("INVALID_TIME", "decided_at 需要带时区偏移的时间")

    price = None
    effective = None
    if decision == "approved":
        if raw.get("price") not in (None, ""):
            price = number(raw.get("price"))
            if price < 0:
                raise DomainError("BAD_PRICE", "单价不能为负")
            price = str(price)
        if raw.get("effective") not in (None, ""):
            effective = str(raw.get("effective")).strip()
            if not valid_period(effective):
                raise DomainError("BAD_PERIOD", "生效周期 effective 格式应为 YYYY-MM")
        if endpoint == COMMERCIAL:
            if price is None:
                raise DomainError("BAD_PRICE", "商务定价通过必须给出单价 price")
            if effective is None:
                raise DomainError("BAD_PERIOD", "商务定价通过必须给出生效周期 effective")

    event = {
        "change_id": change_id,
        "endpoint": endpoint,
        "decision": decision,
        "revision": revision,
        "change_version": change_version,
        "decided_at": decided_at.isoformat(),
        "event_id": str(raw.get("event_id") or raw.get("approval_id") or "").strip(),
        "price": price,
        "effective": effective,
        "approver": str(raw.get("approver") or raw.get("department") or "").strip() or None,
        "note": str(raw.get("note") or "").strip() or None,
    }
    if not event["event_id"]:
        basis = json.dumps({k: v for k, v in event.items() if k != "event_id"},
                           sort_keys=True, ensure_ascii=False)
        event["event_id"] = "auto-" + hashlib.sha256(basis.encode()).hexdigest()[:24]
    return event


def public_event(event):
    if event is None:
        return None
    return {
        **event,
        "endpoint_label": ENDPOINT_LABELS[event["endpoint"]],
        "decision_label": DECISION_LABELS[event["decision"]],
    }


def normalize_publication(raw):
    """规范化发布记录。发布必须指向唯一生效版本，且时间带偏移。"""
    if not isinstance(raw, dict):
        raise DomainError("BAD_PUBLICATION", "发布记录需要对象")
    change_id = str(raw.get("change_id") or "").strip()
    version = str(raw.get("version") or "").strip()
    publication_id = str(raw.get("publication_id") or "").strip()
    if not change_id or not version or not publication_id:
        raise DomainError("BAD_PUBLICATION", "发布记录需要 change_id、version 与 publication_id")
    try:
        published_at = datetime.fromisoformat(str(raw.get("published_at") or "").replace("Z", "+00:00"))
        if published_at.tzinfo is None:
            raise ValueError()
        published_at = published_at.astimezone(timezone.utc)
    except (TypeError, ValueError):
        raise DomainError("INVALID_TIME", "published_at 需要带时区偏移的时间")
    return {
        "publication_id": publication_id,
        "change_id": change_id,
        "version": version,
        "published_at": published_at.isoformat(),
        "approved_by": str(raw.get("approved_by") or "").strip() or None,
    }


BUSINESS_FIELDS = ("change_id", "endpoint", "decision", "revision", "change_version", "price", "effective")


def same_approval(left, right):
    """同一次审批的业务内容是否一致。

    只比较会改变裁决的字段；decided_at、approver、note 属于回传包装信息，
    重试时允许由网络层重新打时间戳，不应因此改变状态。
    """
    return all(left.get(field) == right.get(field) for field in BUSINESS_FIELDS)


def _dedupe(events):
    by_id = {}
    for raw in events or []:
        event = normalize_event(raw)
        previous = by_id.get(event["event_id"])
        if previous is not None and not same_approval(previous, event):
            raise DomainError(
                "DUPLICATE_APPROVAL",
                "同一审批编号回传了不同内容，拒绝重复改变状态", 409,
            )
        by_id[event["event_id"]] = previous or event
    return list(by_id.values())


def _check_revisions(events):
    seen = {}
    for event in events:
        key = (event["change_id"], event["endpoint"], event["revision"])
        if key in seen and not same_approval(seen[key], event):
            label = ENDPOINT_LABELS[event["endpoint"]]
            raise DomainError(
                "CONFLICTING_REVISION",
                f"变更 {event['change_id']} 的{label}修订 {event['revision']} 已存在不同内容，修改需提升 revision",
                409,
            )
        seen[key] = event
    check_version_terms(events)


def check_version_terms(events):
    """同一 change_version 的商务条款不可变。

    已发布版本按 change_version 标识；若允许同版本改价，重新发布前的结算会
    悄悄用上新价。条款变更必须提升 change_version，重新走两端与发布。
    """
    terms = {}
    for event in events:
        if event["decision"] != "approved":
            continue
        key = (event["change_id"], event["change_version"], event["endpoint"])
        value = (event["price"], event["effective"])
        if key in terms and terms[key] != value:
            label = ENDPOINT_LABELS[event["endpoint"]]
            version = event["change_version"]
            raise DomainError(
                "VERSION_TERMS_CONFLICT",
                f"变更 {event['change_id']} 的{label}版本 {version} 条款已确定"
                f"（{terms[key][0] or '—'} / {terms[key][1] or '—'}），"
                f"改价或改生效周期需提升 change_version",
                409,
            )
        terms[key] = value


def _order(event):
    """修订号是业务先后，时间只做同修订号的稳定兜底，与到达顺序无关。"""
    return (event["revision"], event["decided_at"], event["event_id"])


def _stance(events, endpoint):
    """该端别的最新态度（跨版本）。"""
    side = [e for e in events if e["endpoint"] == endpoint]
    return max(side, key=_order) if side else None


def _version_stance(events, endpoint, version):
    """某端对某版本的最终态度：同版本取该端修订号最大的一条。"""
    side = [e for e in events if e["endpoint"] == endpoint and e["change_version"] == version]
    return max(side, key=_order) if side else None


def _withdrawal(event):
    return {
        "change_version": event["change_version"],
        "endpoint": event["endpoint"],
        "endpoint_label": ENDPOINT_LABELS[event["endpoint"]],
        "revision": event["revision"],
        "event_id": event["event_id"],
        "decided_at": event["decided_at"],
        "approver": event.get("approver"),
    }


def _empty_state(change_id):
    return {
        "change_id": change_id,
        "status": "pending",
        "status_label": STATUS_LABELS["pending"],
        "reason_code": PENDING_NO_EVENTS,
        "pending_reason": "尚无审批回传",
        "effective_version": None,
        "price": None,
        "effective": None,
        TECHNICAL: None,
        COMMERCIAL: None,
        "versions": [],
        "withdrawal": None,
        "withdrawn_versions": [],
        "history": [],
    }


def _version_candidates(events):
    """按版本归并两端最终态度，返回可定版候选与全部版本行。

    候选排序只看业务修订号：旧版本被补发/重放不会挤掉更高修订的新版。
    """
    versions = sorted({e["change_version"] for e in events})
    candidates = []
    rows = []
    for version in versions:
        technical = _version_stance(events, TECHNICAL, version)
        commercial = _version_stance(events, COMMERCIAL, version)
        row = {
            "version": version,
            TECHNICAL: public_event(technical),
            COMMERCIAL: public_event(commercial),
            "state": "pending",
            "confirmed_at": None,
            "revision": max([s["revision"] for s in (technical, commercial) if s] or [0]),
        }
        if technical and commercial and technical["decision"] == commercial["decision"] == "approved":
            row["state"] = "approved"
            row["confirmed_at"] = max(technical["decided_at"], commercial["decided_at"])
            candidates.append(row)
        elif any(s and s["decision"] == "withdrawn" for s in (technical, commercial)):
            row["state"] = "withdrawn"
        elif any(s and s["decision"] == "rejected" for s in (technical, commercial)):
            row["state"] = "rejected"
        rows.append(row)
    candidates.sort(key=lambda r: (r["revision"], r["confirmed_at"], r["version"]), reverse=True)
    return candidates, rows


def _adjudicate_change(change_id, events):
    state = _empty_state(change_id)
    state["history"] = [public_event(e) for e in sorted(events, key=lambda e: (e["decided_at"], e["revision"], e["event_id"]))]

    technical = _stance(events, TECHNICAL)
    commercial = _stance(events, COMMERCIAL)
    state[TECHNICAL] = public_event(technical)
    state[COMMERCIAL] = public_event(commercial)

    withdrawn = [e for e in events if e["decision"] == "withdrawn"]
    if withdrawn:
        state["withdrawal"] = _withdrawal(max(withdrawn, key=lambda e: (e["decided_at"], e["revision"], e["event_id"])))

    candidates, versions = _version_candidates(events)
    state["versions"] = versions
    state["withdrawn_versions"] = sorted(v["version"] for v in versions if v["state"] == "withdrawn")

    if not candidates:
        state.update(_pending_reason(technical, commercial, versions))
        return state

    # 唯一生效版本 = 两端都批准过的版本中业务修订最高者。
    # 更高版本若只到单端或已撤回，不推翻仍有效的旧版，只作为待对齐/撤回影响展示。
    chosen = candidates[0]
    tech, comm = chosen[TECHNICAL], chosen[COMMERCIAL]
    version = chosen["version"]

    if tech["price"] and comm["price"] and Decimal(tech["price"]) != Decimal(comm["price"]):
        state["pending_reason"] = (
            f"两端单价不一致：技术核定 {tech['price']} / 商务定价 {comm['price']}，待核实"
        )
        state["reason_code"] = PENDING_VERSION_MISMATCH
        return state
    if tech["effective"] and comm["effective"] and tech["effective"] != comm["effective"]:
        state["pending_reason"] = (
            f"两端生效周期不一致：技术核定 {tech['effective']} / 商务定价 {comm['effective']}"
        )
        state["reason_code"] = PENDING_VERSION_MISMATCH
        return state

    state["status"] = "effective"
    state["status_label"] = STATUS_LABELS["effective"]
    state["effective_version"] = version
    state["price"] = comm["price"] or tech["price"]
    state["effective"] = comm["effective"] or tech["effective"]
    state["reason_code"] = None
    state["pending_reason"] = None
    state["alignment"] = _alignment_note(technical, commercial, chosen, versions)
    return state


def _alignment_note(technical, commercial, chosen, versions):
    """生效版本之外的新动向：待对齐的新版、已撤回的新版，供工作台提示。"""
    if technical is None or commercial is None:
        return None
    if technical["change_version"] == commercial["change_version"] == chosen["version"]:
        return None
    for row in sorted(versions, key=lambda r: r["revision"], reverse=True):
        if row["version"] == chosen["version"] or row["revision"] <= chosen["revision"]:
            continue
        if row["state"] == "approved":
            continue
        if row["state"] == "withdrawn":
            return f"{row['version']}（修订 {row['revision']}）已撤回，{chosen['version']} 继续有效"
        sides = "、".join(ENDPOINT_LABELS[s["endpoint"]] for s in (row[TECHNICAL], row[COMMERCIAL]) if s)
        return f"{row['version']}（修订 {row['revision']}）仅见{sides}回传，待两端对齐后方可改版"
    if technical["change_version"] != commercial["change_version"]:
        return (
            f"技术核定在 {technical['change_version']}（修订 {technical['revision']}），"
            f"商务定价在 {commercial['change_version']}（修订 {commercial['revision']}），待对齐"
        )
    return None


def _pending_reason(technical, commercial, versions):
    """无任何两端共同通过的版本时，给出待定/否决/撤回的具体原因。"""
    if technical is None or commercial is None:
        missing = [ENDPOINT_LABELS[e] for e, s in ((TECHNICAL, technical), (COMMERCIAL, commercial)) if s is None]
        return {
            "reason_code": PENDING_HALF,
            "pending_reason": "缺少" + "、".join(missing) + "回传，单端先到的定价不得入本期",
        }
    if technical["change_version"] != commercial["change_version"]:
        return {
            "reason_code": PENDING_VERSION_MISMATCH,
            "pending_reason": (
                f"两端版本不一致：技术核定 {technical['change_version']} / "
                f"商务定价 {commercial['change_version']}，待对齐后统一定版"
            ),
        }
    version = technical["change_version"]
    denied = [s for s in (technical, commercial) if s["decision"] in {"withdrawn", "rejected"}]
    if any(s["decision"] == "withdrawn" for s in denied):
        who = next(s for s in denied if s["decision"] == "withdrawn")
        return {
            "status": "withdrawn",
            "status_label": STATUS_LABELS["withdrawn"],
            "reason_code": WITHDRAWN,
            "pending_reason": f"{ENDPOINT_LABELS[who['endpoint']]}撤回 {version}（修订 {who['revision']}）",
        }
    if denied:
        who = denied[0]
        return {
            "reason_code": REJECTED,
            "pending_reason": f"{ENDPOINT_LABELS[who['endpoint']]}否决 {version}（修订 {who['revision']}）",
        }
    # 两端同版本同结论但未通过（理论上已被上面的分支覆盖）。
    return {
        "reason_code": PENDING_VERSION_MISMATCH,
        "pending_reason": f"{version} 尚未获得两端一致通过",
    }


def adjudicate(events):
    """返回 {change_id: state}；同一变更至多一个生效版本。"""
    deduped = _dedupe(events)
    _check_revisions(deduped)
    grouped = {}
    for event in deduped:
        grouped.setdefault(event["change_id"], []).append(event)
    return {change_id: _adjudicate_change(change_id, evs) for change_id, evs in grouped.items()}


def publication_map(publications):
    if not publications:
        return {}
    if isinstance(publications, dict):
        return {cid: dict(pub) for cid, pub in publications.items() if pub}
    mapping = {}
    for pub in publications:
        if pub and pub.get("change_id"):
            mapping[pub["change_id"]] = dict(pub)
    return mapping


def _version_terms(events, version):
    """取某版本两端最终态度均为通过的商务条款；用于“旧版仍在用”的追溯定价。

    若该版本任一端最终态度已变为撤回/否决，则不再视为可用的历史条款。
    """
    technical = _version_stance(events, TECHNICAL, version)
    commercial = _version_stance(events, COMMERCIAL, version)
    if not technical or not commercial:
        return None
    if technical["decision"] != "approved" or commercial["decision"] != "approved":
        return None
    return {"version": version, "price": commercial["price"] or technical["price"],
            "effective": commercial["effective"] or technical["effective"]}


def _settlement(state, publication, events):
    """本期是否可用，以及实际适用的是哪个已发布版本。

    发布是原子动作：新版本发布失败时发布指针停在旧版。旧版若仍具备两端历史
    批准且未被撤回/否决，则继续作为本期适用版本（carryover），同时工作台明确
    标注“新版已生效未发布”。旧版被撤回/否决，或不存在两端批准证据时不可用。
    """
    if not publication:
        if state["status"] == "effective":
            return {"usable": False, "status": "unpublished", "code": UNPUBLISHED,
                    "version": None, "price": None, "effective": None,
                    "reason": f"唯一生效版本 {state['effective_version']} 尚未发布，不得入本期"}
        return {"usable": False, "status": state["status"],
                "code": state.get("reason_code") or PENDING_HALF,
                "version": None, "price": None, "effective": None,
                "reason": state.get("pending_reason") or "尚未形成唯一生效版本"}

    version = publication.get("version")
    if version in state["withdrawn_versions"]:
        republish = (f"当前唯一生效版本为 {state['effective_version']}，需重新发布后入本期"
                     if state["status"] == "effective" and state["effective_version"] else "")
        return {"usable": False, "status": "withdrawn", "code": WITHDRAWN,
                "version": version, "price": None, "effective": None,
                "reason": f"已发布版本 {version} 已撤回，保留追溯并停止入本期" + (f"；{republish}" if republish else "")}

    terms = _version_terms(events, version)
    if terms is None:
        return {"usable": False, "status": "stale", "code": STALE_PUBLICATION,
                "version": version, "price": None, "effective": None,
                "reason": f"已发布版本 {version} 缺少两端批准证据，仅保留追溯"}

    if state["status"] == "effective" and state["effective_version"] == version:
        return {"usable": True, "status": "published", "code": None,
                "version": version, "price": terms["price"], "effective": terms["effective"],
                "reason": None}

    current = state["effective_version"] or (
        state.get("pending_reason") if state["status"] == "pending" else "无")
    return {
        "usable": True, "status": "carryover", "code": STALE_PUBLICATION,
        "version": version, "price": terms["price"], "effective": terms["effective"],
        "reason": f"新版 {current} 尚未发布，本期沿用已发布旧版 {version}，待发布后切换",
    }


def apply_publications(states, publications, events=None):
    """把发布指针并入裁决结果，得出本期适用版本。

    发布指针只追加记录：发布失败不写新指针，旧指针保持不变，读取即回到旧版。
    事件列表缺省时从状态的历史中还原，保证纯函数调用也能追溯旧版条款。
    """
    pubs = publication_map(publications)
    merged = {cid: dict(state) for cid, state in states.items()}
    for change_id in pubs:
        merged.setdefault(change_id, _empty_state(change_id))
    for change_id, state in merged.items():
        publication = pubs.get(change_id)
        state["publication"] = publication
        scope = events if events is not None else state.get("history", [])
        state["settlement"] = _settlement(state, publication, scope)
        state["publication_revoked"] = bool(publication and publication.get("version") in state["withdrawn_versions"])
        state["applicable"] = {
            "version": state["settlement"]["version"],
            "price": state["settlement"]["price"],
            "effective": state["settlement"]["effective"],
            "mode": state["settlement"]["status"],
        }
    return merged


def attach_impact(states, rows):
    """按当前批次来源行统计每个变更的即时影响。"""
    by_change = {}
    for row in rows or []:
        change_id = row.get("change_id")
        if change_id:
            by_change.setdefault(change_id, []).append(row)
    for change_id, state in states.items():
        referenced = by_change.get(change_id, [])
        total = Decimal(0)
        unpriced = 0
        for row in referenced:
            try:
                total += number(row.get("reported"))
            except DomainError:
                unpriced += 1
        state["impact"] = {
            "rows": len(referenced),
            "reported": money(total),
            "unpriced_rows": unpriced,
            "settled": bool(state.get("settlement", {}).get("usable")),
        }
    return states


def workspace(events, publications=None, rows=None):
    """工作台视图：裁决、发布与影响一次给出。"""
    states = apply_publications(adjudicate(events), publications or {}, events)
    if rows is not None:
        attach_impact(states, rows)
    items = [states[cid] for cid in sorted(states)]
    summary = {
        "pending": sum(1 for s in items if s["status"] == "pending"),
        "effective": sum(1 for s in items if s["status"] == "effective"),
        "withdrawn": sum(1 for s in items if s["status"] == "withdrawn"),
        "usable": sum(1 for s in items if s.get("settlement", {}).get("usable")),
        "carryover": sum(1 for s in items if s.get("settlement", {}).get("status") == "carryover"),
        "changes": len(items),
    }
    return {"changes": items, "summary": summary}


def _seed(change_id, version, price, effective):
    stamp = "2026-01-01T00:00:00+00:00"
    return [
        {"change_id": change_id, "endpoint": TECHNICAL, "decision": "approved",
         "revision": 1, "change_version": version, "decided_at": stamp,
         "event_id": f"seed-{change_id}-technical", "approver": "技术部"},
        {"change_id": change_id, "endpoint": COMMERCIAL, "decision": "approved",
         "revision": 1, "change_version": version, "decided_at": stamp,
         "event_id": f"seed-{change_id}-commercial", "price": price,
         "effective": effective, "approver": "商务部"},
    ]


# 仓内基础台账：C0/C1 两端已通过并已发布，与本项目历来的已批变更集合一致。
# 该台账在首次使用数据库时写入，后续只追加，不覆盖。
DEFAULT_APPROVALS = _seed("C0", "C0", "100.00", "2026-01") + _seed("C1", "C1", "120.00", "2026-11")
DEFAULT_PUBLICATIONS = {
    "C0": {"change_id": "C0", "version": "C0", "publication_id": "seed-C0-publication",
           "published_at": "2026-01-01T00:00:00+00:00", "approved_by": "商务部"},
    "C1": {"change_id": "C1", "version": "C1", "publication_id": "seed-C1-publication",
           "published_at": "2026-01-01T00:00:00+00:00", "approved_by": "商务部"},
}
