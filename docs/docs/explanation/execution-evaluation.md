---
title: 执行评测与调用统计
sidebar_label: 执行评测
sidebar_position: 6
---

# 执行评测与调用统计

V2 任务结束时保存 `execution_summary` 事件，用于比较 `skill`、`direct`、`planner` 三条执行路径。评测工具读取本地导出的事件和独立人工验收记录，区分运行时通过验收与用户目标实际完成。它不会调用模型或操作设备。

## 统计口径

| 字段 | 含义 |
| --- | --- |
| `route` | 实际选择的执行路径，自动路由解析后保存 |
| `status` / `stop_reason` | 结构化结束状态与原因；`completed` 是运行时的验收结论 |
| `elapsed_ms` | 本次执行阶段耗时，包含模型、设备、验收和清理；不包含排队时间 |
| `steps` | 执行器上报的累计步骤数 |
| `subtasks` | 已记录的不同子任务和只读检查结果数量，含前置条件与最终验收 |
| `continuations` | 已接受的 checkpoint 续跑次数 |
| `planner_calls` / `executor_calls` / `verifier_calls` | 三类模型的逻辑请求次数，失败请求也计入；不从步骤数推算，不包含 SDK 内部 HTTP 重试次数 |
| `calls_complete` | 调用统计是否覆盖本次运行；不可观测的外部执行器必须标为 `false` |
| `input_tokens` / `output_tokens` | 服务方实际返回的 token 用量；缺失信息保持 `null` |
| `usage_complete` | 所有已发起请求均有完整 token 用量，且调用统计完整 |

GLM、Qwen、General Vision 在实际请求位置记录执行器调用。Planner 在 SDK 模型调用开始时记录，验收器在独立请求位置记录。GLM/Qwen 不为收集 token 擅自增加服务方可能不支持的 streaming 参数；收到 usage-only chunk 时也会记录用量。流式重复累计值只覆盖该请求的用量，不重复相加。

一次请求失败、取消或未提供 usage，都会使本任务的对应 token 总数未知。评测聚合只在全部样本 `usage_complete=true` 时给出 token 总量；部分覆盖不能当作完整成本。调用不可观测时，聚合调用数也为 `null`。没有模型请求且调用覆盖完整的运行，用量可以是已知的 `0`。

## 导出并评估任务

后端的任务事件接口包含 `task_id`、`event_type` 和 `payload`，可以直接保存并读取：

```bash
TASK_ID='实际任务ID'
curl --fail --silent "http://127.0.0.1:8000/api/tasks/$TASK_ID/events" > /tmp/task-events.json
uv run python scripts/evaluate_execution.py /tmp/task-events.json --output /tmp/execution-report.json
```

将地址替换为当前后端地址。工具支持 `{ "events": [...] }`、JSON 数组和一行一个对象的 JSONL，只统计 `execution_summary`，忽略步骤和文字事件。支持直接在 `payload` 中放摘要，也支持 `payload.summary`。直接输入摘要时需要同时提供 `task_id`。单次运行未指定 `repetition` 时按 `1` 处理；未指定 `variant` 时使用 `route`。

下面仅展示数据格式，数字不是性能结果：

```json
{
  "task_id": "实际任务ID",
  "case_id": "open-contact-ambiguous",
  "variant": "v2",
  "repetition": 1,
  "event_type": "execution_summary",
  "payload": {
    "route": "skill",
    "status": "blocked",
    "stop_reason": "ambiguous_identity",
    "elapsed_ms": 2000,
    "steps": 2,
    "subtasks": 1,
    "continuations": 0,
    "planner_calls": 0,
    "executor_calls": 2,
    "verifier_calls": 1,
    "calls_complete": true,
    "input_tokens": null,
    "output_tokens": null,
    "usage_complete": false
  }
}
```

比较不同运行时，保留原始 `task_id`，在导出记录外层补充相同的基准 `case_id`、相应 `variant` 和 `repetition`。这些是评测集元数据，不能改写摘要中的实际统计值。同一版本、用例和重复编号出现多条记录会报错，避免重复导出影响结果。

