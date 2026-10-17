# 服务器状态卡

一张图看懂主机状态，底部带一句 AI 点评。

## 指令

```
/服务器状态卡
/状态卡
/出状态卡
```

## 配置

### 点评

| 项 | 说明 |
| --- | --- |
| `comment_mode` | `rules` 走 `persona.py` 本地规则（不花钱）、`llm` 叫模型现写、`fixed` 固定一句、`off` 不显示点评栏 |
| `persona_name` | 署名，**可以留空**，留空就用 `persona.py` 里的人名；选 `llm` 时也用来告诉模型在扮演谁 |
| `fixed_comment` | 固定点评内容，**可以留空**，留空自动退回规则点评 |

### 上下文

只在 `comment_mode = llm` 时生效，让点评先读聊天再说话。

| 项 | 说明 |
| --- | --- |
| `context_mode` | `off` 不读；`livingmemory` 读 `astrbot_plugin_livingmemory` 的记忆上下文；`astrbot` 读 AstrBot 自己的会话历史 |
| `context_messages` | 读多少条，1~60，默认 12 |

选 `livingmemory` 时会去拿它的 `event_handler.conversation_manager.get_context()`，
拿不到（没装、没初始化完）会自动退回无上下文点评，不会报错。

### 板块开关

`show_comment` / `show_mood` / `show_config` / `show_disk`，想精简就关掉对应栏。

## 改画风

编辑 `theme.py`，配色、光斑、圆角、字体路径都在里面。

## 改口吻

编辑 `persona.py` 的 `comment()`，用 `d` 里的数据拼自己的话。

## 依赖

```
pip install psutil pillow numpy
```

Windows 专用：硬件信息走 PowerShell 的 `Win32_*`，磁盘写死 C: / D:。
