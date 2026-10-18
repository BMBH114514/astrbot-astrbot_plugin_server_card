# 服务器状态卡

把主机状态画成一张图，底下带一句 AI 点评。

**作者：FULL**　·　Bilibili：[@白木不华](https://space.bilibili.com/)　·　MIT 开源

完全开源，随便改随便分发，不用找我报备。

---

## 一、指令

| 指令 | 别名 | 作用 | 一般耗时 |
| --- | --- | --- | --- |
| `/状态卡` | `/服务器状态卡` `/出状态卡` | 直接出一张卡 | 有代码风格时 0.2s 上下；走样式 JSON + AI 点评约 3~8s |
| `/状态卡主题 <描述>` | `/生成卡片风格` `/卡片风格` | 让模型单独生成一套样式 JSON | 10~20s |
| `/状态卡重做风格 [要求]` | `/重做卡片风格` `/随机风格` `/换个风格` | 随机抽一个视觉方向，让模型重写代码画一张 | 第一版就过约 10s，两版都重试最多 20s 出头 |
| `/状态卡自由生成 [要求]` | `/自由生成` `/代码生成卡片` `/状态卡代码` | 按你写的要求，让模型直接写代码画一张 | 同上 |

几个说明：

- `/状态卡主题` 生成的是 **JSON 样式**，只能调整已有板块的配色、字号、顺序。
- `/状态卡重做风格` 和 `/状态卡自由生成` 生成的是 **绘图代码**，排版、背景、构图全都能改，不受字段限制。
- 后两个指令跑通的代码会自动存成 `styles/code_<月日>_<时分秒>.py`。
- 生成完想固定下来，就去配置里把「代码风格文件」填上那个文件名（或者填 `random` 随机抽）。

## 二、配置

### 点评

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `show_comment` | 开 | 是否显示底部点评栏 |
| `comment_mode` | `rules` | 点评从哪来：`rules` 走本地规则（不花钱）、`llm` 叫模型现写、`fixed` 固定一句、`off` 不显示 |
| `comment_persona_id` | 空 | 选一个 AstrBot 已有人格，用它的系统提示词来写点评 |
| `persona_name` | 空 | 点评署名，留空就用 `persona.py` 里的人名 |
| `fixed_comment` | 空 | `fixed` 模式下显示的那句话 |
| `context_mode` | `off` | `llm` 模式下是否先读聊天记录：`off` / `livingmemory` / `astrbot` |
| `context_messages` | 12 | 读多少条上下文 |

### 风格

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `style_name` | `default` | 手填 `styles/` 下的文件名，不用带 `.json`。面板提示里会列出现有的 |
| `style_from_persona` | 关 | 打开后，首次出图会让选中的人格照自己的性格生成一套专属风格 |
| `style_ai_enabled` | 开 | 是否允许用 `/状态卡主题` 生成风格 |
| `style_auto_backup` | 开 | 每次出图把生效中的风格备份到 `styles/_backup/`，同一天只存一份 |
| `style_backup_keep` | 10 | 备份保留几份，超了删最旧的 |
| `style_versions_keep` | 8 | `styles/_versions/` 里保留几个历史版本 |

### 代码风格

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `code_file` | 空 | 空：走样式 JSON<br>`code_1018_1355`：固定用这份代码<br>`random`：所有 `code_*.py` 里随机抽，每次出图都不一样<br>`code_a,code_b`：在这几份里随机抽 |

这个优先级高于 `style_name`——填了 `code_file` 就完全走代码，JSON 不再参与。

### 显示开关

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `show_config` | 开 | 硬件配置栏 |
| `show_disk` | 开 | 磁盘占用栏 |
| `show_mood` | 开 | 心情值 / 摸鱼值 / 赛博寿命 |

### 其他

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `hw_cache_hours` | 12 | 硬件信息（CPU 型号、内存品牌这些）缓存多久，免得每次开 wmic 浪费时间 |

## 三、风格是怎么工作的

有三种，越往下越自由：

**1. 样式 JSON**（`styles/*.json`）
限定在现有六个板块里调：`header` `kpi` `chart` `config` `disk` `comment`。能改顺序、间距、字号、配色、背景光斑。自带 `default` `midnight` `neon` `forest` `cyber` 五套。

**2. 随机方向**（`/状态卡重做风格`）
内置 20 个视觉方向：极简黑白海报、赛博朋克霓虹、复古报纸、蒸汽波、科幻 HUD、工业暗红、日式侘寂、像素复古、玻璃拟态、杂志封面、工程蓝图、故障艺术、粗野主义、数据可视化海报、终端窗口、暗黑哥特、糖果色、水彩纸质、军事简报、太空站。每次随机抽一个，让模型按这个方向重写代码。

**3. 自由代码**（`/状态卡自由生成`）
你自己描述，想要什么给什么。

代码执行的限制：`import` 只放行 PIL / math / time / random / colorsys / itertools / datetime；禁 `while True`；禁 `open` `eval` `exec` 和双下划线属性；builtins 换白名单，拿不到 `os` `sys` `socket`。挡得住手滑，别拿它跑陌生来源的代码。

## 四、日志

每次调用都会在 AstrBot 运行日志里留一行，格式统一带作者名：

```
[astrbot_plugin_server_card] [INFO] [main.py:561]: [状态卡·FULL] 某某(1234567890) 调用了 /状态卡
[astrbot_plugin_server_card] [INFO] [main.py:566]: [状态卡·FULL] 用代码风格 code_xxx.py 画完
[astrbot_plugin_server_card] [INFO] [main.py:568]: [状态卡·FULL] 出图 0.24s -> .../card_xxx.png
```

生成类的指令会多打方向、耗时：

```
[状态卡·FULL] 小明(123456) 调用了 /状态卡重做风格 · 抽到方向：故障艺术，错位色带，撕裂文字
[状态卡·FULL] 第 1 版跑通 · 采集 0.7s · 模型 5.0s · 总 10.2s
```

跑挂的时候是 WARN，会写清第几版、卡了多久、错在哪：

```
[状态卡·FULL] 第 1 版没跑通（已用 6.3s）: 不许 import os
```

## 五、引用与致谢

| 依赖 | 用途 | 协议 |
| --- | --- | --- |
| [Pillow](https://python-pillow.org/) | 画图、混色、圆角、文字渲染 | MIT-CMU |
| [psutil](https://github.com/giampaolo/psutil) | 采集 CPU、内存、磁盘、进程 | BSD-3 |
| [NumPy](https://numpy.org/) | 柱状图渐变的光栅运算 | BSD-3 |

字体走系统安装的，插件不打包字体文件：

- 标题：标小智无界黑 `LogoSCUnboundedSans.otf`
- 正文：鸿蒙 Sans SC `HarmonyOS_Sans_SC_Regular.ttf`，找不到退回微软雅黑 `msyh.ttc`
- 粗体：鸿蒙 Sans SC Bold，找不到退回 `msyhbd.ttc`

字体缺失只会退回默认字体，不影响出图。

## 六、目录结构

```
astrbot_plugin_server_card/
├── main.py              指令入口、风格生成、配置解析、日志
├── card.py              数据采集 collect() 和渲染 render()
├── freedraw.py          自由代码的安全执行（AST 检查 + builtins 白名单）
├── theme.py             配色、字号、版式，默认值都在这
├── persona.py           本地点评规则
├── _conf_schema.json    配置面板定义
├── styles/              样式 JSON 和生成的 code_*.py
│   ├── _backup/         自动备份
│   └── _versions/       历史版本
└── out/                 出图缓存目录
```

出图时的采样顺序：`collect()` 拿数据 → 有 `code_file` 就跑代码，没有就 `render()` 画 JSON 样式 → 存 `out/` → 发群 → 删文件。

## 七、常见问题

**改了风格没变化？**
看配置里 `code_file` 是不是填了东西，填了就走代码，`style_name` 不生效。

**重做风格一直失败？**
模型写出来的代码过不了检查或跑不通。插件会自动把报错回喂给模型再写一版，两版都不行就放弃，日志里有具体原因。多半是它想 `import os` 或者画出了画布边界。

**出图很慢？**
只有生成新风格才慢，要等模型写代码。日常群友要图（`code_file` 填 `random`）是直接跑已有代码，0.2 秒。

**AI 点评没了？**
`comment_mode` 是不是 `off`，`show_comment` 是不是关了，或者模型那条挂了——挂了不影响出图，日志里有 WARN。

**能商用吗？**
MIT，随便用。

## 八、更新日志

见 [CHANGELOG.md](CHANGELOG.md)。
