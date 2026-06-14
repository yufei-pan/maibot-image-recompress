"""智能入站图片重压缩插件。

在 ``chat.receive.before_process`` Hook 中，把入站消息里的图片组件重压缩为
更高效的目标格式（默认 WebP），用于替代主程序内置的 JPEG 压缩。

工作前提：主程序内置压缩（``visual.handle_oversized_images``）在本 Hook 之前
执行，会先把超大图转成 JPEG。要让本插件接管压缩，必须在 bot_config.toml 中
将其关闭；插件会在加载时检测并告警。

并发模型：插件运行在独立 Runner 进程，本 Hook 为 BLOCKING——该条消息的处理
链必须等压缩结果返回，这是 Hook 语义决定的。Pillow 的编解码在 C 库层会释放
GIL，因此把压缩丢进线程池即可让 Runner 事件循环保持空闲，同一条消息内的多张
图片也能真正并行。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Iterator

from PIL import Image, ImageOps, ImageSequence

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

# 触发模式：always=所有入站图片都重压缩；oversized_only=仅超过阈值的图片
VALID_MODES = ("always", "oversized_only")
# 输出格式与 Pillow 格式名的映射
PIL_FORMAT_NAMES = {"webp": "WEBP", "jpeg": "JPEG", "png": "PNG"}
# 动图策略：keep_animated=保留动画按输出格式编码；skip=不处理；first_frame=只保留首帧
VALID_ANIMATED_POLICIES = ("keep_animated", "skip", "first_frame")
# 能承载动画的输出格式：webp→动态 WebP，png→APNG；jpeg 不支持
ANIMATED_CAPABLE_FORMATS = ("webp", "png")

CURRENT_CONFIG_VERSION = "1.2.0"

DEFAULT_TRIGGER_MODE = "always"
DEFAULT_SIZE_THRESHOLD_MB = 1.0
DEFAULT_MIN_SOURCE_SIZE_KB = 4.0
DEFAULT_SKIP_IF_ALREADY_TARGET = True
DEFAULT_PROCESS_FORWARD = True

DEFAULT_OUTPUT_FORMAT = "webp"
DEFAULT_MAX_QUALITY = 80
DEFAULT_LOSSLESS = False
DEFAULT_WEBP_METHOD = 4
DEFAULT_KEEP_ONLY_IF_SMALLER = True
DEFAULT_MAX_DIMENSION = 4096

DEFAULT_ANIMATED_POLICY = "keep_animated"
DEFAULT_MAX_FRAMES = 512

DEFAULT_SINGLE_PASS_ONLY = True
DEFAULT_TARGET_RATIO = 0.9
DEFAULT_QUALITY_FLOOR = 10
DEFAULT_QUALITY_SEARCH_ITERATIONS = 6
DEFAULT_DOWNSCALE_ITERATIONS = 8
DEFAULT_LOG_STATS = True
DEFAULT_VERBOSE = False

DEFAULT_MAX_PARALLEL_IMAGES = 4

# 动图帧缺省时长（毫秒），与 Pillow 对 GIF 的常见缺省值一致
DEFAULT_FRAME_DURATION_MS = 100

# 质量估算用的试编码像素数上限（编码耗时大致与像素数成正比，
# 试编码成本仅为全尺寸压缩的几个百分点）
PROBE_MAX_PIXELS = 65536


# --------------------------------------------------------------------------- #
# 配置模型
# --------------------------------------------------------------------------- #


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default=CURRENT_CONFIG_VERSION, description="配置版本")


class TriggerSectionConfig(PluginConfigBase):
    """触发条件配置：决定哪些图片会被重压缩。"""

    __ui_label__ = "触发条件"
    __ui_icon__ = "filter"
    __ui_order__ = 1

    mode: str = Field(
        default="",
        description="触发模式：always=所有入站图片都重压缩；oversized_only=仅压缩超过 size_threshold 的图片。留空使用插件内置默认。",
        json_schema_extra={"placeholder": DEFAULT_TRIGGER_MODE},
    )
    size_threshold_mb: float | None = Field(
        default=None,
        description=(
            "图片大小阈值（MB）：oversized_only 模式下作为触发条件，同时作为压缩的大小目标。"
            "0 表示无大小目标，仅转换格式。留空使用插件内置默认。"
        ),
        json_schema_extra={"placeholder": str(DEFAULT_SIZE_THRESHOLD_MB)},
    )
    min_source_size_kb: float | None = Field(
        default=None,
        description="小于该大小（KB）的图片不处理（压缩收益太小）。留空使用插件内置默认。",
        json_schema_extra={"placeholder": str(DEFAULT_MIN_SOURCE_SIZE_KB)},
    )
    skip_source_formats: list[str] = Field(
        default_factory=list,
        description='无条件跳过的源格式列表（小写），例如 ["gif", "bmp"]。',
    )
    skip_if_already_target: bool | None = Field(
        default=None,
        description=(
            "源图片格式已经是输出格式、且大小未超过 size_threshold_mb（或无大小目标）时跳过，"
            "避免对已达标的图片做重复有损压缩造成画质劣化。留空使用插件内置默认。"
        ),
        json_schema_extra={"placeholder": str(DEFAULT_SKIP_IF_ALREADY_TARGET).lower()},
    )
    process_forward: bool | None = Field(
        default=None,
        description="是否递归处理合并转发消息节点内的图片（转发可多层嵌套）。留空使用插件内置默认。",
        json_schema_extra={"placeholder": str(DEFAULT_PROCESS_FORWARD).lower()},
    )


class OutputSectionConfig(PluginConfigBase):
    """输出格式配置。"""

    __ui_label__ = "输出"
    __ui_icon__ = "image"
    __ui_order__ = 2

    format: str = Field(
        default="",
        description="输出格式：webp / jpeg / png。webp 在同等画质下体积通常最小。留空使用插件内置默认。",
        json_schema_extra={"placeholder": DEFAULT_OUTPUT_FORMAT},
    )
    max_quality: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description=(
            "webp / jpeg 的编码质量上限（1-100，png 忽略）。"
            "这只是最高质量：图片超过大小阈值时，实际编码质量会由估算 / 搜索从这里往下调。"
            "留空使用插件内置默认。"
        ),
        json_schema_extra={"placeholder": str(DEFAULT_MAX_QUALITY)},
    )
    lossless: bool | None = Field(
        default=None,
        description=(
            "WebP 无损模式（仅 format=webp 时有效）。无损与 png 输出固定使用最高压缩率参数编码"
            "（webp 无损为 quality=100 + method=6，png 为 compress_level=9），"
            "无法通过降质量缩小体积，超过大小阈值时只能缩小图片像素尺寸。留空使用插件内置默认。"
        ),
        json_schema_extra={"placeholder": str(DEFAULT_LOSSLESS).lower()},
    )
    webp_method: int | None = Field(
        default=None,
        ge=0,
        le=6,
        description="WebP 有损编码 method（0-6）：越大压缩率越高但越慢；动图较多时建议不超过 4。无损模式固定用 6。留空使用插件内置默认。",
        json_schema_extra={"placeholder": str(DEFAULT_WEBP_METHOD)},
    )
    keep_only_if_smaller: bool | None = Field(
        default=None,
        description="仅当压缩结果比原图更小时才替换，否则保留原图。留空使用插件内置默认。",
        json_schema_extra={"placeholder": str(DEFAULT_KEEP_ONLY_IF_SMALLER).lower()},
    )
    max_dimension: int | None = Field(
        default=None,
        ge=0,
        description=(
            "静态图最长边像素上限，超过先等比预缩放再编码（避免在超大图上浪费压缩计算）；"
            "0 表示不限制。动图不受此限制。留空使用插件内置默认。"
        ),
        json_schema_extra={"placeholder": str(DEFAULT_MAX_DIMENSION)},
    )


class AnimatedSectionConfig(PluginConfigBase):
    """动图（GIF 等多帧图片）处理配置。"""

    __ui_label__ = "动图"
    __ui_icon__ = "film"
    __ui_order__ = 3

    policy: str = Field(
        default="",
        description=(
            "动图处理策略：keep_animated=保留动画并按输出格式编码（webp→动态 WebP，png→APNG）；"
            "skip=动图原样放行；first_frame=只保留首帧转静态图。"
            "输出格式为 jpeg（无法承载动画）时 keep_animated 自动退化为 skip。留空使用插件内置默认。"
        ),
        json_schema_extra={"placeholder": DEFAULT_ANIMATED_POLICY},
    )
    max_frames: int | None = Field(
        default=None,
        ge=0,
        description="动图帧数上限，超过则跳过该图（防止超长 GIF 编码耗时过久）；0 表示不限制。留空使用插件内置默认。",
        json_schema_extra={"placeholder": str(DEFAULT_MAX_FRAMES)},
    )


class AdvancedSectionConfig(PluginConfigBase):
    """高级压缩与日志配置。"""

    __ui_label__ = "高级"
    __ui_icon__ = "settings"
    __ui_order__ = 4

    single_pass_only: bool | None = Field(
        default=None,
        description=(
            "只对全尺寸图片编码一次：先用小图试编码快速估算达到大小目标所需的质量（远快于实际压图），"
            "再按估算质量单次编码，结果允许在目标附近上下浮动；"
            "关闭则改用多轮“质量搜索 + 缩放”循环精确逼近目标。仅对静态图生效，lossless 模式下不启用。"
            "留空使用插件内置默认。"
        ),
        json_schema_extra={"placeholder": str(DEFAULT_SINGLE_PASS_ONLY).lower()},
    )
    target_ratio: float | None = Field(
        default=None,
        ge=0.1,
        le=1.0,
        description=(
            "仅 single_pass_only 模式的数学估算使用：估算目标 = size_threshold_mb × 该比例，"
            "预留些许上下浮动空间。循环模式不受影响，直接以 size_threshold_mb 为目标。"
            "留空使用插件内置默认。"
        ),
        json_schema_extra={"placeholder": str(DEFAULT_TARGET_RATIO)},
    )
    quality_floor: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description="质量估算 / 搜索允许的最低质量，低于此值改为缩小图片尺寸。留空使用插件内置默认。",
        json_schema_extra={"placeholder": str(DEFAULT_QUALITY_FLOOR)},
    )
    quality_search_iterations: int | None = Field(
        default=None,
        ge=1,
        description="循环模式下自适应质量搜索的最大轮数（按 q × sqrt(目标/实际) 启发式跳跃）。仅 single_pass_only = false 时生效。留空使用插件内置默认。",
        json_schema_extra={"placeholder": str(DEFAULT_QUALITY_SEARCH_ITERATIONS)},
    )
    downscale_iterations: int | None = Field(
        default=None,
        ge=0,
        description="循环模式下质量到达下限仍超标时，循环缩小尺寸的最大轮数。仅 single_pass_only = false 时生效。留空使用插件内置默认。",
        json_schema_extra={"placeholder": str(DEFAULT_DOWNSCALE_ITERATIONS)},
    )
    log_stats: bool | None = Field(
        default=None,
        description="每条消息处理后输出一行压缩统计日志。留空使用插件内置默认。",
        json_schema_extra={"placeholder": str(DEFAULT_LOG_STATS).lower()},
    )
    verbose: bool | None = Field(
        default=None,
        description="输出每张图片的详细处理日志（格式、大小、耗时、跳过原因）。留空使用插件内置默认。",
        json_schema_extra={"placeholder": str(DEFAULT_VERBOSE).lower()},
    )


class PerformanceSectionConfig(PluginConfigBase):
    """性能配置。"""

    __ui_label__ = "性能"
    __ui_icon__ = "zap"
    __ui_order__ = 5

    max_parallel_images: int | None = Field(
        default=None,
        ge=1,
        description="同一条消息内并行压缩的图片数量上限（线程池并发，信号量控制）。留空使用插件内置默认。",
        json_schema_extra={"placeholder": str(DEFAULT_MAX_PARALLEL_IMAGES)},
    )


class ImageRecompressConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    trigger: TriggerSectionConfig = Field(default_factory=TriggerSectionConfig)
    output: OutputSectionConfig = Field(default_factory=OutputSectionConfig)
    animated: AnimatedSectionConfig = Field(default_factory=AnimatedSectionConfig)
    advanced: AdvancedSectionConfig = Field(default_factory=AdvancedSectionConfig)
    performance: PerformanceSectionConfig = Field(default_factory=PerformanceSectionConfig)


# --------------------------------------------------------------------------- #
# 配置解析（空值 = 使用代码内置默认，便于版本升级后自动跟随新默认）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EffectiveImageRecompressConfig:
    """运行时生效的插件配置（已解析占位空值）。"""

    mode: str
    size_threshold_mb: float
    min_source_size_kb: float
    skip_source_formats: tuple[str, ...]
    skip_if_already_target: bool
    process_forward: bool
    out_format: str
    max_quality: int
    lossless: bool
    webp_method: int
    keep_only_if_smaller: bool
    max_dimension: int
    animated_policy: str
    max_frames: int
    single_pass_only: bool
    target_ratio: float
    quality_floor: int
    quality_search_iterations: int
    downscale_iterations: int
    log_stats: bool
    verbose: bool
    max_parallel_images: int


def _effective_bool(value: bool | None, default: bool) -> bool:
    if value is None:
        return default
    return bool(value)


def _effective_str(value: str | None, default: str) -> str:
    if value is None or not str(value).strip():
        return default
    return str(value)


def _effective_float(value: float | None, default: float, *, minimum: float = 0.0) -> float:
    if value is None:
        return default
    return max(minimum, float(value))


def _effective_int(value: int | None, default: int, *, minimum: int = 0) -> int:
    if value is None:
        return default
    return max(minimum, int(value))


def resolve_effective_config(cfg: ImageRecompressConfig) -> EffectiveImageRecompressConfig:
    return EffectiveImageRecompressConfig(
        mode=_effective_str(cfg.trigger.mode, DEFAULT_TRIGGER_MODE),
        size_threshold_mb=_effective_float(cfg.trigger.size_threshold_mb, DEFAULT_SIZE_THRESHOLD_MB),
        min_source_size_kb=_effective_float(cfg.trigger.min_source_size_kb, DEFAULT_MIN_SOURCE_SIZE_KB),
        skip_source_formats=tuple(
            fmt.strip().lower() for fmt in cfg.trigger.skip_source_formats if fmt.strip()
        ),
        skip_if_already_target=_effective_bool(
            cfg.trigger.skip_if_already_target, DEFAULT_SKIP_IF_ALREADY_TARGET
        ),
        process_forward=_effective_bool(cfg.trigger.process_forward, DEFAULT_PROCESS_FORWARD),
        out_format=_effective_str(cfg.output.format, DEFAULT_OUTPUT_FORMAT).lower(),
        max_quality=_effective_int(cfg.output.max_quality, DEFAULT_MAX_QUALITY, minimum=1),
        lossless=_effective_bool(cfg.output.lossless, DEFAULT_LOSSLESS),
        webp_method=_effective_int(cfg.output.webp_method, DEFAULT_WEBP_METHOD),
        keep_only_if_smaller=_effective_bool(cfg.output.keep_only_if_smaller, DEFAULT_KEEP_ONLY_IF_SMALLER),
        max_dimension=_effective_int(cfg.output.max_dimension, DEFAULT_MAX_DIMENSION),
        animated_policy=_effective_str(cfg.animated.policy, DEFAULT_ANIMATED_POLICY),
        max_frames=_effective_int(cfg.animated.max_frames, DEFAULT_MAX_FRAMES),
        single_pass_only=_effective_bool(cfg.advanced.single_pass_only, DEFAULT_SINGLE_PASS_ONLY),
        target_ratio=min(1.0, max(0.1, _effective_float(cfg.advanced.target_ratio, DEFAULT_TARGET_RATIO, minimum=0.1))),
        quality_floor=_effective_int(cfg.advanced.quality_floor, DEFAULT_QUALITY_FLOOR, minimum=1),
        quality_search_iterations=_effective_int(
            cfg.advanced.quality_search_iterations, DEFAULT_QUALITY_SEARCH_ITERATIONS, minimum=1
        ),
        downscale_iterations=_effective_int(cfg.advanced.downscale_iterations, DEFAULT_DOWNSCALE_ITERATIONS),
        log_stats=_effective_bool(cfg.advanced.log_stats, DEFAULT_LOG_STATS),
        verbose=_effective_bool(cfg.advanced.verbose, DEFAULT_VERBOSE),
        max_parallel_images=_effective_int(
            cfg.performance.max_parallel_images, DEFAULT_MAX_PARALLEL_IMAGES, minimum=1
        ),
    )


_LEGACY_BAKED_DEFAULTS: dict[str, dict[str, bool | float | int | str | list[str]]] = {
    "trigger": {
        "mode": DEFAULT_TRIGGER_MODE,
        "size_threshold_mb": DEFAULT_SIZE_THRESHOLD_MB,
        "min_source_size_kb": DEFAULT_MIN_SOURCE_SIZE_KB,
        "skip_if_already_target": DEFAULT_SKIP_IF_ALREADY_TARGET,
        "process_forward": DEFAULT_PROCESS_FORWARD,
    },
    "output": {
        "format": DEFAULT_OUTPUT_FORMAT,
        "max_quality": DEFAULT_MAX_QUALITY,
        "lossless": DEFAULT_LOSSLESS,
        "webp_method": DEFAULT_WEBP_METHOD,
        "keep_only_if_smaller": DEFAULT_KEEP_ONLY_IF_SMALLER,
        "max_dimension": DEFAULT_MAX_DIMENSION,
    },
    "animated": {
        "policy": DEFAULT_ANIMATED_POLICY,
        "max_frames": DEFAULT_MAX_FRAMES,
    },
    "advanced": {
        "single_pass_only": DEFAULT_SINGLE_PASS_ONLY,
        "target_ratio": DEFAULT_TARGET_RATIO,
        "quality_floor": DEFAULT_QUALITY_FLOOR,
        "quality_search_iterations": DEFAULT_QUALITY_SEARCH_ITERATIONS,
        "downscale_iterations": DEFAULT_DOWNSCALE_ITERATIONS,
        "log_stats": DEFAULT_LOG_STATS,
        "verbose": DEFAULT_VERBOSE,
    },
    "performance": {
        "max_parallel_images": DEFAULT_MAX_PARALLEL_IMAGES,
    },
}


def _migrate_legacy_baked_defaults(config: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """将旧版 config.toml 中写死的默认值还原为占位空值，以便跟随代码内置默认。"""
    changed = False
    for section_name, fields in _LEGACY_BAKED_DEFAULTS.items():
        section = config.get(section_name)
        if not isinstance(section, dict):
            continue
        for key, legacy_value in fields.items():
            if key not in section or section[key] != legacy_value:
                continue
            if isinstance(legacy_value, str):
                section[key] = ""
            else:
                section[key] = None
            changed = True

    plugin_section = config.get("plugin")
    if isinstance(plugin_section, dict):
        plugin_section["config_version"] = CURRENT_CONFIG_VERSION

    return config, changed


# --------------------------------------------------------------------------- #
# 压缩参数快照与结果
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RecompressSettings:
    """压缩参数快照。

    纯压缩函数只依赖本快照，不触碰 pydantic 配置对象，保证线程安全且便于测试。
    on_load / on_config_update 时由 :meth:`ImageRecompressPlugin._build_settings` 重建。
    """

    mode: str
    # 大小阈值（字节，由 size_threshold_mb 换算）：触发条件 + 压缩大小目标；0 表示无大小目标
    size_threshold: int
    # 处理下限（字节，由 min_source_size_kb 换算）
    min_source_size: int
    skip_source_formats: tuple[str, ...]
    skip_if_already_target: bool
    process_forward: bool
    out_format: str
    # 编码质量上限：实际质量会由估算 / 搜索从这里往下调
    max_quality: int
    lossless: bool
    webp_method: int
    keep_only_if_smaller: bool
    # 静态图最长边像素上限，0 表示不限制
    max_dimension: int
    animated_policy: str
    # 动图帧数上限，0 表示不限制
    max_frames: int
    # True=估算质量后单次编码；False=多轮循环精确逼近目标
    single_pass_only: bool
    # 仅单次估算模式使用的目标字节数（size_threshold × target_ratio），预留浮动空间
    estimation_target: int
    quality_floor: int
    quality_search_iterations: int
    downscale_iterations: int


@dataclass
class RecompressResult:
    """单张图片的压缩结果。``new_bytes`` 为 None 表示跳过，原因见 ``skipped_reason``。"""

    new_bytes: bytes | None
    skipped_reason: str | None = None
    src_format: str = ""
    was_animated: bool = False
    orig_size: int = 0
    new_size: int = 0
    elapsed_ms: float = 0.0
    # 转换后的图片尺寸与实际使用的编码质量（无损/png 为 None）
    final_width: int = 0
    final_height: int = 0
    final_quality: int | None = None


# --------------------------------------------------------------------------- #
# 纯同步压缩函数（不依赖 SDK，可独立测试）
# --------------------------------------------------------------------------- #


def _iter_image_components(components: list[Any], include_forward: bool) -> Iterator[dict[str, Any]]:
    """遍历组件字典列表，产出所有 type=="image" 的组件。

    合并转发组件（type=="forward"）的节点 content 内可再嵌套转发，需要递归。
    """
    for component in components:
        if not isinstance(component, dict):
            continue
        component_type = component.get("type")
        if component_type == "image":
            yield component
        elif component_type == "forward" and include_forward:
            forward_nodes = component.get("data")
            if not isinstance(forward_nodes, list):
                continue
            for node in forward_nodes:
                if not isinstance(node, dict):
                    continue
                node_content = node.get("content")
                if isinstance(node_content, list):
                    yield from _iter_image_components(node_content, include_forward)


def _has_alpha(image: Image.Image) -> bool:
    """判断图片是否带透明通道（含调色板透明）。"""
    return image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)


def _prepare_static_frame(image: Image.Image, out_format: str) -> Image.Image:
    """把静态图整理为适合目标格式编码的模式。

    - 先应用 EXIF 方向（转码后 EXIF 会丢失，必须先转正）；
    - webp / png 保留透明通道；
    - jpeg 不支持透明，把 alpha 拍平到白色背景（与主程序内置压缩行为一致）。
    """
    normalized = ImageOps.exif_transpose(image)
    if _has_alpha(normalized):
        if out_format == "jpeg":
            alpha_image = normalized.convert("RGBA")
            background = Image.new("RGB", alpha_image.size, (255, 255, 255))
            background.paste(alpha_image, mask=alpha_image.getchannel("A"))
            return background
        return normalized.convert("RGBA")
    return normalized.convert("RGB")


def _quality_adjustable(settings: RecompressSettings) -> bool:
    """输出是否能通过降质量缩小体积：webp 无损与 png 不行，只能缩像素尺寸。"""
    return settings.out_format in ("webp", "jpeg") and not settings.lossless


def _encode_static(image: Image.Image, settings: RecompressSettings, quality: int) -> bytes:
    """按目标格式编码单帧静态图。

    webp 无损与 png 固定使用最高压缩率参数（quality 参数对其无意义）。
    """
    buffer = BytesIO()
    if settings.out_format == "webp":
        if settings.lossless:
            # 无损模式：quality 表示压缩努力程度，固定拉满
            image.save(buffer, format="WEBP", lossless=True, quality=100, method=6)
        else:
            image.save(buffer, format="WEBP", quality=quality, method=settings.webp_method)
    elif settings.out_format == "jpeg":
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
    else:
        image.save(buffer, format="PNG", optimize=True, compress_level=9)
    return buffer.getvalue()


def _resize_keep_aspect(image: Image.Image, factor: float) -> Image.Image:
    """按比例缩小图片，保持纵横比，最小边长 16 像素。"""
    new_width = max(16, round(image.width * factor))
    new_height = max(16, round(image.height * factor))
    return image.resize((new_width, new_height), Image.Resampling.LANCZOS)


def _estimate_quality_and_scale(image: Image.Image, settings: RecompressSettings) -> tuple[int, float]:
    """快速估算满足估算目标所需的质量，必要时附加一个等比缩放比例。

    做法：把图片等比缩到不超过 ``PROBE_MAX_PIXELS`` 像素做一次试编码，按像素数
    外推全尺寸体积，再用与循环压缩一致的 sqrt 启发式反解质量。质量下限仍不够时，
    按“体积近似随质量平方变化”的同一模型补一个缩放比例，保证仍然只做单次全尺寸编码。

    webp 无损与 png 没有质量可调，超出目标时直接按“体积随像素数线性变化”反解缩放比例。

    Returns:
        (估算质量, 缩放比例)；比例为 1.0 表示无需缩放。
    """
    target_size = settings.estimation_target
    pixels = image.width * image.height
    probe = image
    if pixels > PROBE_MAX_PIXELS:
        probe = _resize_keep_aspect(image, math.sqrt(PROBE_MAX_PIXELS / pixels))
    probe_encoded = _encode_static(probe, settings, settings.max_quality)
    estimated_full = len(probe_encoded) * pixels / (probe.width * probe.height)

    if estimated_full <= target_size:
        return settings.max_quality, 1.0

    if not _quality_adjustable(settings):
        # 无损 / png：只能缩小像素尺寸
        scale = math.sqrt(target_size / estimated_full)
        return settings.max_quality, max(0.1, min(1.0, scale))

    estimated_quality = int(settings.max_quality * math.sqrt(target_size / estimated_full))
    if estimated_quality >= settings.quality_floor:
        return estimated_quality, 1.0

    # 质量压到下限仍不够：按 q² 体积模型估算下限时的体积，反解需要的缩放比例
    estimated_at_floor = estimated_full * (settings.quality_floor / settings.max_quality) ** 2
    scale = math.sqrt(target_size / estimated_at_floor)
    return settings.quality_floor, max(0.1, min(1.0, scale))


def _adaptive_quality_search(
    image: Image.Image,
    settings: RecompressSettings,
    target_size: int,
) -> tuple[bytes, bool]:
    """自适应质量搜索：按 ``q × sqrt(target/actual)`` 启发式跳跃而非固定步进。

    Returns:
        (编码结果, 是否满足大小目标)
    """
    quality = max(settings.quality_floor, min(100, settings.max_quality))
    encoded = b""
    for _ in range(settings.quality_search_iterations):
        encoded = _encode_static(image, settings, quality)
        if len(encoded) <= target_size:
            return encoded, True
        if quality <= settings.quality_floor:
            break
        estimated = int(quality * math.sqrt(target_size / len(encoded)))
        quality = max(settings.quality_floor, min(quality - 5, estimated))
    return encoded, False


def _compress_to_target(image: Image.Image, settings: RecompressSettings) -> bytes:
    """循环压缩到 size_threshold 以内：先自适应质量搜索，质量到底仍超标再循环缩小尺寸。

    webp 无损与 png 没有质量可调，跳过质量搜索，单次编码后直接进入缩放阶段。
    """
    target_size = settings.size_threshold
    working_image = image
    if _quality_adjustable(settings):
        encoded, fits = _adaptive_quality_search(working_image, settings, target_size)
        floor_quality = settings.quality_floor
    else:
        encoded = _encode_static(working_image, settings, settings.max_quality)
        fits = len(encoded) <= target_size
        floor_quality = settings.max_quality

    # 质量下限仍超标：按 sqrt(target/actual) 比例逐步缩小尺寸
    for _ in range(settings.downscale_iterations):
        if fits:
            break
        if min(working_image.size) <= 16:
            break
        factor = math.sqrt(target_size / len(encoded))
        factor = max(0.3, min(0.95, factor))
        working_image = _resize_keep_aspect(working_image, factor)
        encoded = _encode_static(working_image, settings, floor_quality)
        fits = len(encoded) <= target_size

    return encoded


def _encode_animated(image: Image.Image, settings: RecompressSettings) -> bytes:
    """把多帧图片整体转为动画，保留每帧时长与循环次数。

    webp 输出为动态 WebP，png 输出为 APNG；两者均保留逐帧时长与 loop。
    """
    frames: list[Image.Image] = []
    durations: list[int] = []
    for frame in ImageSequence.Iterator(image):
        frames.append(frame.convert("RGBA"))
        durations.append(int(frame.info.get("duration", DEFAULT_FRAME_DURATION_MS)) or DEFAULT_FRAME_DURATION_MS)

    buffer = BytesIO()
    loop = int(image.info.get("loop", 0))
    if settings.out_format == "webp":
        if settings.lossless:
            # 无损模式固定使用最高压缩率参数
            encode_kwargs = {"lossless": True, "quality": 100, "method": 6}
        else:
            encode_kwargs = {"quality": settings.max_quality, "method": settings.webp_method}
        frames[0].save(
            buffer,
            format="WEBP",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=loop,
            **encode_kwargs,
        )
    else:
        # APNG：PNG 无损，固定最高压缩率
        frames[0].save(
            buffer,
            format="PNG",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=loop,
            optimize=True,
            compress_level=9,
        )
    return buffer.getvalue()


def _recompress_blocking(data: bytes, settings: RecompressSettings) -> RecompressResult:
    """单张图片重压缩总入口（同步阻塞，在线程池中执行）。

    任何失败都转化为跳过结果返回，绝不向上抛异常中断 Hook。
    """
    start_time = time.perf_counter()
    orig_size = len(data)

    try:
        with Image.open(BytesIO(data)) as image:
            src_format = (image.format or "").lower()
            if src_format in settings.skip_source_formats:
                return RecompressResult(None, f"源格式 {src_format} 在跳过列表中", src_format, orig_size=orig_size)

            # 源格式与输出格式一致、且大小符合要求（未超阈值或无大小目标）时跳过，
            # 避免对已达标的图片做重复有损压缩
            size_acceptable = settings.size_threshold <= 0 or orig_size <= settings.size_threshold
            if settings.skip_if_already_target and src_format == settings.out_format and size_acceptable:
                return RecompressResult(None, "已是目标格式且大小符合要求", src_format, orig_size=orig_size)

            is_animated = bool(getattr(image, "is_animated", False)) and getattr(image, "n_frames", 1) > 1

            final_quality: int | None = None
            final_width: int = 0
            final_height: int = 0
            if is_animated:
                if settings.animated_policy == "skip":
                    return RecompressResult(None, "动图按配置跳过", src_format, True, orig_size=orig_size)
                if settings.animated_policy == "keep_animated":
                    frame_count = getattr(image, "n_frames", 1)
                    # max_frames 为 0 表示不限制帧数
                    if settings.max_frames > 0 and frame_count > settings.max_frames:
                        return RecompressResult(
                            None,
                            f"动图帧数 {frame_count} 超过上限 {settings.max_frames}",
                            src_format,
                            True,
                            orig_size=orig_size,
                        )
                    new_bytes = _encode_animated(image, settings)
                    # 从第一帧读取尺寸
                    image.seek(0)
                    final_width, final_height = image.size
                    final_quality = settings.max_quality if _quality_adjustable(settings) else None
                else:
                    # first_frame：取首帧按静态图处理
                    image.seek(0)
                    new_bytes, final_quality, final_width, final_height = _encode_static_pipeline(
                        image, settings, orig_size
                    )
            else:
                new_bytes, final_quality, final_width, final_height = _encode_static_pipeline(
                    image, settings, orig_size
                )
    except Exception as exc:
        return RecompressResult(None, f"压缩失败: {exc}", orig_size=orig_size)

    elapsed_ms = (time.perf_counter() - start_time) * 1000
    return RecompressResult(
        new_bytes=new_bytes,
        src_format=src_format,
        was_animated=is_animated,
        orig_size=orig_size,
        new_size=len(new_bytes),
        elapsed_ms=elapsed_ms,
        final_width=final_width,
        final_height=final_height,
        final_quality=final_quality,
    )


def _encode_static_pipeline(
    image: Image.Image, settings: RecompressSettings, orig_size: int
) -> tuple[bytes, int | None, int, int]:
    """静态图编码管线：模式整理 → 超大图预缩放 → 质量估算单次编码或循环逼近。

    Returns:
        (encoded_bytes, quality_used, final_width, final_height)
        quality_used 为 None 表示无损/png（质量参数无意义）。
    """
    prepared = _prepare_static_frame(image, settings.out_format)
    # 预缩放：超大图先压到 max_dimension，避免在高质量大图上浪费压缩计算
    if settings.max_dimension > 0 and max(prepared.size) > settings.max_dimension:
        prepared = _resize_keep_aspect(prepared, settings.max_dimension / max(prepared.size))

    quality_adjustable = _quality_adjustable(settings)

    # 原图未超过阈值时单次最高质量转码即可
    if settings.size_threshold <= 0 or orig_size <= settings.size_threshold:
        encoded = _encode_static(prepared, settings, settings.max_quality)
        quality = settings.max_quality if quality_adjustable else None
        return encoded, quality, prepared.width, prepared.height

    if settings.single_pass_only:
        # 估算质量（无损 / png 则估算缩放比例）后全尺寸单次编码
        quality, scale = _estimate_quality_and_scale(prepared, settings)
        if scale < 1.0:
            prepared = _resize_keep_aspect(prepared, scale)
        encoded = _encode_static(prepared, settings, quality)
        return encoded, quality if quality_adjustable else None, prepared.width, prepared.height

    encoded = _compress_to_target(prepared, settings)
    return encoded, None, prepared.width, prepared.height


# --------------------------------------------------------------------------- #
# 插件主体
# --------------------------------------------------------------------------- #


class ImageRecompressPlugin(MaiBotPlugin):
    """智能入站图片重压缩插件主体。"""

    config_model = ImageRecompressConfig

    def __init__(self) -> None:
        super().__init__()
        # 压缩参数快照，on_load / on_config_update 时重建
        self._settings: RecompressSettings | None = None
        self._log_stats = DEFAULT_LOG_STATS
        self._verbose = DEFAULT_VERBOSE
        self._max_parallel_images = DEFAULT_MAX_PARALLEL_IMAGES

    def normalize_plugin_config(
        self, config_data: Mapping[str, Any] | None
    ) -> tuple[dict[str, Any], bool]:
        normalized, changed = super().normalize_plugin_config(config_data)
        migrated, migrated_changed = _migrate_legacy_baked_defaults(normalized)
        return migrated, changed or migrated_changed

    def _effective(self) -> EffectiveImageRecompressConfig:
        return resolve_effective_config(self.config)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def on_load(self) -> None:
        """插件加载：构建参数快照并检查主程序内置压缩状态。"""
        self._refresh_settings()

        # 内置压缩在本 Hook 之前执行（bot.py 先压缩后触发 chat.receive.before_process），
        # 不关闭的话图片会先被转成 JPEG 再交给本插件
        host_compress_enabled = await self.ctx.config.get("visual.handle_oversized_images", True)
        if host_compress_enabled:
            self.ctx.logger.warning(
                "检测到主程序内置图片压缩 visual.handle_oversized_images 仍为开启状态。"
                "内置压缩在本插件 Hook 之前执行，超大图会先被压成 JPEG 再交给本插件。"
                "请在 bot_config.toml 中将其设为 false，由本插件接管压缩。"
            )

        settings = self._settings
        if settings is not None:
            self.ctx.logger.info(
                "智能入站图片重压缩已加载：模式=%s, 输出=%s, 最高质量=%d, 无损=%s, 动图=%s, 并行=%d",
                settings.mode,
                settings.out_format,
                settings.max_quality,
                "开" if settings.lossless else "关",
                settings.animated_policy,
                self._max_parallel_images,
            )

    async def on_unload(self) -> None:
        """插件卸载。"""
        self.ctx.logger.info("智能入站图片重压缩已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热更新：重建参数快照。"""
        del config_data
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self._refresh_settings()
            self.ctx.logger.info("智能入站图片重压缩配置已更新: version=%s", version)

    # ------------------------------------------------------------------ #
    # 配置快照
    # ------------------------------------------------------------------ #
    def _refresh_settings(self) -> None:
        """从 self.config 重建压缩参数快照与日志开关。"""
        effective = self._effective()
        self._settings = self._build_settings(effective)
        self._log_stats = effective.log_stats
        self._verbose = effective.verbose
        self._max_parallel_images = effective.max_parallel_images

    def _build_settings(self, effective: EffectiveImageRecompressConfig) -> RecompressSettings:
        """校验配置枚举字段并消解冲突，生成不可变快照。"""
        mode = effective.mode
        if mode not in VALID_MODES:
            self.ctx.logger.warning("无效的触发模式 %r，回退为 always", mode)
            mode = "always"

        out_format = effective.out_format
        if out_format not in PIL_FORMAT_NAMES:
            self.ctx.logger.warning("无效的输出格式 %r，回退为 webp", out_format)
            out_format = "webp"

        animated_policy = effective.animated_policy
        if animated_policy not in VALID_ANIMATED_POLICIES:
            self.ctx.logger.warning("无效的动图策略 %r，回退为 keep_animated", animated_policy)
            animated_policy = "keep_animated"
        # jpeg 无法承载动画，keep_animated 退化为 skip（webp / png 可保留动画）
        if animated_policy == "keep_animated" and out_format not in ANIMATED_CAPABLE_FORMATS:
            self.ctx.logger.warning("输出格式 %s 无法承载动画，动图策略 keep_animated 退化为 skip", out_format)
            animated_policy = "skip"

        # 阈值统一换算为字节；估算目标 = 阈值 × 比例，仅单次估算模式使用
        size_threshold = int(max(0.0, effective.size_threshold_mb) * 1024 * 1024)
        target_ratio = min(1.0, max(0.1, effective.target_ratio))

        return RecompressSettings(
            mode=mode,
            size_threshold=size_threshold,
            min_source_size=int(max(0.0, effective.min_source_size_kb) * 1024),
            skip_source_formats=effective.skip_source_formats,
            skip_if_already_target=effective.skip_if_already_target,
            process_forward=effective.process_forward,
            out_format=out_format,
            max_quality=min(100, max(1, effective.max_quality)),
            lossless=effective.lossless,
            webp_method=min(6, max(0, effective.webp_method)),
            keep_only_if_smaller=effective.keep_only_if_smaller,
            max_dimension=max(0, effective.max_dimension),
            animated_policy=animated_policy,
            max_frames=max(0, effective.max_frames),
            single_pass_only=effective.single_pass_only,
            estimation_target=int(size_threshold * target_ratio),
            quality_floor=min(100, max(1, effective.quality_floor)),
            quality_search_iterations=max(1, effective.quality_search_iterations),
            downscale_iterations=max(0, effective.downscale_iterations),
        )

    # ------------------------------------------------------------------ #
    # Hook：入站图片重压缩
    # ------------------------------------------------------------------ #
    @HookHandler(
        "chat.receive.before_process",
        name="recompress_inbound_images",
        description="入站消息处理前，将图片组件重压缩为目标格式（默认 WebP）。",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=30000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def recompress_inbound(self, message: Any = None, **kwargs: Any) -> dict[str, Any]:
        """重压缩入站消息中的图片组件，有改动时通过 modified_kwargs 写回。"""
        del kwargs
        settings = self._settings
        if not self.config.plugin.enabled or settings is None or not isinstance(message, dict):
            return {"action": "continue"}
        raw_message = message.get("raw_message")
        if not isinstance(raw_message, list):
            return {"action": "continue"}

        # 第一步：收集需要压缩的图片组件（解码 + 触发规则筛选）
        candidates: list[tuple[dict[str, Any], bytes]] = []
        total_images = 0
        for component in _iter_image_components(raw_message, settings.process_forward):
            total_images += 1
            binary_base64 = component.get("binary_data_base64")
            if not isinstance(binary_base64, str) or not binary_base64:
                self._log_verbose("跳过图片：无二进制数据")
                continue
            try:
                image_bytes = base64.b64decode(binary_base64)
            except Exception:
                self._log_verbose("跳过图片：base64 解码失败")
                continue
            if len(image_bytes) < settings.min_source_size:
                self._log_verbose(f"跳过图片：{len(image_bytes)} 字节小于处理下限")
                continue
            if settings.mode == "oversized_only" and len(image_bytes) <= settings.size_threshold:
                self._log_verbose(f"跳过图片：{len(image_bytes)} 字节未超过阈值")
                continue
            candidates.append((component, image_bytes))

        if not candidates:
            return {"action": "continue"}

        # 第二步：线程池并行压缩，信号量限制同时在编码的图片数
        semaphore = asyncio.Semaphore(self._max_parallel_images)

        async def compress_one(image_bytes: bytes) -> RecompressResult:
            async with semaphore:
                return await asyncio.to_thread(_recompress_blocking, image_bytes, settings)

        results = await asyncio.gather(*(compress_one(image_bytes) for _, image_bytes in candidates))

        # 第三步：把更小的结果写回组件；hash 必须由插件自己更新，
        # Host 反序列化时只在 hash 为空时才重算 sha256
        changed_count = 0
        original_total = 0
        compressed_total = 0
        for (component, image_bytes), result in zip(candidates, results):
            if result.new_bytes is None:
                self._log_verbose(f"跳过图片({result.src_format or '未知'}): {result.skipped_reason}")
                continue
            if settings.keep_only_if_smaller and len(result.new_bytes) >= len(image_bytes):
                self._log_verbose(
                    f"保留原图({result.src_format}): 压缩后 {len(result.new_bytes)} 字节未小于原图 {len(image_bytes)} 字节"
                )
                continue
            component["binary_data_base64"] = base64.b64encode(result.new_bytes).decode("ascii")
            component["hash"] = hashlib.sha256(result.new_bytes).hexdigest()
            changed_count += 1
            original_total += len(image_bytes)
            compressed_total += len(result.new_bytes)
            quality_str = f", 质量={result.final_quality}" if result.final_quality is not None else ""
            dim_str = (
                f", 尺寸={result.final_width}x{result.final_height}"
                if result.final_width and result.final_height
                else ""
            )
            self._log_verbose(
                f"已压缩({result.src_format}{'动图' if result.was_animated else ''} -> {settings.out_format}): "
                f"{result.orig_size / 1024:.1f}KB -> {result.new_size / 1024:.1f}KB"
                f"{quality_str}{dim_str}, 耗时 {result.elapsed_ms:.0f}ms"
            )

        if changed_count:
            if self._log_stats:
                self.ctx.logger.info(
                    "图片重压缩：%d/%d 张，%.1fKB -> %.1fKB",
                    changed_count,
                    total_images,
                    original_total / 1024,
                    compressed_total / 1024,
                )
            return {"action": "continue", "modified_kwargs": {"message": message}}
        # 无改动时不返回 modified_kwargs，省去 Host 的反序列化往返
        return {"action": "continue"}

    def _log_verbose(self, text: str) -> None:
        """verbose 开关控制的逐图日志。"""
        if self._verbose:
            self.ctx.logger.info(text)


def create_plugin() -> ImageRecompressPlugin:
    """插件入口。"""
    return ImageRecompressPlugin()
