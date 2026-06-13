"""离线冒烟测试：不依赖 MaiBot Host，验证插件可导入与压缩纯函数行为。

运行方式（在仓库根目录）：
    PYTHONPATH=../maibot-plugin-sdk python tests/smoke_test.py
"""

from __future__ import annotations

import random
import sys
import tomllib
from io import BytesIO
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from PIL import Image  # noqa: E402

import plugin as recompress_plugin  # noqa: E402


def _make_settings(**overrides) -> recompress_plugin.RecompressSettings:
    """构建测试用压缩参数快照，默认值与配置模型一致。"""
    base = dict(
        mode="always",
        size_threshold=1024 * 1024,
        min_source_size=0,
        skip_source_formats=(),
        skip_if_already_target=True,
        process_forward=True,
        out_format="webp",
        max_quality=80,
        lossless=False,
        webp_method=4,
        keep_only_if_smaller=True,
        max_dimension=2048,
        animated_policy="animated_webp",
        max_frames=512,
        single_pass_only=True,
        estimation_target=int(1024 * 1024 * 0.9),
        quality_floor=10,
        quality_search_iterations=6,
        downscale_iterations=8,
    )
    base.update(overrides)
    return recompress_plugin.RecompressSettings(**base)


def _make_image_bytes(fmt: str, size: tuple[int, int], mode: str = "RGB", noisy: bool = False) -> bytes:
    """生成测试图片字节。noisy=True 时使用随机噪声（难以压缩）。

    RGBA 使用半透明填充：全不透明的 alpha 平面会被 WebP 编码器优化掉，无法验证透明保留。
    """
    image = Image.new(mode, size, (200, 30, 30, 128) if mode == "RGBA" else (200, 30, 30))
    if noisy:
        random.seed(7)
        channels = len(mode)
        pixels = [
            tuple(random.randint(0, 255) for _ in range(channels))
            for _ in range(size[0] * size[1])
        ]
        image.putdata(pixels)
    buffer = BytesIO()
    image.save(buffer, format=fmt.upper())
    return buffer.getvalue()


def _make_gif_bytes(frame_count: int, size: tuple[int, int] = (64, 64), duration: int = 80) -> bytes:
    """生成多帧 GIF 测试字节。"""
    frames = []
    for index in range(frame_count):
        frame = Image.new("RGB", size, ((index * 40) % 256, 80, 160))
        frames.append(frame)
    buffer = BytesIO()
    frames[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=duration,
        loop=0,
    )
    return buffer.getvalue()


