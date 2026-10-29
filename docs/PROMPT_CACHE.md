# 缓存优化 fork

本 fork 基于 Him666233 的 `V1.2.3.hotfix.2`，保留原作者署名。版本为
`V1.2.3.hotfix.2-cache.2`，更新地址为
<https://github.com/Shennoter/astrbot_plugin_group_chat_plus>。

## 优化内容

前缀缓存复用的是请求开头连续相同的内容。在大段历史之前插入会变化的名字、ID 或心情，
会使变化位置之后的历史难以复用。调整后的大致顺序是：

```text
固定人格与回复规则
历史消息（保留每条消息的发送者标记）
当前消息
当前回复对象、批次提醒等本轮信息
当前心情
```

- 群聊回复的 `append` 和 `override` 两种模式，都将 `sender_emphasis` 放在
  `formatted_message` 之后；末尾的回复对象确认继续保留。
- 普通群聊与主动群聊共享最终请求钩子，心情提示追加到 `req.prompt` 末尾，
  避免心情变化改写 `req.system_prompt`。
- 主动聊天直接调用 `provider.text_chat()`，群聊和私聊路径均会转发第三方插件的
  `extra_user_content_parts`，以保留生活日程等临时状态。没有临时内容时沿用旧调用参数。
  自身心情继续由请求正文末尾承载；插件使用原始消息保存历史。
- 请求结束时清理事件上的心情标记，避免复用事件时沿用上一轮心情。
- 群聊、私聊的兼容心情注入方法也改为末尾追加。

## 安装及后续更新

在 AstrBot 插件管理中，通过仓库地址安装本 fork：

```text
https://github.com/Shennoter/astrbot_plugin_group_chat_plus
```

已有同名插件时，先备份配置和插件数据，再按当前 AstrBot 版本提供的替换、重装方式安装。
本 fork 保留插件识别名 `astrbot_plugin_group_chat_plus`，继续使用原有配置和数据目录。
安装后核对版本号，以及插件元数据中的 `repo` 是否为上面的 fork 地址。
随后通过该 fork 更新；切换回原作者仓库会恢复上游的提示词构造方式。

本次调整自动生效。`max_context_messages` 可以继续按聊天需要设置；发送历史数量与
`custom_storage_max_messages` 存储上限是不同设置，缩短存储不是本次优化的前提。

## 验证与边界

回归测试使用模拟的多用户历史和请求对象，运行实际提示词构造方法、实际最终请求钩子与重写器，
验证换人接话后公共前缀仍包含历史、心情不改变系统提示词、当前身份提醒和图片/音频参数保留。
测试不会请求模型或发送聊天消息。

```sh
python -m pytest -q tests
```

真实收益还取决于模型及中转的缓存支持、保留时间、路由稳定性，以及历史窗口是否变化。
第三方插件在系统提示词中注入动态状态，也会继续影响缓存。生活日程可配合
[缓存优化 fork](https://github.com/Shennoter/astrbot_plugin_life_scheduler)，并开启其 `inject_as_temp`。
因此不能由这些测试推算或保证真实缓存命中率。

上线后按同一群聊、同一模型比较未缓存输入量、总输入量和实际费用。
历史裁剪、会话重置、换模型及长时间空闲都可能使缓存重新建立。
