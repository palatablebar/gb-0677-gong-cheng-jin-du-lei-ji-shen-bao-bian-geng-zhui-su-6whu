import csv,io,json


def build_archive(run):
    stream=io.StringIO(newline="");writer=csv.writer(stream)
    writer.writerow(["run_id","input_sha256","rule_version","kind","source_lines","reason","evidence","change_decision"])
    meta=run["manifest"]
    workspace=run.get("changes") or {}
    decisions={state["change_id"]:state for state in workspace.get("changes",[])}
    def decision(row):
        state=decisions.get(row.get("change_id"))
        if state is None:return ""
        settlement=state.get("settlement") or {}
        return json.dumps({
            "change_id":state["change_id"],
            "status":state["status"],
            "status_label":state["status_label"],
            "effective_version":state.get("effective_version"),
            "applied_version":settlement.get("version"),
            "settlement":settlement.get("status"),
            "usable":settlement.get("usable"),
            "reason":settlement.get("reason") or state.get("pending_reason"),
            "withdrawal":state.get("withdrawal"),
            "history":state.get("history",[]),
        },ensure_ascii=False)
    for row in run["stages"][-1]["rows"]:
        writer.writerow([run["run_id"],meta["input_sha256"],meta["rule_version"],"accepted",json.dumps(row.get("_sources",[])),row.get("status",""),json.dumps(row,ensure_ascii=False),decision(row)])
    for row in run["stages"][-1].get("cumulative_quarantine",[]):
        writer.writerow([run["run_id"],meta["input_sha256"],meta["rule_version"],"quarantine",json.dumps(row.get("_sources",[])),row.get("reason",""),json.dumps(row,ensure_ascii=False),decision(row)])
    for state in workspace.get("changes",[]):
        writer.writerow([run["run_id"],meta["input_sha256"],meta["rule_version"],"change",json.dumps([]),state.get("reason_code") or state["status"],json.dumps(state,ensure_ascii=False),decision({"change_id":state["change_id"]})])
    return "﻿"+stream.getvalue()