## 独立人工验收

验收者根据预先定义的任务条件检查最终界面或业务结果，再写入独立 JSONL。不要把运行时 `status`、执行器回答或验收模型结论复制为人工标签。格式示例：

```json
{"task_id":"实际任务ID","case_id":"open-contact-ambiguous","variant":"v2","repetition":1,"source":"human","success":false,"note":"出现重名候选后停止，尚未进入确定的联系人页面"}
```

`source` 必须为 `human`，`success` 必须是布尔值。工具检查标签格式，标签是否确由独立人工给出由评测过程保证。同一标签键重复会报错；没有对应运行的标签计入 `unused_labels`。

安全停止与任务完成应分开记录。例如重名时及时停止符合操作边界，但“打开指定联系人页面”尚未完成，因此该目标的 `success=false`；可在 `note` 记录正确停止。若基准任务本身要求识别歧义并停止，则按该任务事先约定的验收条件给出标签。不要在看到结果后改变成功定义。

```bash
uv run python scripts/evaluate_execution.py /tmp/task-events.json \
  --labels /tmp/human-labels.jsonl \
  --group-by route \
  --output /tmp/execution-report.json
```

报告包含运行时完成数、人工标签覆盖率、真实完成率、误报成功率、未标注的运行时完成数，以及耗时 p50/p95 和调用、token 汇总：

- `real_success_rate` = 人工确认成功数 / 有人工标签的运行数。
- `false_success_rate` = 人工判失败且运行时为 `completed` 的数量 / 有人工标签且运行时为 `completed` 的数量。
- 没有相应人工标签时，比例为 `null`；无标签任务不会被算作真实成功，也不会被默认为失败。
- 分位数对实际样本线性插值，小样本结果不代表统计显著性。

## 同任务比较

在相同设备、应用版本、账号数据和初始页面上运行预先固定的用例集。分别记录两个实现的 `variant`，每轮还原初始条件；重复执行时增加 `repetition`。每个版本都需要独立验收。

```bash
uv run python scripts/evaluate_execution.py /tmp/comparison-runs.jsonl \
  --labels /tmp/comparison-human-labels.jsonl \
  --group-by variant \
  --baseline v1 --candidate v2 \
  --output /tmp/comparison-report.json
```

比较严格配对相同 `case_id`（无此字段时用 `task_id`）和 `repetition`，只计算配对样本，另外列出两个版本未配对的数量。没有配对、缺少人工标签、候选真实成功数下降或误报成功数增加时，`savings` 为 `null` 并说明原因。满足条件后，报告配对样本的耗时、调用数和 token 降低百分比；缺失的调用或 token 数据对应结果仍为 `null`。负值表示增加，基线为零时不计算百分比。结果只适用于该批匹配样本，不能外推为普遍性能收益。

建议基准至少覆盖：重名目标、普通弹窗、页面变化、加载缓慢、设备结果未知、用户接管、预算耗尽后的续跑，以及发送或提交后的明确结果核对。结果未知的动作不能为了补齐评测而重放。

## 合成样例与实现位置

`tests/fixtures/execution_benchmark.jsonl` 是用于测试解析和统计边界的合成记录，覆盖重名、未知结果、接管、预算续跑和简单读取。每条记录标记 `synthetic=true`，没有附带人工成功标签；这些数字不能作为真机验收、调用节省或成功率证据。报告单列 `synthetic_runs`；含合成数据的配对比较不会输出 `savings`。

```bash
uv run python scripts/evaluate_execution.py tests/fixtures/execution_benchmark.jsonl --group-by variant
uv run pytest tests/test_execution_evaluation.py -q
```

实现入口为 `AutoGLM_GUI/execution_metrics.py`（任务上下文与调用计数）、`AutoGLM_GUI/evaluation.py`（标签、聚合和配对规则）和 `scripts/evaluate_execution.py`（CLI）。测试覆盖失败请求、缺失用量、并发请求关联、流末 usage-only chunk、虚假成功、标签缺失、样本配对与合成数据边界。
