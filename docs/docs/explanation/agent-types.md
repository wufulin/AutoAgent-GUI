---
title: Agent 类型怎么选
sidebar_label: Agent 类型选型
sidebar_position: 2
---

# Agent 类型怎么选

「设置 → 视觉模型」提供三类执行器。类型应与模型的动作输出方式一致；默认是 GLM Agent。

| Agent | 配置值 | 适用模型与协议 |
| --- | --- | --- |
| GLM Agent | `glm-async` | AutoGLM-Phone / GLM 的文本动作格式 |
| Qwen Agent | `qwen` | Qwen 的视觉动作与 `<answer>` 格式 |
| General Vision Agent | `gemini` | 支持图片输入和 Function Calling 的通用视觉模型 |

`async-glm` 和 `general-vision` 旧别名继续有效；原始 `glm` 配置在读取和保存时规范化为 `glm-async`。分层规划与直接执行均使用同一类视觉执行器；分层模式另需配置决策模型。

## 旧配置迁移

MAI、DroidRun、Midscene 已移除，不再提供其代码、依赖、界面选项或专用参数。
已有配置文件仍能读取，并保留旧 `agent_type` 及模型地址、名称等字段；配置接口返回 `agent_type_supported=false`，界面提示改选。系统不会自动换成 GLM 或其他执行器。旧类型不能保存或初始化。

改选 GLM、Qwen 或 General Vision 并保存后，旧 `agent_config_params` 会清空，即使旧客户端仍发送这些参数也不会带入新类型。旧任务与历史数据不删除，旧的 Agent 名称按普通文本显示。

如需回滚，恢复上一版本代码与依赖；已经保存的新类型配置仍兼容。若要恢复旧专用参数，请从升级前自行保留的配置恢复，系统不会重建已经清空的值。

配置入口见[配置模型服务](../guide/configure-model.md)，执行方式见[两种工作模式](./modes.md)。
