import sqlite3

import pytest

from app import change_store, changes, store
from app.errors import DomainError
from app.pipeline import calculate, load_sample
from app.modules.m3_changes.rules import evaluate as m3


def approval(change_id="C2", endpoint="commercial", revision=1, version="C2",
             decision="approved", at="2026-11-20T02:00:00+08:00", **extra):
    event = {
        "change_id": change_id, "endpoint": endpoint, "decision": decision,
        "revision": revision, "change_version": version, "decided_at": at,
    }
    if decision == "approved" and endpoint == "commercial":
        event.update({"price": "150.00", "effective": "2026-11"})
    event.update(extra)
    return event


def commercial(change_id="C2", revision=1, version="C2", **extra):
    return approval(change_id, "commercial", revision, version, **extra)


def technical(change_id="C2", revision=1, version="C2", **extra):
    return approval(change_id, "technical", revision, version, **extra)


def state(change_id="C2"):
    view = changes.workspace(change_store.events(), change_store.publications())
    return next(c for c in view["changes"] if c["change_id"] == change_id)


# ---------- 1. 乱序到达不改变裁决 ----------

def test_arrival_order_does_not_change_verdict():
    # 商务定价先到（时间更早），技术核定后到；与反序到达结论一致。
    first, later = commercial(), technical(at="2026-11-21T09:00:00+08:00")
    a = changes.workspace(changes.DEFAULT_APPROVALS + [first, later])
    b = changes.workspace(changes.DEFAULT_APPROVALS + [later, first])
    sa = next(c for c in a["changes"] if c["change_id"] == "C2")
    sb = next(c for c in b["changes"] if c["change_id"] == "C2")
    assert sa["status"] == sb["status"] == "effective"
    assert sa["effective_version"] == sb["effective_version"] == "C2"
    assert sa["price"] == sb["price"] == "150.00"


def test_single_end_pending_never_settles():
    view = changes.workspace(changes.DEFAULT_APPROVALS + [commercial()])
    pending = next(c for c in view["changes"] if c["change_id"] == "C2")
    assert pending["status"] == "pending"
    assert pending["reason_code"] == changes.PENDING_HALF
    assert pending["settlement"]["usable"] is False
    assert pending["applicable"]["price"] is None


def test_single_end_price_not_used_by_m3():
    workspace = changes.workspace(changes.DEFAULT_APPROVALS + [commercial()])
    result = m3([{"claim_id": "1", "change_id": "C2", "period": "2026-11"}], {"change_workspace": workspace})
    assert result["rows"] == []
    assert result["quarantine"][0]["reason"] == changes.PENDING_HALF


def test_version_mismatch_pending():
    view = changes.workspace(changes.DEFAULT_APPROVALS + [commercial(), technical(version="C2b")])
    pending = next(c for c in view["changes"] if c["change_id"] == "C2")
    assert pending["status"] == "pending"
    assert pending["reason_code"] == changes.PENDING_VERSION_MISMATCH
    assert pending["settlement"]["usable"] is False


# ---------- 2. 存储层幂等与撤回追溯 ----------

def test_duplicate_approval_retry_is_idempotent():
    event = commercial(event_id="legacy-1")
    first = change_store.record_approval(event)
    before = state()
    retry = change_store.record_approval({**event, "decided_at": "2026-11-21T10:00:00+08:00"})
    after = state()
    assert first["outcome"] == "recorded"
    assert retry["outcome"] == "duplicate"
    assert len(change_store.events()) == len(changes.DEFAULT_APPROVALS) + 1
    assert before["status"] == after["status"]
    assert [v["version"] for v in before["versions"]] == [v["version"] for v in after["versions"]]
    # 重试不产生第二个生效版本
    assert sum(1 for v in after["versions"] if v["state"] == "approved") == 0


def test_duplicate_approval_with_different_content_conflicts():
    change_store.record_approval(commercial(event_id="dup-1"))
    with pytest.raises(DomainError) as error:
        change_store.record_approval(commercial(event_id="dup-1", price="999.00"))
    assert error.value.code == "DUPLICATE_APPROVAL"
    assert error.value.status == 409


