# 深度登记 工程进度累计申报与变更追溯

规模：中型。使用者：总包项目计量审核员。核心规则：累计申报必须减前期已审金额，变更按审批生效版本覆盖；负进度需关联冲回证据。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 申报采信 | 查看并运行申报采信 | `/api/claims` | `app/modules/m1_claims/rules.py` | `tests/test_m1.py` |
| M2 清单映射 | 查看并运行清单映射 | `/api/billitems` | `app/modules/m2_billitems/rules.py` | `tests/test_m2.py` |
| M3 变更定版 | 查看并运行变更定版；两端口径回传、撤回、发布 | `/changes`、`/api/changes`、`/api/changes/approvals`、`/api/changes/withdraw`、`/api/changes/publish` | `app/changes.py`、`app/change_store.py`、`app/modules/m3_changes/rules.py` | `tests/test_m3.py`、`tests/test_changes.py` |
| M4 累计还原 | 查看并运行累计还原 | `/api/increment` | `app/modules/m4_increment/rules.py` | `tests/test_m4.py` |
| M5 扣回核算 | 查看并运行扣回核算 | `/api/deductions` | `app/modules/m5_deductions/rules.py` | `tests/test_m5.py` |
| M6 进度对账 | 查看并运行进度对账 | `/api/valuation` | `app/modules/m6_valuation/rules.py` | `tests/test_m6.py` |
| M7 签证归档 | 查看并运行签证归档 | `/api/certificates` | `app/modules/m7_certificates/rules.py` | `tests/test_m7.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | 旧编码 | 默认解码失败 | 回退GB18030 | `app/modules/m1_claims/rules.py` | `tests/test_m1.py::test_legacy_encoding_real_sample` |
| TRAP-02 | BOM | 首列名多字符 | utf-8-sig | `app/modules/m1_claims/rules.py` | `tests/test_m1.py::test_bom_real_sample` |
| TRAP-03 | 带引号换行 | 拆成两条 | csv逻辑行 | `app/modules/m1_claims/rules.py` | `tests/test_m1.py::test_quoted_newline_is_one_record` |
| TRAP-05 | 列数不匹配 | 静默错列 | 隔离原行 | `app/modules/m1_claims/rules.py` | `tests/test_m1.py::test_bad_columns_real_sample` |
| TRAP-08 | 前导零ID | 编码变形 | 全程字符串 | `app/modules/m1_claims/rules.py` | `tests/test_m1.py::test_identifier_preserved_real_sample` |
| TRAP-14 | 重复键冲突 | 后写覆盖 | 保留首条并隔离冲突行 | `app/modules/m1_claims/rules.py` | `tests/test_m1.py::test_duplicate_conflict_real_sample` |
| TRAP-18 | 空态三态 | 一律成功 | 分别错误码 | `app/modules/m1_claims/rules.py` | `tests/test_m1.py::test_three_empty_states` |
| TRAP-26 | 旧版本提交 | 覆盖新结果 | 版本条件更新 | `app/store.py` | `tests/test_e2e.py::test_stale_review_409` |
| TRAP-06 | 全角累计量 | 转换失败 | Decimal归一 | `app/values.py` | `tests/test_m2.py::test_fullwidth_quantity` |
| TRAP-07 | 累计量空值 | 按零冲回 | 隔离缺失 | `app/values.py` | `tests/test_m2.py::test_real_null_not_zero` |
| TRAP-10 | 分币舍入 | 浮点误差 | 分项量化后净额守恒 | `app/modules/m5_deductions/rules.py` | `tests/test_m5.py::test_fraction_rounds_to_cent` |
| TRAP-23 | 变更生效边界 | 旧价覆盖 | 按周期选择已批版本 | `app/modules/m3_changes/rules.py` | `tests/test_m3.py::test_effective_boundary` |
| TRAP-27 | 回传乱序 | 先到先入账 | 按 revision 与版本裁决，与到达顺序无关 | `app/changes.py` | `tests/test_changes.py::test_arrival_order_does_not_change_verdict` |
| TRAP-28 | 单端先到 | 先到的商务单价直接结算 | 单端一律待定，两端口径一致才定版 | `app/changes.py` | `tests/test_changes.py::test_single_end_pending_never_settles` |
| TRAP-29 | 审批重试 | 重复提交改状态 | event_id 幂等，内容冲突 409 | `app/change_store.py` | `tests/test_changes.py::test_duplicate_approval_retry_is_idempotent` |
| TRAP-30 | 撤回抹痕 | 删除已批版本 | 只追加撤回事件，保留全轨迹 | `app/change_store.py` | `tests/test_changes.py::test_withdrawal_keeps_trace_and_blocks_settlement` |
| TRAP-31 | 发布半途失败 | 新旧版本同时有效 | 单事务追加，失败保留旧指针 | `app/change_store.py` | `tests/test_changes.py::test_publish_failure_keeps_old_version` |

## 变更定版口径

技术核定与商务定价独立回传，每条回传带 `change_id`、`endpoint`、`revision`、`change_version`、`decided_at` 与幂等键 `event_id`。裁决只看各端修订号与该版本两端最终态度，与报文到达顺序无关：两端最终均为“通过”的版本才成为候选，取其中业务修订号最高者为唯一生效版本；旧版被补发或重放不会挤掉更高修订的新版。更高版本只到单端、被否决或被撤回时，不推翻仍有效的旧版，只在工作台列为待对齐或撤回影响。两端都没有可定版版本时落到待定/已撤回，先到的单价一律不入本期。事件表、撤回表只追加，撤回后仍可追溯完整轨迹。

生效不等于可付：只有发布指针指向当前唯一生效版本时才可入本期。发布在一个事务里追加记录，校验失败或写入异常都不改指针，旧版继续适用（工作台标注 carryover）；已发布版本被撤回则立即停止入本期。同一 `event_id` 重试返回 duplicate，内容不同返回 409；同一端别同一 revision 内容不同同样 409，改审批必须提升 revision。

## 对账规则

输入原始逻辑行=最终采纳来源行+隔离来源行+重复行+空白行+分段表头。派生记录用_sources保存完整来源，因此分组不丢行。业务预期合计与上游申报合计之差等于逐行差异之和。结果按输入摘要、规则版本及参数产生唯一批次，事务写入；同源重跑只复用已有批次。

## 已知不覆盖

4MB以上文件需预先按业务键分批；不模拟生产支付与外部回调。人工复核只变更批次状态，不改写原始记录。审批回传由外部部门系统推送，本项目不提供登录、签名校验与推送重放窗口；撤回需人工在 `/api/changes/withdraw` 录入。
