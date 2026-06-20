# -*- coding: utf-8 -*-
"""引擎注册中心.

实例化 OCR 与翻译两个 DispatchEngine,集中提供派发、预探测、状态重置入口。
托盘与设置窗只与本模块交互,不再 reach 进引擎内部状态。
"""
from app.dispatch import DispatchEngine
from app.providers import build_provider, OCR_PROBE_URLS, ocr_is_configured
from app.translators import (build_translator, TRANSLATION_PROBE_URLS,
                            translation_is_configured, resolve_target_lang)


# ── OCR 引擎:沿用原超时 20s/10s ────────────────────────────────────────────
_ocr_engine = DispatchEngine(
    probe_urls=OCR_PROBE_URLS,
    build_fn=build_provider,
    is_configured=ocr_is_configured,
    first_timeout=20,
    rest_timeout=10,
)

# ── 翻译引擎:超时改短 8s/5s(纯文字,响应快) ─────────────────────────────
_translation_engine = DispatchEngine(
    probe_urls=TRANSLATION_PROBE_URLS,
    build_fn=build_translator,
    is_configured=translation_is_configured,
    first_timeout=8,
    rest_timeout=5,
)


# ── OCR 派发 ────────────────────────────────────────────────────────────────
def run_ocr(image_bytes: bytes, providers_config: list) -> tuple:
    """图片 → (原文, 接口名)。"""
    return _ocr_engine.run(providers_config, lambda p: p.recognize(image_bytes))


# ── 翻译派发 ────────────────────────────────────────────────────────────────
def run_translation(text: str, target_lang: str, providers_config: list) -> tuple:
    """原文 + 目标语言 → (译文, 接口名)。"""
    return _translation_engine.run(providers_config, lambda p: p.translate(text, target_lang))


# ── 跨引擎的预探测 / 重置(供托盘「重置接口状态」与设置窗关闭后调用) ──────────
def warmup_all(config: dict):
    """对 OCR + 翻译两个引擎都做可用性预探测。"""
    _ocr_engine.warmup(config.get("providers", []))
    _translation_engine.warmup(config.get("translators", []))


def warmup_ocr(providers_config: list):
    _ocr_engine.warmup(providers_config)


def warmup_translation(translators_config: list):
    _translation_engine.warmup(translators_config)


def reset_all():
    """重置接口状态:两个引擎都忘掉上次成功接口。"""
    _ocr_engine.reset_preferred()
    _translation_engine.reset_preferred()


# ── 探测状态查询(供设置窗连通状态列实时刷新) ──────────────────────────────
def get_ocr_probe_status() -> dict:
    """{provider_id: "testing"|"reachable"|"unreachable"}。"""
    return _ocr_engine.get_status()


def get_translation_probe_status() -> dict:
    return _translation_engine.get_status()


# 重新导出,方便调用方一处引入
__all__ = [
    "run_ocr", "run_translation", "warmup_all", "warmup_ocr",
    "warmup_translation", "reset_all", "resolve_target_lang",
    "get_ocr_probe_status", "get_translation_probe_status",
]
