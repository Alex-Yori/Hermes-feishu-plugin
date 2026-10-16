# Hermes Feishu Plugin

Hermes Agent 飞书平台插件 — 流式卡片、任务过程（step card）、思维链通道。

## 功能

- **CardKit 流式卡片**：回答实时流式更新，支持折叠/展开
- **任务过程（step card）**：工具调用 + 思考过程累积在独立蓝卡中，回答完毕封定折叠
- **思维链通道**：原生思维链事件渲染
- **线程回复**：step 卡以 `replyTo` 形式发成用户消息的线程回复

## 文件结构

| 文件 | 说明 |
|------|------|
| `adapter.py` | 飞书适配器主入口，混入所有 mixin |
| `streaming.py` | CardKit 2.0 流式卡片 native transport |
| `step_card.py` | 任务过程纯渲染 + 切分核心 |
| `step_card_lane.py` | 任务过程 adapter mixin（turn 生命周期） |
| `thinking_process.py` | 思维链通道 mixin |
| `feishu_comment.py` | 飞书评论处理 |
| `feishu_comment_rules.py` | 评论规则 |
| `feishu_meeting_invite.py` | 会议邀请 |
| `feishu_admission_diagnostics.py` | 准入诊断 |
| `plugin.yaml` | 插件元数据 |

## 安装

将本目录复制到 Hermes Agent 的 `plugins/platforms/feishu/` 下，重启网关即可。

## 配置

在 `~/.hermes/config.yaml` 中：

```yaml
platforms:
  feishu:
    step_cards_enabled: true      # 启用任务过程卡
    thinking_process: false       # 思维链通道（与 step card 二选一）
streaming:
  enabled: true                   # 启用流式卡片
display:
  interim_assistant_messages: false  # 关闭中间消息外漏
```

## 测试

```bash
cd hermes-agent
.venv/bin/python -m pytest tests/gateway/test_feishu_step_card.py tests/gateway/test_feishu_streaming_cards.py -q
# 56 passed
```
