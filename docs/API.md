# 接口

模块GET使用仓内样例，模块POST可传csv或rows与context，直接计算当前规则。/api/runs使用完整流水线并原子保存。每个阶段返回rows、quarantine、metrics；后续阶段还返回累计隔离。/api/upload接收multipart文件file。/api/runs/<id>/archive只导出一份带清单与证据的CSV。

## 变更定版

审批回传、撤回与发布都是追加写入，不覆盖历史。裁决结果由全量证据重算，接口返回的 `state` 即工作台口径：`status` 为 `pending | effective | withdrawn`，`settlement` 说明本期是否可用及实际适用的已发布版本，`applicable` 给出本期单价与生效周期。

`POST /api/changes/approvals` 接收一条部门回传：

```json
{"change_id":"C2","endpoint":"commercial","decision":"approved","revision":1,
 "change_version":"C2","price":"150.00","effective":"2026-11",
 "decided_at":"2026-11-20T10:00:00+08:00","event_id":"approval-20261120-01"}
```

- `endpoint`：`technical`（技术核定）或 `commercial`（商务定价），也接受中文别名。
- `decision`：`approved | rejected | withdrawn`，也接受“通过/否决/撤回”。
- `revision`：该端业务修订号，正整数；修改审批必须提升 revision。
- `change_version`：变更文件版本，两端须一致才可能定版。
- `price`/`effective`：商务定价通过时必填；技术核定可省略。
- `event_id`：幂等键，可省略（按业务内容派生）。相同键相同内容返回 `outcome=duplicate`，不改状态；相同键不同内容返回 409 `DUPLICATE_APPROVAL`。

`GET /api/changes/approvals` 返回 `changes`（裁决与结算）、`summary`、`events`（全部回传）、`publications`（发布窗口）、`withdrawals`、`failures`（发布失败尝试）。

`POST /api/changes/withdraw` 参数同上并去掉 `price`/`effective`，追加撤回事件并单列撤回记录；撤回后 `status=withdrawn`，已发布版本立即停止入本期，历史回传全部保留。

`POST /api/changes/publish`：

```json
{"change_id":"C2","version":"C2"}
```

仅当 `version` 等于当前唯一生效版本时成功。发布失败（无生效版本、版本过期、写入异常）返回 409 且不改发布指针，旧版继续作为本期适用版本；失败原因写入 `failures`，可用 `GET /api/changes/approvals` 审计。同版本重复发布返回 `outcome=duplicate`。

`GET /api/changes` 是单阶段预览，按仓内样例或提交的 rows 计算；M3 的 rows 会附带 `change_status`、`settlement_status`、`effective_version`。