def test_same_business_content_resend_is_idempotent():
    """同一审批换了回传编号但业务内容相同：按幂等处理，不产生第二个版本。"""
    change_store.record_approval(commercial(event_id="net-1"))
    resent = change_store.record_approval(commercial(event_id="net-2",
                                                     at="2026-11-21T10:00:00+08:00"))
    assert resent["outcome"] == "duplicate"
    assert len([e for e in change_store.events() if e["change_id"] == "C2"]) == 1


def test_same_revision_different_content_conflicts():
    change_store.record_approval(commercial(event_id="rev-1"))
    with pytest.raises(DomainError) as error:
        change_store.record_approval(commercial(event_id="rev-1b", price="999.00"))
    assert error.value.code == "CONFLICTING_REVISION"


def test_same_version_price_change_requires_new_version():
    """同版本改价会绕过重新发布，必须在写入时拦住。"""
    change_store.record_approval(commercial(event_id="v-1"))
    with pytest.raises(DomainError) as error:
        change_store.record_approval(commercial(event_id="v-2", revision=2, price="130.00"))
    assert error.value.code == "VERSION_TERMS_CONFLICT"
    # 失败写入整体回滚，证据里没有半截记录
    assert [e["event_id"] for e in change_store.events()].count("v-2") == 0


def test_newer_single_end_does_not_displace_effective_version():
    """新版只有商务回传时，旧版仍是唯一生效版本，新价不得启用。"""
    view = changes.workspace(changes.DEFAULT_APPROVALS + [
        commercial(event_id="a-1"), technical(event_id="a-2"),
        commercial(event_id="a-3", revision=2, version="C2b", price="160.00",
                   at="2026-11-21T09:00:00+08:00"),
    ])
    state_c2 = next(c for c in view["changes"] if c["change_id"] == "C2")
    assert state_c2["effective_version"] == "C2"
    assert state_c2["price"] == "150.00"
    assert "C2b" in state_c2["alignment"] and "待两端对齐" in state_c2["alignment"]


def test_newer_fully_approved_version_supersedes_old():
    view = changes.workspace(changes.DEFAULT_APPROVALS + [
        commercial(event_id="b-1"), technical(event_id="b-2"),
        commercial(event_id="b-3", revision=2, version="C2b", price="160.00",
                   at="2026-11-21T09:00:00+08:00"),
        technical(event_id="b-4", revision=2, version="C2b", at="2026-11-21T10:00:00+08:00"),
    ])
    state_c2 = next(c for c in view["changes"] if c["change_id"] == "C2")
    assert state_c2["effective_version"] == "C2b"
    assert state_c2["price"] == "160.00"


def test_withdrawn_old_version_with_newer_effective_keeps_trace():
    events = changes.DEFAULT_APPROVALS + [
        commercial(event_id="c-1"), technical(event_id="c-2"),
        commercial(event_id="c-3", revision=2, version="C2b", price="160.00",
                   at="2026-11-21T09:00:00+08:00"),
        technical(event_id="c-4", revision=2, version="C2b", at="2026-11-21T10:00:00+08:00"),
        commercial(event_id="c-5", revision=3, version="C2b", decision="withdrawn",
                   at="2026-11-22T09:00:00+08:00"),
    ]
    publications = {"C2": {"change_id": "C2", "version": "C2", "publication_id": "p-c2",
                           "published_at": "2026-11-20T00:00:00+00:00"}}
    view = changes.workspace(events, publications)
    state_c2 = next(c for c in view["changes"] if c["change_id"] == "C2")
    # 新版撤回后回落到上一版，且上一版仍处于已发布状态
    assert state_c2["effective_version"] == "C2"
    assert state_c2["withdrawn_versions"] == ["C2b"]
    assert state_c2["settlement"]["usable"] is True
    assert state_c2["settlement"]["version"] == "C2"
    assert "C2b" in state_c2["alignment"]


def test_withdrawal_leaves_no_effective_version_when_older_also_withdrawn():
    events = changes.DEFAULT_APPROVALS + [
        commercial(event_id="d-1"), technical(event_id="d-2"),
        commercial(event_id="d-3", revision=2, version="C2", decision="withdrawn",
                   at="2026-11-21T09:00:00+08:00"),
    ]
    state_c2 = next(c for c in changes.workspace(events)["changes"] if c["change_id"] == "C2")
    assert state_c2["status"] == "withdrawn"
    assert state_c2["effective_version"] is None


