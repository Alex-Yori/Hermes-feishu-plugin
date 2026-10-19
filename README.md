# Hermes Feishu Plugin

Hermes Agent 飞书平台插件 — 流式卡片、任务过程（step card）、思维链通道。

## 功能

- **CardKit 流式卡片**：回答实时流式更新，支持折叠/展开
- **任务过程（step card）**：工具调用 + 思考过程累积在独立蓝卡中，回答完毕封定折叠
- **思维链通道**：原生思维链事件渲染
- **线程回复**：step 卡以 `replyTo` 形式发成用户消息的线程回复
- **原生命令面板**：在飞书输入框打 `/` 直接选命令，命令清单随 `COMMAND_REGISTRY` 自动同步
- **助手中心卡片**：`/menu` 弹出一张卡片，可切换会话（跨端续接桌面端未聊完的对话）和模型

## 会话与模型下拉

`/menu` 卡片的两个下拉读的是你自己的数据，不是预置清单：

- **会话**取自会话数据库（`query_session_listing`，与 `/sessions` 同一 helper），因此包含桌面端会话；选中后走 `/resume` 把当前飞书聊天绑定过去
- **模型**取自会话记录里**实际用过的模型**（按最近使用排序，当前模型置顶），不是 API 的全量目录；本地 GGUF 路径会被排除。想加一个还没用过的模型，在平台配置里钉住它：

```yaml
platforms:
  feishu:
    menu_models: ["cn:step-5-preview"]   # 钉住的模型额外列出，不占短名单名额
```

跨端会话受 Hermes 既有的 slash 权限门控保护，需要在平台配置里显式把自己列为 admin：

```yaml
platforms:
  feishu:
    allow_admin_from: ["ou_你的open_id"]   # DM scope
    group_allow_admin_from: ["ou_xxx"]    # 群 scope（admin 名单不跨 scope）
```

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
| `slash_panel.py` | 飞书原生 `/` 命令面板（从 `COMMAND_REGISTRY` 动态同步） |
| `menu_card.py` | `/menu` 助手中心卡片：会话 / 模型下拉、任务控制 |
| `plugin.yaml` | 插件元数据 |
| `BACKLOG.md` | 尚未移植的 dsh-im 功能（待加入区） |

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
.venv/bin/python -m pytest tests/gateway/test_feishu_menu_card.py tests/gateway/test_feishu_slash_panel.py -q
# 119 passed
```
