---
id: layered-agent-analysis
title: Layered Agent 架构分析
---

# Layered Agent 实现与验收契约

## 职责与数据流

`layered_agent_service.py` 组织 Planner、SDK 会话和工具事件；`subtasks.py` 管理当前 task 的子任务、预算、checkpoint 与视觉验收。`PhoneAgentManager` 统一物理设备所有权，GLM、Qwen、Gemini 执行器共享异步设备协议。

用户任务进入 TaskManager 后，Planner 用 `list_devices` 发现设备，用 `execute_subtask` 下达可验证子目标。工具的任务、会话与绑定设备来自隐藏的 run context，模型不能通过参数切换到其他任务上下文。手机执行器继续使用逐步截图、生成动作、执行、再观察的短循环。

## 内部工具契约

```python
execute_subtask(
    device_id: str,
    goal: str,
    success_criteria: list[str],
    constraints: list[str],
    max_steps: int = 10,
    checkpoint: str | None = None,
)
```

`goal` 与每项 `success_criteria` 必须非空；`max_steps` 取 1–20。`default_max_steps` 是整个 task 的累计执行预算，`None` 表示无总步数上限；`layered_max_turns` 保留规划轮数限制。完成条件应描述可观察界面结果，约束明确禁止动作或歧义处理，不应仅重复「完成任务」。

返回包保留 `result / steps / success` 兼容字段，`result` 现在是结构化对象，包含 `subtask_id / goal / status / message / stop_reason / steps / total_steps / success_criteria / verification / checkpoint`。`steps` 是本段步数，`total_steps` 是该子任务执行器累计步数；服务另行统计整个 task 的总预算。`success` 仅在 `status=completed` 时为真。前端与历史仍能显示旧字符串 result 和旧 Agent 标签。

| 状态 | 处理 |
| --- | --- |
| `completed` | 新截图逐项验收通过，可规划下一子任务 |
| `partial` | 仅本段预算耗尽或验收明确未满足；携 checkpoint 续跑 |
| `blocked` | 停滞、验收不可用、配置变化、无效请求、设备忙或总预算耗尽；结束本 task |
| `failed` | 模型、解析或执行错误；结束本 task |
| `cancelled` | 用户取消；不执行后续动作 |
| `needs_user` | 需要用户处理歧义、确认或接管；结束本 task |
| `unknown` | 已发动作结果未确定；停止并保留设备隔离 |

原 `chat(device_id, message)` 保留兼容包装；Planner 的新主路径使用带完成条件的 `execute_subtask`。公共 `/api/layered-agent/*` 和 task API 路由保持兼容。独立 MCP 同步 `chat` 的契约不因分层工具升级而更改。

## 完成验收与续跑

执行器终态通过流事件传递，不再用 `run()` 的自然语言或 `"Max steps reached"` 字符串判断成功。设备或模型错误、解析失败、watchdog、接管与取消均保留其真实状态；`finish` 只是内部的完成申请。

完成申请后，在同一设备占用期间重新截图，用子任务启动时的视觉 `ModelConfig` 发起独立、不带动作工具的视觉请求。验收结构为：

```json
{
  "criteria": [
    {"criterion": "页头显示张三", "verdict": "met", "reason": "页头可见姓名张三"}
  ],
  "facts": ["当前页面是与张三的聊天页"],
  "observed_at": "2026-10-21T00:00:00+00:00",
  "screenshot_sha256": "截图内容的 SHA-256",
  "error": null
}
```

条件数量、文本与顺序必须匹配请求，判定取 `met / unmet / unknown`。无有效截图、格式错误或调用失败返回 `blocked`，验收不可用标记为 `verification_unavailable`；任一 `unknown` 返回不可判定状态。所有条件 `met` 才能 completed。截图保留于本次内存调用，不进入工具结果和历史；只保存上述证据摘要，不新增图片读取端点。

partial 的 checkpoint 是服务生成的一次性随机句柄，绑定 task、session、设备、目标、条件、约束和执行器。续跑消耗旧句柄，保留局部上下文，注入验收差异或继续观察提示；再次 partial 生成新句柄。同一 task 只允许顺序执行，存在未解决 partial 时不得新建另一子任务。任务结束、取消、配置变化或服务重启后不能续跑。

整体成功要求至少一个子任务，且所有已发起子任务的最新状态均 completed，没有取消或其他终止状态。该聚合防止错误被 Planner 文字掩盖；它证明已声明的子目标完成，仍依赖 Planner 正确覆盖用户原始目标，不等同于独立的整体业务验收。

## 生命周期与取消

执行器 context 使用 task 作用域，Planner 对话会话与手机执行器局部记忆分开。设备租约在首次执行时取得，贯穿执行、验收与 partial 等待；续跑复用同一 owner，任务结束时释放并销毁任务执行器。不同 context 的直接执行、分层任务及人工输入共享物理设备占用边界。

取消针对所属 task 的执行器，不任意中止同设备其他 context。异步设备适配层保护已发出的本地 ADB 与远端线程调用，等待在途动作结算后再释放；超时或结果未知保留进程内隔离，需要操作者核对设备状态后显式恢复。取消不表示已发动作被撤销，断连或未知动作不得自动重放。

## 配置、验证与回滚

本版只保留 GLM、Qwen、Gemini 执行器及其兼容别名，旧 MAI、DroidRun、Midscene 配置可读但不可执行或保存；切换类型清空旧专用参数。历史不删除。设备存在活动租约、执行或人工操作时，配置保存返回 409 且不修改文件；保存期间拒绝新设备获取，避免配置写入与执行器清理竞态。无需数据库结构迁移，新增状态和验收摘要随任务事件与历史消息保存。

确定性测试应覆盖：错误不报成功、finish 后新截图验收、无效验收失败关闭、预算耗尽续跑、checkpoint 绑定与一次消费、同设备跨 context 互斥、取消等待在途动作、未知结果隔离，以及新旧历史兼容。测试通过不代表真机任务成功率、token 成本或耗时已改善，真实收益需同批任务对照验证。

升级与回滚前停止正在执行的任务，核对设备没有未结束动作，再成套更新前后端。回滚使进程内 checkpoint 失效；无需恢复旧任务执行状态，也不能重放未知动作。旧类型若需恢复执行，必须恢复对应旧版本代码与依赖；已清空的专用参数需从升级前保留的配置恢复。