def test_withdrawal_keeps_trace_and_blocks_settlement():
    change_store.record_approval(commercial(event_id="w-comm"))
    change_store.record_approval(technical(event_id="w-tech"))
    published = change_store.publish({"change_id": "C2", "version": "C2"})
    assert published["outcome"] == "published"
    assert state()["settlement"]["usable"] is True

    record = change_store.withdraw({
        "change_id": "C2", "endpoint": "technical", "revision": 2,
        "change_version": "C2", "decided_at": "2026-11-22T09:00:00+08:00",
        "event_id": "w-withdraw", "approver": "技术部",
    })
    assert record["outcome"] == "recorded"
    after = state()
    assert after["status"] == "withdrawn"
    assert after["settlement"]["usable"] is False
    assert after["settlement"]["code"] == changes.WITHDRAWN
    assert after["withdrawal"]["endpoint_label"] == "技术核定"
    # 撤回后全部回传轨迹仍在，审计可追溯
    assert [e["event_id"] for e in after["history"]][-1] == "w-withdraw"
    assert len(change_store.withdraw_records()) == 1
    assert after["withdrawn_versions"] == ["C2"]


def test_withdrawal_retry_is_idempotent():
    payload = {"change_id": "C2", "endpoint": "technical", "revision": 2,
               "change_version": "C2", "decided_at": "2026-11-22T09:00:00+08:00",
               "event_id": "w-retry"}
    assert change_store.withdraw(payload)["outcome"] == "recorded"
    assert change_store.withdraw(payload)["outcome"] == "duplicate"
    assert len(change_store.events()) == len(changes.DEFAULT_APPROVALS) + 1


def test_withdrawal_can_be_reapproved_at_higher_revision():
    change_store.record_approval(commercial(event_id="r-comm"))
    change_store.record_approval(technical(event_id="r-tech"))
    change_store.publish({"change_id": "C2", "version": "C2"})
    change_store.withdraw({"change_id": "C2", "endpoint": "technical", "revision": 2,
                           "change_version": "C2", "decided_at": "2026-11-22T09:00:00+08:00",
                           "event_id": "r-withdraw"})
    assert state()["status"] == "withdrawn"
    change_store.record_approval(technical(event_id="r-tech-3", revision=3,
                                           at="2026-11-23T09:00:00+08:00"))
    restored = state()
    assert restored["status"] == "effective"
    assert restored["withdrawn_versions"] == []
    # 撤回历史仍保留，只是最终态度被更高修订覆盖
    assert "r-withdraw" in [e["event_id"] for e in restored["history"]]
    assert len(change_store.withdraw_records()) == 1


# ---------- 3. 发布：原子失败保留旧版 ----------

def _publish_c2_v1():
    change_store.record_approval(commercial(event_id="p-comm"))
    change_store.record_approval(technical(event_id="p-tech"))
    assert change_store.publish({"change_id": "C2", "version": "C2"})["ok"] is True


def _approve_c2b():
    change_store.record_approval(commercial(event_id="p-comm2", revision=2,
                                            version="C2b", price="160.00",
                                            at="2026-11-21T09:00:00+08:00"))
    change_store.record_approval(technical(event_id="p-tech2", revision=2,
                                           version="C2b", at="2026-11-21T10:00:00+08:00"))


def test_publish_failure_keeps_old_version(monkeypatch):
    _publish_c2_v1()
    _approve_c2b()
    before = state()
    assert before["effective_version"] == "C2b"
    assert before["settlement"]["status"] == "carryover"
    assert before["applicable"]["price"] == "150.00"

    def boom(record):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(change_store, "_insert_publication", boom)
    result = change_store.publish({"change_id": "C2", "version": "C2b"})
    assert result["ok"] is False
    assert result["code"] == changes.FAILED_PUBLICATION

    after = state()
    # 发布失败：旧版仍是本期适用版本，新版本不得入账
    assert after["publication"]["version"] == "C2"
    assert after["settlement"]["usable"] is True
    assert after["settlement"]["status"] == "carryover"
    assert after["applicable"]["price"] == "150.00"
    # 失败尝试可审计
    assert change_store.snapshot()["failures"][-1]["code"] == changes.FAILED_PUBLICATION


def test_publish_rejects_non_effective_version():
    _publish_c2_v1()
    result = change_store.publish({"change_id": "C2", "version": "C3"})
    assert result["ok"] is False
    assert result["code"] == changes.STALE_VERSION
    assert state()["publication"]["version"] == "C2"


