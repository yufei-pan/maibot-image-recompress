# 智能入站图片重压缩（maibot-image-recompress）

把入站消息中的图片重压缩为更高效的 **WebP**（也可配置 JPEG / PNG），替代主程序内置的 JPEG 压缩。支持动图转动态 WebP、仅压缩超大图、压到目标大小等多种模式。

> **⚠️ 使用前必读**
>
> **务必**在 `bot_config.toml` 关闭麦麦默认的「处理过大图片」（`visual.handle_oversized_images`），否则同一张图片可能被**连续压缩三次**，画质严重劣化：
>
> ```toml
> [visual]
> handle_oversized_images = false
> ```
>
> | 顺序 | 环节 | 说明 |
> |---|---|---|
> | 1 | 主程序内置压缩 | 在本插件 Hook **之前**执行，硬编码 JPEG |
> | 2 | 本插件 | `chat.receive.before_process` 中重编码为 WebP 等 |
> | 3 | VLM 识图链路 | 送 API 前可能再次压图（如转 JPEG、缩尺寸） |
>
> 本插件用于**替代**内置压缩，而不是与内置压缩叠加。插件加载时若检测到该选项仍为开启，会输出警告日志。

## 工作原理

主程序的内置图片压缩硬编码 JPEG，且在消息 Hook 之前执行：

```
适配器消息入站
  └─ 内置压缩 visual.handle_oversized_images（JPEG，先于 Hook）
       └─ Hook: chat.receive.before_process   ← 本插件在这里接管
            └─ 消息正式进入处理链（存储、识图、回复……）
```

本插件订阅 `chat.receive.before_process`（BLOCKING / EARLY），把消息里的图片组件解码后用 Pillow 重新编码为目标格式，再写回消息。

若不关闭 `visual.handle_oversized_images`，超大图会先被主程序压成 JPEG，再经本插件重压缩，后续识图时还可能第三次压图——详见文首警告。

## 安装

1. **先在** `bot_config.toml` **关闭** `visual.handle_oversized_images`（见文首警告）；
2. 将本仓库放入（或软链到）MaiBot 的 `plugins/` 目录；
3. 依赖 `pillow>=10.0.0` 由 Host 依赖解析器自动安装；
4. 重启 MaiBot，日志中出现 `智能入站图片重压缩已加载` 即成功。

## 配置说明

### [plugin]

| 字段 | 默认值 | 说明 |
|---|---|---|
| `enabled` | `true` | 是否启用插件 |
| `config_version` | `"1.0.0"` | 配置版本 |

### [trigger] 触发条件

| 字段 | 默认值 | 说明 |
|---|---|---|
| `mode` | `"always"` | `always`=所有入站图片都重压缩；`oversized_only`=仅压缩超过阈值的图片 |
| `size_threshold_mb` | `1.0` | 大小阈值（MB）：`oversized_only` 的触发条件，同时是压缩的大小目标；`0` 表示无大小目标仅转格式 |
| `min_source_size_kb` | `4.0` | 小于该大小（KB）的图片不处理 |
| `skip_source_formats` | `[]` | 无条件跳过的源格式（小写），如 `["gif"]` |
| `skip_if_already_target` | `true` | 源图片**格式已是输出格式、且大小未超过阈值**（即已达标）时跳过，避免重复有损压缩 |
| `process_forward` | `true` | 递归处理合并转发节点内的图片 |

### [output] 输出

| 字段 | 默认值 | 说明 |
|---|---|---|
| `format` | `"webp"` | 输出格式：`webp` / `jpeg` / `png` |
| `max_quality` | `80` | webp/jpeg 编码质量**上限**（1-100）：图片超过阈值时实际质量会由估算/搜索往下调 |
| `lossless` | `false` | WebP 无损模式：固定用最高压缩率参数（quality=100 + method=6）编码，超阈值时只能缩像素尺寸 |
| `webp_method` | `4` | WebP 有损编码 method（0-6），越大越慢压缩率越高；无损固定用 6 |
| `keep_only_if_smaller` | `true` | 压缩结果不比原图小则保留原图 |
| `max_dimension` | `4096` | 静态图最长边像素上限，超过先等比预缩放；`0` 不限制（动图不受此限制） |

### [animated] 动图

