"""变更审批与发布的可追溯存储。

四张表都只追加，不更新、不删除：

* change_approvals  —— 审批回传事件，event_id 唯一。同一审批重试命中幂等键，
  不会二次改变状态；若同键内容不同则整体拒绝（409）。
* change_publications —— 发布记录，只有发布成功才落一条；发布失败不写入，
  因此数据库里保留的上一条即旧版，读取时自然回退到旧版。
* change_withdrawals —— 撤回记录，独立于审批事件，保证撤回后仍可追溯。
* change_publication_failures —— 发布失败尝试，仅记录原因，不构成发布。

裁决结果永远由全量事件重算（见 app.changes），不存“当前状态”字段，避免
状态与证据不一致。
"""
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from app.changes import (
    DEFAULT_APPROVALS, DEFAULT_PUBLICATIONS, FAILED_PUBLICATION, STALE_VERSION,
    check_version_terms, normalize_event, normalize_publication, same_approval,
)
from app.errors import DomainError

SCHEMA = """
CREATE TABLE IF NOT EXISTS change_approvals (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    change_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS change_publications (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id TEXT NOT NULL UNIQUE,
    change_id TEXT NOT NULL,
    version TEXT NOT NULL,
    payload TEXT NOT NULL,
    published_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS change_withdrawals (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    withdrawal_id TEXT NOT NULL UNIQUE,
    change_id TEXT NOT NULL,
    version TEXT NOT NULL,
    payload TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS change_publication_failures (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL UNIQUE,
    change_id TEXT NOT NULL,
    version TEXT NOT NULL,
    code TEXT NOT NULL,
    reason TEXT NOT NULL,
    attempted_at TEXT NOT NULL
);
"""


def _now():
    return datetime.now(timezone.utc).isoformat()


def connect(path=None):
    """默认沿用 app.store 的数据目录，测试替换 store.DATA_DIR 时一并隔离。"""
    from app import store as run_store
    directory = path or run_store.DATA_DIR
    from pathlib import Path
    Path(directory).mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(Path(directory) / "records.db", timeout=20)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    return db


def _read_approvals(db):
    return [json.loads(p) for _, p in db.execute("SELECT event_id,payload FROM change_approvals ORDER BY seq")]


def _read_publications(db):
    latest = {}
    for _, change_id, payload in db.execute(
            "SELECT publication_id,change_id,payload FROM change_publications ORDER BY seq"):
        latest[change_id] = json.loads(payload)
    return latest


def events():
    with connect() as db:
        return _read_approvals(db)


def publications():
    with connect() as db:
        return _read_publications(db)


def exposures():
    """发布窗口：每条成功发布都可审计，按写入顺序返回。"""
    with connect() as db:
        return [
            {"publication_id": pid, "change_id": cid, "version": v, "payload": json.loads(p)}
            for pid, cid, v, p in db.execute(
                "SELECT publication_id,change_id,version,payload FROM change_publications ORDER BY seq")
        ]


def _ensure_seed(db):
    """首次使用时写入仓内基础台账。event_id 唯一，重复调用不会重复写入。"""
    if db.execute("SELECT 1 FROM change_approvals LIMIT 1").fetchone():
        return
    stamp = _now()
    for raw in DEFAULT_APPROVALS:
        event = normalize_event(raw)
        db.execute(
            "INSERT OR IGNORE INTO change_approvals(event_id,change_id,payload,received_at) VALUES (?,?,?,?)",
            (event["event_id"], event["change_id"], json.dumps(event, ensure_ascii=False), stamp),
        )
    for change_id, pub in DEFAULT_PUBLICATIONS.items():
        record = normalize_publication(pub)
        db.execute(
            "INSERT OR IGNORE INTO change_publications(publication_id,change_id,version,payload,published_at) VALUES (?,?,?,?,?)",
            (record["publication_id"], change_id, record["version"], json.dumps(record, ensure_ascii=False), record["published_at"]),
        )