def _is_webp(data: bytes) -> bool:
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def test_plugin_importable() -> None:
    instance = recompress_plugin.create_plugin()
    assert instance is not None
    default_config = type(instance).build_default_config()
    assert default_config["plugin"]["enabled"] is True
    assert default_config["trigger"]["mode"] == "always"
    assert default_config["trigger"]["size_threshold_mb"] == 1.0
    assert default_config["trigger"]["min_source_size_kb"] == 4.0
    assert default_config["output"]["format"] == "webp"
    assert default_config["output"]["max_quality"] == 80
    assert "quality" not in default_config["output"]
    assert default_config["animated"]["policy"] == "animated_webp"
    assert default_config["animated"]["max_frames"] == 512
    assert default_config["output"]["max_dimension"] == 2048
    assert default_config["advanced"]["quality_floor"] == 10
    assert default_config["advanced"]["single_pass_only"] is True
    assert default_config["advanced"]["target_ratio"] == 0.9
    assert "target_size" not in default_config["advanced"]
    assert default_config["performance"]["max_parallel_images"] == 4

    # config.toml 与配置模型字段一致（允许 config.toml 省略字段，不允许多出字段）
    config_data = tomllib.loads((PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
    for section, fields in config_data.items():
        assert section in default_config, f"config.toml 中存在未知配置节：{section}"
        for field in fields:
            assert field in default_config[section], f"config.toml 中存在未知字段：{section}.{field}"
    # 默认值一致性
    for section, fields in config_data.items():
        for field, value in fields.items():
            assert default_config[section][field] == value, f"config.toml 默认值不一致：{section}.{field}"
    print("ok: plugin importable, config model consistent")


def test_static_png_to_webp() -> None:
    data = _make_image_bytes("png", (256, 256), noisy=True)
    result = recompress_plugin._recompress_blocking(data, _make_settings())
    assert result.new_bytes is not None, result.skipped_reason
    assert _is_webp(result.new_bytes)
    assert len(result.new_bytes) < len(data)
    assert result.src_format == "png"
    assert not result.was_animated
    print(f"ok: static png -> webp ({len(data)} -> {len(result.new_bytes)} bytes)")


def test_rgba_alpha_preserved() -> None:
    data = _make_image_bytes("png", (64, 64), mode="RGBA")
    result = recompress_plugin._recompress_blocking(data, _make_settings(keep_only_if_smaller=False))
    assert result.new_bytes is not None, result.skipped_reason
    with Image.open(BytesIO(result.new_bytes)) as image:
        assert image.format == "WEBP"
        assert "A" in image.mode, f"alpha 通道丢失：mode={image.mode}"
    print("ok: rgba alpha preserved in webp")


def test_jpeg_output_flattens_alpha() -> None:
    data = _make_image_bytes("png", (64, 64), mode="RGBA")
    result = recompress_plugin._recompress_blocking(
        data, _make_settings(out_format="jpeg", animated_policy="skip", keep_only_if_smaller=False)
    )
    assert result.new_bytes is not None, result.skipped_reason
    with Image.open(BytesIO(result.new_bytes)) as image:
        assert image.format == "JPEG"
        assert image.mode == "RGB"
    print("ok: jpeg output flattens alpha")


def test_skip_if_already_target() -> None:
    data = _make_image_bytes("webp", (64, 64))
    result = recompress_plugin._recompress_blocking(data, _make_settings())
    assert result.new_bytes is None
    assert "已是目标格式" in result.skipped_reason
    print("ok: already-target webp skipped")


def test_skip_source_formats() -> None:
    data = _make_image_bytes("png", (64, 64))
    result = recompress_plugin._recompress_blocking(data, _make_settings(skip_source_formats=("png",)))
    assert result.new_bytes is None
    assert "跳过列表" in result.skipped_reason
    print("ok: skip_source_formats respected")


def test_animated_gif_to_animated_webp() -> None:
    data = _make_gif_bytes(5, duration=80)
    result = recompress_plugin._recompress_blocking(data, _make_settings(keep_only_if_smaller=False))
    assert result.new_bytes is not None, result.skipped_reason
    assert result.was_animated
    with Image.open(BytesIO(result.new_bytes)) as image:
        assert image.format == "WEBP"
        assert getattr(image, "is_animated", False)
        assert image.n_frames == 5
        durations = []
        for index in range(image.n_frames):
            image.seek(index)
            image.load()  # WebP 的帧 duration 在 load 后才写入 info
            durations.append(int(image.info.get("duration", 0)))
        assert all(60 <= value <= 120 for value in durations), f"帧时长偏差过大：{durations}"
    print(f"ok: 5-frame gif -> animated webp (durations={durations})")


def test_animated_first_frame() -> None:
    data = _make_gif_bytes(5)
    result = recompress_plugin._recompress_blocking(
        data, _make_settings(animated_policy="first_frame", keep_only_if_smaller=False)
    )
    assert result.new_bytes is not None, result.skipped_reason
    with Image.open(BytesIO(result.new_bytes)) as image:
        assert image.format == "WEBP"
        assert not getattr(image, "is_animated", False)
    print("ok: animated first_frame -> static webp")


def test_animated_skip() -> None:
    data = _make_gif_bytes(5)
    result = recompress_plugin._recompress_blocking(data, _make_settings(animated_policy="skip"))
    assert result.new_bytes is None
    assert "动图按配置跳过" in result.skipped_reason
    print("ok: animated skip policy")


def test_max_frames_cap() -> None:
    data = _make_gif_bytes(6)
    result = recompress_plugin._recompress_blocking(data, _make_settings(max_frames=5))
    assert result.new_bytes is None
    assert "超过上限" in result.skipped_reason
    print("ok: max_frames cap")


def test_max_frames_unlimited() -> None:
    # max_frames=0 表示不限制帧数
    data = _make_gif_bytes(6)
    result = recompress_plugin._recompress_blocking(data, _make_settings(max_frames=0, keep_only_if_smaller=False))
    assert result.new_bytes is not None, result.skipped_reason
    with Image.open(BytesIO(result.new_bytes)) as image:
        assert image.n_frames == 6
    print("ok: max_frames=0 unlimited")


def test_max_dimension_predownscale() -> None:
    data = _make_image_bytes("png", (300, 100))
    result = recompress_plugin._recompress_blocking(
        data, _make_settings(max_dimension=150, keep_only_if_smaller=False)
    )
    assert result.new_bytes is not None, result.skipped_reason
    with Image.open(BytesIO(result.new_bytes)) as image:
        assert max(image.size) <= 150, f"未按 max_dimension 预缩放：{image.size}"
        assert image.size == (150, 50), f"未保持纵横比：{image.size}"
    print("ok: max_dimension pre-downscale keeps aspect")


def test_corrupt_bytes() -> None:
    result = recompress_plugin._recompress_blocking(b"not an image at all", _make_settings())
    assert result.new_bytes is None
    assert result.skipped_reason
    print("ok: corrupt bytes -> skipped, no exception")


def test_iter_image_components_forward() -> None:
    image_a = {"type": "image", "data": "a", "hash": "", "binary_data_base64": ""}
    image_b = {"type": "image", "data": "b", "hash": "", "binary_data_base64": ""}
    image_c = {"type": "image", "data": "c", "hash": "", "binary_data_base64": ""}
    raw_message = [
        {"type": "text", "data": "hello"},
        image_a,
        {
            "type": "forward",
            "data": [
                {
                    "message_id": "1",
                    "content": [
                        image_b,
                        # 转发嵌套转发
                        {"type": "forward", "data": [{"message_id": "2", "content": [image_c]}]},
                    ],
                }
            ],
        },
    ]
    found = list(recompress_plugin._iter_image_components(raw_message, include_forward=True))
    assert found == [image_a, image_b, image_c]
    top_only = list(recompress_plugin._iter_image_components(raw_message, include_forward=False))
    assert top_only == [image_a]
    print("ok: forward recursion & include_forward=False")


def test_single_pass_estimation() -> None:
    # 单次编码模式：估算出更低的质量后一次编码，结果应明显小于基准质量编码
    # （纯噪声图是质量模型的最差情况，不对目标值做严格断言，只验证机制方向正确）
    data = _make_image_bytes("png", (512, 512), noisy=True)
    target = 20 * 1024
    settings = _make_settings(
        size_threshold=target, estimation_target=int(target * 0.9), single_pass_only=True
    )
    result = recompress_plugin._recompress_blocking(data, settings)
    assert result.new_bytes is not None, result.skipped_reason
    baseline = recompress_plugin._recompress_blocking(
        data, _make_settings(size_threshold=0, estimation_target=0, keep_only_if_smaller=False)
    )
    assert len(result.new_bytes) < len(baseline.new_bytes), "估算质量未低于基准质量编码"
    print(
        f"ok: single-pass estimation ({len(data)} -> {len(result.new_bytes)} bytes, "
        f"baseline {len(baseline.new_bytes)}, target {target})"
    )


def test_estimate_quality_and_scale() -> None:
    # 大噪声图 + 小目标：估算质量应低于最高质量且不低于下限
    data = _make_image_bytes("png", (512, 512), noisy=True)
    settings = _make_settings(estimation_target=20 * 1024)
    with Image.open(BytesIO(data)) as image:
        prepared = recompress_plugin._prepare_static_frame(image, "webp")
        quality, scale = recompress_plugin._estimate_quality_and_scale(prepared, settings)
    assert settings.quality_floor <= quality < settings.max_quality, f"估算质量异常：{quality}"
    assert 0.1 <= scale <= 1.0
    print(f"ok: quality estimation (q={quality}, scale={scale:.2f})")


def test_lossless_downscales_when_oversized() -> None:
    # 无损模式没有质量可调：超过阈值时只能缩小像素尺寸
    data = _make_image_bytes("png", (512, 512), noisy=True)
    target = 20 * 1024
    settings = _make_settings(
        size_threshold=target,
        estimation_target=int(target * 0.9),
        lossless=True,
        single_pass_only=True,
        keep_only_if_smaller=False,
    )
    result = recompress_plugin._recompress_blocking(data, settings)
    assert result.new_bytes is not None, result.skipped_reason
    with Image.open(BytesIO(result.new_bytes)) as image:
        assert image.format == "WEBP"
        assert max(image.size) < 512, f"无损超标未缩小像素尺寸：{image.size}"
    print(f"ok: lossless oversized -> downscaled to {image.size}")


def test_target_size_loop() -> None:
    # 循环模式：严格压到 size_threshold 以内（不乘 target_ratio）
    data = _make_image_bytes("png", (512, 512), noisy=True)
    target = 20 * 1024
    result = recompress_plugin._recompress_blocking(
        data, _make_settings(size_threshold=target, single_pass_only=False)
    )
    assert result.new_bytes is not None, result.skipped_reason
    assert len(result.new_bytes) <= target, f"未压到目标大小：{len(result.new_bytes)} > {target}"
    print(f"ok: target_size loop ({len(data)} -> {len(result.new_bytes)} bytes, target {target})")


def main() -> None:
    test_plugin_importable()
    test_static_png_to_webp()
    test_rgba_alpha_preserved()
    test_jpeg_output_flattens_alpha()
    test_skip_if_already_target()
    test_skip_source_formats()
    test_animated_gif_to_animated_webp()
    test_animated_first_frame()
    test_animated_skip()
    test_max_frames_cap()
    test_max_frames_unlimited()
    test_max_dimension_predownscale()
    test_corrupt_bytes()
    test_iter_image_components_forward()
    test_single_pass_estimation()
    test_estimate_quality_and_scale()
    test_lossless_downscales_when_oversized()
    test_target_size_loop()
    print("\n全部冒烟测试通过")


if __name__ == "__main__":
    main()