| 字段 | 默认值 | 说明 |
|---|---|---|
| `policy` | `"keep_animated"` | `keep_animated`=保留动画并按输出格式编码（webp→动态 WebP，png→APNG）；`skip`=原样放行；`first_frame`=只留首帧 |
| `max_frames` | `512` | 帧数超过上限的动图跳过，防止编码耗时过久；`0` 不限制 |

注意：jpeg 无法承载动画，输出 jpeg 时 `keep_animated` 在运行时退化为 `skip`，加载时会告警一次（webp / png 可保留动画）。

### [advanced] 高级

| 字段 | 默认值 | 说明 |
|---|---|---|
| `single_pass_only` | `true` | 全尺寸图片只编码一次：先快速估算质量再单次编码；关闭则用循环精确逼近目标 |
| `target_ratio` | `0.9` | 仅单次估算模式使用：估算目标 = `size_threshold_mb` × 该比例，预留浮动空间；循环模式直接以 `size_threshold_mb` 为目标 |
| `quality_floor` | `10` | 质量估算 / 搜索的最低质量，低于此值改为缩小尺寸 |
| `quality_search_iterations` | `6` | 循环模式质量搜索最大轮数（仅 `single_pass_only = false`） |
| `downscale_iterations` | `8` | 循环模式尺寸缩小最大轮数（仅 `single_pass_only = false`） |
| `log_stats` | `true` | 每条消息输出一行压缩统计 |
| `verbose` | `false` | 逐图详细日志（格式、大小、耗时、跳过原因） |

**大小目标压缩**只在原图超过 `size_threshold_mb` 时介入，两种模式：

- **单次编码（默认，`single_pass_only = true`）**：把图片等比缩到 ≤ 64k 像素做一次试编码，按像素数外推全尺寸体积，再用 `q × sqrt(目标/估算体积)` 反解所需质量，全尺寸只编码一次。估算目标取 `size_threshold_mb × target_ratio`（默认 90%），预留上下浮动空间；试编码成本仅为全尺寸压缩的几个百分点。质量压到 `quality_floor` 仍不够时，按同一模型附加一个等比缩放，仍保持单次编码。
- **循环逼近（`single_pass_only = false`，与 fetch-url 算法一致）**：直接以 `size_threshold_mb` 为目标（不乘 `target_ratio`），自适应质量搜索（按 `q × sqrt(目标/实际)` 跳跃，最多 `quality_search_iterations` 轮）；质量到底仍超标再按 `sqrt(目标/实际)` 比例循环缩小尺寸（最多 `downscale_iterations` 轮，最小边长 16 像素）。结果严格不超目标（轮数耗尽除外），但要多次全尺寸编码。

无损 WebP 与 PNG 输出没有质量可调（固定最高压缩率参数），超过阈值时两种模式都**只能缩小图片像素尺寸**来满足目标。大小目标压缩仅对静态图生效。

### [performance] 性能

| 字段 | 默认值 | 说明 |
|---|---|---|
| `max_parallel_images` | `4` | 同一条消息内并行压缩的图片上限 |

## 性能与注意事项

- **再次提醒**：未关闭 `visual.handle_oversized_images` 时，同一张图可能经历内置压缩 → 本插件 → VLM 送图三轮有损重编码。
- 插件运行在独立 Runner 进程；`chat.receive.before_process` 是 BLOCKING Hook，**这条消息**的处理链必须等压缩结果（Hook 语义决定），但 Pillow 编码在 C 库层释放 GIL，压缩在线程池执行，不会卡住插件事件循环，多图可真正并行。
- `webp_method` 越大编码越慢：静态图通常无感，动图帧多时差异明显，默认 `4` 是质量/速度的平衡点。
- 替换图片字节后插件会同步重写组件的 sha256 hash，下游存储与识图按新 WebP 数据正常工作。
- 只处理 `type == "image"` 的组件；表情包（emoji）、语音等组件不受影响，与内置压缩的处理范围一致。
- Hook 超时设为 30 秒（编码超大动图的余量）；单图压缩失败只跳过该图，不影响消息链。

## 测试

```bash
PYTHONPATH=../maibot-plugin-sdk python tests/smoke_test.py
```

离线冒烟测试，不依赖 MaiBot Host，使用 Pillow 生成的图片验证压缩纯函数与配置一致性。

## License

MIT