def _insert_event(db, event):
    """幂等写入审批事件。

    先查幂等键：同键同内容返回 duplicate，同键异内容 409。再查同端别同修订号，
    内容不同即 409。最后依赖 UNIQUE 约束兜底并发重试，避免“先查后写”竞态。
    """
    existing = db.execute(
        "SELECT payload FROM change_approvals WHERE event_id=?", (event["event_id"],)
    ).fetchone()
    if existing:
        if not same_approval(json.loads(existing[0]), event):
            raise DomainError("DUPLICATE_APPROVAL",
                              "同一审批编号回传了不同内容，拒绝重复改变状态", 409)
        return "duplicate", json.loads(existing[0])
    rows = db.execute(
        "SELECT payload FROM change_approvals WHERE change_id=? AND event_id<>?",
        (event["change_id"], event["event_id"]),
    ).fetchall()
    for (payload,) in rows:
        other = json.loads(payload)
        if other["endpoint"] == event["endpoint"] and other["revision"] == event["revision"]:
            if same_approval(other, event):
                # 同一审批被网络层重发（换了包装时间戳但业务内容相同）：按幂等处理。
                return "duplicate", other
            raise DomainError(
                "CONFLICTING_REVISION",
                "同一审批端别的修订号已存在不同内容，修改审批需提升 revision", 409,
            )
    try:
        db.execute(
            "INSERT INTO change_approvals(event_id,change_id,payload,received_at) VALUES (?,?,?,?)",
            (event["event_id"], event["change_id"], json.dumps(event, ensure_ascii=False), _now()),
        )
        # 写入后按全量证据复核版本条款，冲突则整体回滚，避免半截证据入库。
        check_version_terms(_read_approvals(db))
    except sqlite3.IntegrityError:
        # 并发重试：另一个请求刚写入同一 event_id，按已存在处理。
        stored = db.execute(
            "SELECT payload FROM change_approvals WHERE event_id=?", (event["event_id"],)
        ).fetchone()
        if stored and same_approval(json.loads(stored[0]), event):
            return "duplicate", json.loads(stored[0])
        raise DomainError("DUPLICATE_APPROVAL",
                          "同一审批编号回传了不同内容，拒绝重复改变状态", 409)
    return "recorded", event


def record_approval(raw):
    """写入一条审批回传。

    返回 {"outcome": "recorded"|"duplicate", "event": ...}。
    duplicate 表示同一 event_id 的完全相同重试，未改变任何状态。
    """
    event = normalize_event(raw)
    with connect() as db:
        _ensure_seed(db)
        outcome, stored = _insert_event(db, event)
    return {"outcome": outcome, "event": stored}


def withdraw(raw):
    """记录一次撤回，并把撤回同时作为审批事件追加，保证裁决只读事件即可。"""
    event_raw = {**raw, "decision": "withdrawn"}
    event_raw.pop("price", None)
    event_raw.pop("effective", None)
    event = normalize_event(event_raw)
    with connect() as db:
        _ensure_seed(db)
        outcome, stored = _insert_event(db, event)
        record = {
            "withdrawal_id": event["event_id"],
            "change_id": event["change_id"],
            "version": event["change_version"],
            "endpoint": event["endpoint"],
            "revision": event["revision"],
            "decided_at": event["decided_at"],
            "approver": event.get("approver"),
        }
        db.execute(
            "INSERT OR IGNORE INTO change_withdrawals(withdrawal_id,change_id,version,payload,recorded_at) VALUES (?,?,?,?,?)",
            (record["withdrawal_id"], record["change_id"], record["version"],
             json.dumps(record, ensure_ascii=False), _now()),
        )
    return {"outcome": outcome, "event": stored, "withdrawal": record}


def withdraw_records():
    with connect() as db:
        return [
            {"withdrawal_id": wid, "change_id": cid, "version": v, "payload": json.loads(p)}
            for wid, cid, v, p in db.execute(
                "SELECT withdrawal_id,change_id,version,payload FROM change_withdrawals ORDER BY seq")
        ]


def _current_publication(db, change_id):
    row = db.execute(
        "SELECT payload FROM change_publications WHERE change_id=? AND payload IS NOT NULL ORDER BY seq DESC LIMIT 1",
        (change_id,),
    ).fetchone()
    return json.loads(row[0]) if row else None


