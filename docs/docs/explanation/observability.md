---
title: 追踪与可观测性
sidebar_label: 追踪与可观测性
sidebar_position: 5
---

# 追踪与可观测性

AutoGLM-GUI 内置一套轻量的执行追踪（trace）系统，用来回答「这次任务到底慢在哪、错在哪」。本页解释它的设计与用法。

## 为什么需要追踪

一次任务执行跨越多个阶段：截图 → 应用检测 → 模型推理 → 解析 → 执行动作 → ADB 调用 → 等待。任何一步都可能慢或失败。如果只看最终「成功 / 失败」，根因无从查起。追踪把每一步记录成带时间、父子关系和属性的 span，让你能精确定位瓶颈。

## 默认开启

追踪默认启用。把 `AUTOGLM_TRACE_ENABLED` 设为 `0` / `false` / `no` / `off` 可关闭。span 以 JSONL 写入 `logs/trace_{date}.jsonl`，路径可用 `AUTOGLM_TRACE_FILE` 覆盖（见[环境变量](../reference/env-vars.md)）。

每次任务运行都会把自己的 `trace_id` 存入记录；`/api/tasks/*` 与 `/api/history/*` 的响应里都带这个值。要排查某次任务，先拿到它的 `trace_id`，再用它过滤 trace 文件即可。

## 追踪覆盖了什么

- **模型调用**：经典 Agent 发 `step.llm`；分层规划层的流式调用发 `model.call` 与 `layered.planner.*`。
- **子任务验收**：`subtask.verify` 覆盖新截图、只读模型调用、JSON 解析和完成条件核对；其中的 `model.call` 是子 span，可区分模型调用成功与整体验收成功。
- **工具调用**：分层规划层发 `tool.call` 和 `tool.result`；Gemini 的 Function Calling 发 `tool.call`。
- **设备 / ADB 调用**：设备封装与底层 ADB 操作发 `device.*` 与 `adb.*`。
- **记忆与持久化**：分层会话发 `memory.*`；任务/历史写入发 `task_store.*` / `history.*`。
- **任务汇总**：任务结束追加一条 `trace_summary` 事件，并据同一份 trace 数据记录 Prometheus 时延指标。

## 怎么用来排障

1. 在后端正常运行下复现问题。
2. 在 `/api/tasks/{task_id}` 或 `/api/history/{serialno}/{record_id}` 找到这次任务，复制它的 `trace_id`。
3. 用该 `trace_id` 过滤 `logs/trace_{date}.jsonl`，查看各 span 的名称、父子关系、耗时和 `attrs`。
4. 历史详情里也有按阶段拆分的步骤耗时（截图、应用检测、LLM、解析、动作执行、ADB、sleep 等），可快速看出瓶颈。
5. 任务跑完后，`/api/metrics` 提供聚合的 Prometheus 直方图。

## 子任务验收失败

分层任务在执行器申请完成后，取得新截图并用当前视觉模型只读验收。验收输出预算固定为 `max_tokens=4096`，包含模型推理和最终 JSON；总超时为 130 秒、请求超时为 120 秒，SDK 自动重试关闭。增加预算不保证模型一定输出完整或正确的结果；失败后停止当前任务，不自动重试验收或重放设备操作。

排查时先看子任务的 `stop_reason` / `verification.error`，再按 `trace_id` 查 `subtask.verify` 和它的 `model.call` 子 span：

| 错误分类                                                    | 含义与排查线索                                                                                              |
| ----------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `verification_output_truncated`                             | `finish_reason=length`。输出触及预算，即使 JSON 可以解析也不能通过；结合输出与推理 token 用量判断预算消耗。 |
| `verification_response_incomplete`                          | 模型以其他非 `stop` 原因结束；响应不具备完整性保证。                                                        |
| `verification_response_empty`                               | 没有可解析的响应正文。                                                                                      |
| `verification_json_invalid` / `verification_schema_invalid` | 正文不是合法 JSON，或未满足验收结果结构；查看脱敏校验路径。                                                 |
| `verification_criteria_mismatch`                            | 返回条件与输入条件的内容、数量或顺序不一致；查看不匹配索引。                                                |
| `verification_request_failed` / `verification_timeout`      | 请求失败或超时；查看失败阶段和异常类型，不记录服务方错误原文。                                              |
| `verification_screenshot_unavailable`                       | 无可用的新截图，不能继续验收。                                                                              |
| `verification_unavailable`                                  | 尚未归类的验收异常；结合阶段和异常类型定位。                                                                |

验收结果的可选 `diagnostics` 只保存固定字段的元数据：阶段、输出预算、结束原因、token 计数、响应长度与 SHA-256、请求 ID 的 SHA-256、异常类型、脱敏校验路径和条件不匹配索引。这些摘要仅用于关联和对比，不能作为原始响应或可回看的图片；诊断不保存原始模型响应、思维链、验收截图、API Key 或服务方错误原文。模型没有提供的统计保持缺省，不能按零用量解释。旧历史没有该字段仍可读取，无需数据库迁移。

预算调整前的同类复现曾出现 `finish_reason=length`：1500 个输出 token 中，1229 个用于推理，最终 JSON 被截断。该证据说明截断可以导致验收失败；首次故障的原始响应未留存，不能据此断言其唯一根因，也不能用复现次数推断稳定失败率。

更新或回滚前停止任务并确认设备空闲。`diagnostics` 不参与恢复，运行中的 checkpoint 不迁移；后续提交重新观察并开始新任务。

## 可回放追踪

`AUTOGLM_TRACE_REPLAY_ENABLED`（默认开启）会记录足以「回放」一次执行的数据（含截图等），便于事后复盘和复现，无需重新连接真机。

> 相关接口见 [REST API](../reference/rest-api.md) 的 `/api/metrics`、`/api/tasks/{task_id}/events`。
