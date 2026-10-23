# At Tool · 精简版

给 AstrBot 的模型回复提供 QQ 群聊 @ 功能。支持 OneBot v11，已在 AstrBot 4.29.1 + NapCat 环境验证。

## 功能

- 将模型回复中的 `[at:QQ号]` 转为真正的 @。
- 提供 `get_group_members(keyword)` 工具，按昵称、群名片、QQ号查找成员。
- 将 `[at:all]` 转为 @全体；仅群主、群管理员或 AstrBot 管理员可以要求执行。机器人账号自身仍需具备 QQ 的发送权限。
- 权限查询失败时拒绝 @全体，并降级成普通文本；直接出现的 `At(qq="all")` 组件也会检查。

不需要填写配置。普通文本、原有空格及图片等组件保持原样，At 后的分隔由 AstrBot 的 OneBot 适配器处理。

## 缓存修复

系统提示只包含固定的 @ 使用规则。当前操作者的权限结果通过 `extra_user_content_parts` 放在本轮用户消息末尾，并使用 `mark_as_temp()` 避免写入历史。群成员换人或权限变化不会再由本插件改写系统提示前缀。

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

测试使用真实 AstrBot 的请求、消息组件及 OneBot 序列化代码，平台接口用模拟对象隔离，不发送 QQ 消息、不请求模型。

## 来源与许可证

原作者：**糯米茨（nuomicici）及原仓库贡献者**。

本项目 fork 自 [nuomicici/astrbot_plugin_AtTool](https://github.com/nuomicici/astrbot_plugin_AtTool)，基于上游 v2.3.2（`dbf318d08530c48c6f13514344cd41ee241fccc5`）。由 camera-2018 于 2026-10-23 精简维护：移除可编辑提示词、权限校验开关、空格重写和零宽字符注入，并修复动态系统提示影响缓存的问题。

完整源码可从[本仓库](https://github.com/camera-2018/astrbot_plugin_AtTool)获取；继续使用 [GNU AGPL v3](LICENSE)。保留上游 Git 历史和许可证。