def publish(raw):
    """发布唯一生效版本。

    先由审批证据重算裁决，确认目标版本确为唯一生效版本，再在一个事务里追加
    发布记录。任何失败（校验不通过、写入异常）都不改发布表，旧发布指针保持
    不变，读取即继续返回旧版。
    """
    if not isinstance(raw, dict):
        raise DomainError("BAD_PUBLICATION", "发布请求需要对象")
    change_id = str(raw.get("change_id") or "").strip()
    version = str(raw.get("version") or "").strip()
    if not change_id or not version:
        raise DomainError("BAD_PUBLICATION", "发布需要 change_id 与 version")
    from app import changes as engine
    evidence_events = events()
    states = engine.apply_publications(engine.adjudicate(evidence_events), publications(), evidence_events)
    state = states.get(change_id)
    if state is None or state["status"] != "effective":
        detail = state["pending_reason"] if state else "审批证据中不存在该变更"
        return _publish_failure(change_id, version, "CHANGE_NOT_EFFECTIVE", detail or "无唯一生效版本")
    if version != state["effective_version"]:
        return _publish_failure(
            change_id, version, STALE_VERSION,
            f"唯一生效版本为 {state['effective_version']}，拒绝发布 {version}",
        )
    record = normalize_publication({
        "change_id": change_id,
        "version": version,
        "publication_id": raw.get("publication_id") or str(uuid.uuid4()),
        "published_at": raw.get("published_at") or _now(),
        "approved_by": raw.get("approved_by"),
    })
    try:
        outcome, stored = _insert_publication(record)
    except (sqlite3.Error, OSError) as error:
        # 写入失败：旧发布指针不动，旧版继续生效；失败尝试另账可审计。
        return _publish_failure(change_id, version, FAILED_PUBLICATION, f"发布写入失败：{error}")
    return {"ok": True, "outcome": outcome, "publication": stored}


def _insert_publication(record):
    """单事务追加发布记录；已存在同版本时按幂等返回，不重复改写。"""
    with connect() as db:
        _ensure_seed(db)
        current = _current_publication(db, record["change_id"])
        if current and current["version"] == record["version"]:
            return "duplicate", current
        db.execute(
            "INSERT INTO change_publications(publication_id,change_id,version,payload,published_at) VALUES (?,?,?,?,?)",
            (record["publication_id"], record["change_id"], record["version"],
             json.dumps(record, ensure_ascii=False), record["published_at"]),
        )
    return "published", record


def _publish_failure(change_id, version, code, reason):
    """失败尝试不进入发布表，只追加失败审计记录。"""
    attempt = {
        "publication_id": "failed-" + uuid.uuid4().hex,
        "change_id": change_id,
        "version": version,
        "status": "failed",
        "code": code,
        "reason": reason,
        "attempted_at": _now(),
    }
    try:
        with connect() as db:
            db.execute(
                "INSERT INTO change_publication_failures(attempt_id,change_id,version,code,reason,attempted_at) "
                "VALUES (?,?,?,?,?,?)",
                (attempt["publication_id"], change_id, version, code, reason, attempt["attempted_at"]),
            )
    except sqlite3.Error:
        pass
    return {"ok": False, "outcome": "failed", "code": code, "reason": reason, "attempt": attempt}


def snapshot():
    """一次性读取全部证据，供工作台与流水线使用。"""
    with connect() as db:
        _ensure_seed(db)
        return {
            "events": _read_approvals(db),
            "publications": _read_publications(db),
            "exposures": [
                {"publication_id": pid, "change_id": cid, "version": v, "payload": json.loads(p) if p else None}
                for pid, cid, v, p in db.execute(
                    "SELECT publication_id,change_id,version,payload FROM change_publications ORDER BY seq")
            ],
            "withdrawals": [
                {"withdrawal_id": wid, "change_id": cid, "version": v, "payload": json.loads(p)}
                for wid, cid, v, p in db.execute(
                    "SELECT withdrawal_id,change_id,version,payload FROM change_withdrawals ORDER BY seq")
            ],
            "failures": [
                {"attempt_id": aid, "change_id": cid, "version": v, "code": code,
                 "reason": reason, "attempted_at": at}
                for aid, cid, v, code, reason, at in db.execute(
                    "SELECT attempt_id,change_id,version,code,reason,attempted_at "
                    "FROM change_publication_failures ORDER BY seq")
            ],
        }
