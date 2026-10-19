# 飞书插件 · 待加入区

对照 dsh-im（xmanrui/dsh-im）尚未移植的功能。按优先级排序。

## 待加入

### 1. `/watch` — 关注一个会话（最高优先）
- **命令**：`/watch <session ID>`、`/unwatch`、`/watchlist`
- **作用**：关注某会话，任务完成后把结果主动推送到当前飞书聊天（同话题回复）。
- **为什么**：Hermes 飞书端目前是一问一答，没有主动推送。`/watch` 让飞书变成任务看板——发起后走开，完成了机器人在群里 @ 你。配合 cron / 后台任务体验质变。
- **实现要点**（dsh-im `bridge.mjs` `#runWatch` + `state-store.mjs`）：
  - 关注持久化，重启不丢
  - 每聊天最多 N 个（`MAX_WATCHES_PER_KEY`）
  - 边界 = `watchStartedAt`（墙钟）+ `lastSeq`（序列下界），只投递关注之后的完成事件，不重放历史
  - 记 `replyToMessageId`，完成推送作同话题回复
  - 补偿机制：新建/未结算的关注主动补一次投递
- **依赖**：Hermes 侧需要"会话完成事件"钩子 + 飞书主动推送（replyTo 线程回复）

### 2. 群消息权限 — 群聊响应模式
- **命令**：`/permission`、`/permissionlist`
- **作用**：控制机器人在群里"只回 @ 它的"（mention，默认）还是"回所有消息"（all）。
- **为什么**：`all` 模式要读群内每条消息，飞书把 `im:message.group_msg` 标为敏感租户权限，默认不给。需要增量授予 + 模式切换。
- **实现要点**（dsh-im `group-response-mode.mjs` + `group-message-permission-manager.mjs`）：
  - 两模式：`mention` / `all`
  - `GroupMessagePermissionManager` 为已有应用增量授予 `im:message.group_msg`，走验证 URL 授权
  - 固定 manifest，只加这一个 scope，不夹带无关能力
- **依赖**：飞书应用需有该敏感权限的授权路径；Hermes 侧群消息过滤逻辑要按模式分流

### 3. `/repair` — 补全飞书权限与卡片回调（仅私聊）
- **命令**：`/repair`
- **作用**：应用缺权限/回调时增量补齐，不新建应用。
- **为什么**：卡片按钮失灵（缺 `card.action.trigger`）、媒体下载失败（缺 `im:message:readonly`）、图片上传失败（缺 `im:message:resource`）、群消息读不到、原生 `/` 面板出不来——这些都能靠 `/repair` 命令式修复，不用去开放平台手动改。
- **实现要点**（dsh-im `repair-manager.mjs`）：
  - 固定 manifest，只加需要的 scope + 回调
  - 生成验证 URL（二维码/浏览器）授权
  - `assertTargetedAppToken` 严格校验 URL：只认目标账号域名、单例 SDK 参数、无占位符
  - 增量更新，不触发 create-only 流程
- **依赖**：飞书应用管理 API 权限；Hermes 侧需暴露"当前应用缺哪些能力"的诊断

## 已移植（参考）
- 核心流式体验（思考过程 / 工具调用 / 正文按执行顺序流式输出，step 卡 replyTo 线程回复）
- #1 飞书原生 `/` 命令面板（COMMAND_REGISTRY 动态读，排除内部探针）
- #6 功能菜单卡（会话下拉 + 模型下拉 + 按钮响应，scope / 渲染截断已修）
