# Changelog

本文件记录 maibot-image-recompress（智能入站图片重压缩）的版本变更。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 变更

- manifest 兼容上限改为实际验证过的范围：宿主 `max_version` 1.99.99 → **1.3.99**，SDK `max_version` 2.99.99 → **2.14.99**（宿主 1.3.8 + SDK 2.12.0 上实际运行，SDK 2.14.0 上测试通过）。插件仓库审核不再接受「永远兼容」式的上限

## [0.2.5] - 2026-08-01

### 变更

- 将带注释的配置模板改为 `config.default.toml`，运行期 `config.toml` 不再入库
- 在 `create_plugin` / `on_load` 中从模板补齐或恢复 Runner 生成的空壳配置

## [0.2.4] - 2026-07-11

### 修复

- 持久化配置前去除 `None`；WebUI 清空可选字段时按默认值处理

## [0.2.3] - 2026-06-21

### 修复

- 混合分辨率多帧图（如手机 MPO）仅保留最大帧
- 动图输出应用 `max_dimension`；`keep_animated` 失败时回退 `first_frame` 再 skip

### 变更

- 默认改为 `keep_only_if_smaller=false`、`max_dimension=2000`

## [0.2.2] - 2026-06-14

### 修复

- 静态 / 动图输出强制 WebP 单边 ≤16383px，超宽高图先缩小再编码，避免静默失败

## [0.2.1] - 2026-06-14

### 变更

- verbose 模式记录实际编码使用的 `final_width` / `final_height` / `final_quality`

## [0.2.0] - 2026-06-13

### 变更

- 空配置字段升级时跟随代码默认值；恢复详细 `config.toml` 注释
- `keep_animated` 策略、APNG 输出；`config_version` 1.2.0 迁移
- 插件更名为「智能入站图片重压缩」；`animated_webp` 由 `keep_animated` 替代

## [0.1.0] - 2026-06-12

### 新增

- 首次发布：入站图片智能重压缩
- 提醒使用前关闭 Host `visual.handle_oversized_images`