def test_publish_rejects_pending_change():
    change_store.record_approval(commercial(event_id="pending-only"))
    result = change_store.publish({"change_id": "C2", "version": "C2"})
    assert result["ok"] is False
    assert result["code"] == "CHANGE_NOT_EFFECTIVE"
    assert state()["publication"] is None


def test_publish_retry_is_idempotent():
    _publish_c2_v1()
    again = change_store.publish({"change_id": "C2", "version": "C2"})
    assert again["outcome"] == "duplicate"
    assert len(change_store.snapshot()["exposures"]) == 3  # C0、C1 种子 + C2


# ---------- 4. 流水线与归档 ----------

def test_pipeline_quarantines_withdrawn_change():
    change_store.record_approval(commercial(event_id="x-comm"))
    change_store.record_approval(technical(event_id="x-tech"))
    change_store.publish({"change_id": "C2", "version": "C2"})
    change_store.withdraw({"change_id": "C2", "endpoint": "commercial", "revision": 3,
                           "change_version": "C2", "decided_at": "2026-11-22T09:00:00+08:00",
                           "event_id": "x-withdraw"})
    raw = ("claim_id,item,period,cumulative_qty,previous_qty,change_id,reversal,reported,note\n"
           "00001,001,2026-11,125,100,C2,none,2000.00,新增变更\n").encode()
    result = calculate(raw)
    reasons = {q["reason"] for q in result["stages"][2]["quarantine"]}
    assert changes.WITHDRAWN in reasons
    assert result["stages"][2]["rows"] == []


def test_run_id_changes_with_evidence_and_keeps_old():
    raw = load_sample("clean.csv")
    before = calculate(raw)
    change_store.record_approval(commercial(event_id="ev-1"))
    change_store.record_approval(technical(event_id="ev-2"))
    after = calculate(raw)
    assert before["run_id"] != after["run_id"]
    assert before["manifest"]["change_evidence_sha256"] != after["manifest"]["change_evidence_sha256"]


def test_archive_exports_change_decisions(client):
    created = client.post("/api/runs", json={"sample": "dirty.csv"}).get_json()
    response = client.get("/api/runs/" + created["run_id"] + "/archive")
    body = response.data.decode("utf-8-sig")
    assert "change_decision" in body.splitlines()[0]
    assert "C1" in body and "已生效" in body


def test_approval_http_flow(client):
    payload = commercial(event_id="http-1")
    first = client.post("/api/changes/approvals", json=payload)
    assert first.status_code == 200 and first.get_json()["outcome"] == "recorded"
    retry = client.post("/api/changes/approvals", json=payload)
    assert retry.get_json()["outcome"] == "duplicate"
    assert retry.get_json()["state"]["status"] == "pending"
    client.post("/api/changes/approvals", json=technical(event_id="http-2"))
    view = client.get("/api/changes/approvals").get_json()
    state_c2 = next(c for c in view["changes"] if c["change_id"] == "C2")
    assert state_c2["status"] == "effective"
    assert state_c2["settlement"]["usable"] is False  # 尚未发布
    assert client.post("/api/changes/publish", json={"change_id": "C2", "version": "C2"}).status_code == 200
    state_c2 = next(c for c in client.get("/api/changes/approvals").get_json()["changes"]
                    if c["change_id"] == "C2")
    assert state_c2["settlement"]["usable"] is True


def test_conflicting_duplicate_over_http_is_409(client):
    payload = commercial(event_id="http-dup")
    assert client.post("/api/changes/approvals", json=payload).status_code == 200
    conflict = client.post("/api/changes/approvals", json={**payload, "price": "999.00"})
    assert conflict.status_code == 409
    assert conflict.get_json()["code"] == "DUPLICATE_APPROVAL"


def test_archive_header_carries_change_evidence_hash(client):
    created = client.post("/api/runs", json={"sample": "clean.csv"}).get_json()
    body = client.get("/api/runs/" + created["run_id"] + "/archive").data.decode("utf-8-sig")
    assert body.split("\r\n")[0].count(",") == 7  # 8 列，含 change_decision
    assert created["manifest"]["change_evidence_sha256"]


def test_publish_http_failure_returns_409(client):
    response = client.post("/api/changes/publish", json={"change_id": "DRAFT", "version": "DRAFT"})
    assert response.status_code == 409
    assert response.get_json()["ok"] is False
