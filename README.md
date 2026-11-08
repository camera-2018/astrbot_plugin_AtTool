# At Tool · 精简版

让 AstrBot 在群聊回复里真正 @ 到人。支持 OneBot v11（NapCat 等）和 QQ 官方机器人，要求 AstrBot 4.29.4 或更高版本。

## 功能

- OneBot：模型在回复中输出 `[at:QQ号]`，插件转换成真正的 @。
- QQ 官方机器人：提示模型调用内置 `send_message_to_user`，使用 `mention_user` 组件和 `mention_user_id=member_openid`，正文放在同一次发送中。发送成功后不再重复输出正文；旧版标签转换仍兼容已有回复。
- 提供 `get_group_members(keyword)` 工具，按昵称、群名片或成员 ID 查找成员。OneBot 可查全员；官方平台返回本账号、本群实际见过的成员，并明确标记为不完整名单。
- 官方群友互相 @ 时，将旧式 `<@OPENID>`、`<@!OPENID>`、新版 `<qqbot-at-user id="OPENID" />` 及 `mentions` 元数据恢复成原生 `At` 组件；引用消息里的 @ 不会被当成本轮点名。
- 官方消息带 @ 和图片、语音、视频或文件时，先发带 @ 的 Markdown 消息，再按原顺序发媒体，避免 @ 标签出现在图片说明里却不触发提及。普通回复和主动发送均支持。
- @全体：OneBot 使用 `[at:all]`，允许群主、群管理员或 AstrBot 管理员发起；官方平台使用内置发送工具的 `mention_user_id=all`，只允许 AstrBot 管理员发起，因为无法沿用 OneBot 的群角色查询。
- 权限查询失败时拒绝 @全体，并降级成普通文本；直接出现的 `At(qq="all")` 组件也会检查。

不需要填写配置。QQ 官方群聊的 @ 用户会使用 Markdown，即使这条消息原本设置为纯文本；普通消息沿用原来的发送设置。QQ 服务端仍会检查机器人的群管理权限；[官方文档](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/trans/text-chain.html)目前规定官方群 @全体需要机器人为管理员，每群每天限一次。

官方成员名单只保存 ID 和昵称，不保存聊天正文；按账号和群隔离，只在内存中保留最近 7 天见过的成员，重载或重启后会重新积累。最多保留 128 个群、每群 2048 人，工具单次最多返回 200 人。查不到时模型应说明原因，不能把普通 QQ 号当作 OpenID。

官方适配器的兼容处理随插件加载启用，卸载时恢复原方法，不修改 AstrBot 核心文件。它补齐了 4.29.4 的入站 @ 解析和图文混发问题；后续升级 AstrBot 时建议重新运行下面的回归测试。

## 缓存修复

OneBot 和 QQ 官方机器人各使用一段固定的 @ 规则，分别引导标签转换和内置工具发送。当前操作者的权限结果通过 `extra_user_content_parts` 放在本轮用户消息末尾，并使用 `mark_as_temp()` 避免写入历史。群成员换人或权限变化不会再由本插件改写系统提示前缀。官方工具直接发送时也会经过 @全体权限检查。

这只消除本插件的动态前缀干扰，不保证模型服务的缓存命中率；其他插件、上下文压缩和模型侧策略仍可能影响缓存。

## 安装

在 AstrBot 插件页选择「从链接安装」，填入：

```text
https://github.com/camera-2018/astrbot_plugin_AtTool
```

替换原插件时先卸载旧版，避免重复注册相同工具。插件名称和 `get_group_members` 工具名称沿用上游，不能同时安装两个版本。

## 开发验证

在已安装 AstrBot 的 Python 环境中运行：

```bash
python -m unittest discover -s tests -v
```

测试使用真实 AstrBot 的请求、消息组件、官方消息解析和发送方法，以及 OneBot 序列化代码。只模拟 QQ 的发送、上传接口，不发送真实消息、不请求模型。覆盖群友互相 @、机器人点名、引用、多图、主动发送、权限、缓存前缀和插件卸载恢复。

## 来源与许可证

原作者：**糯米茨（nuomicici）及原仓库贡献者**。

本项目 fork 自 [nuomicici/astrbot_plugin_AtTool](https://github.com/nuomicici/astrbot_plugin_AtTool)，基于上游 v2.3.2（`dbf318d08530c48c6f13514344cd41ee241fccc5`）。由 camera-2018 于 2026-10-23 精简维护：移除可编辑提示词、权限校验开关、空格重写和零宽字符注入，并修复动态系统提示影响缓存的问题。

完整源码可从[本仓库](https://github.com/camera-2018/astrbot_plugin_AtTool)获取；继续使用 [GNU AGPL v3](LICENSE)。保留上游 Git 历史和许可证。
