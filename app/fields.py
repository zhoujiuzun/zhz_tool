# -*- coding: utf-8 -*-
"""接口字段模型(纯数据,零 Qt 依赖).

每个 OCR / 翻译接口用一组 Field 声明自己需要哪些配置项。
设置窗据此通用渲染表单,config 据此派生加密字段集,派发引擎据此判断「是否已配置」。
后端(providers/translators)只产出 Field,不碰 Qt;UI 层负责把 kind 映射成控件。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    """一个接口配置字段的声明。

    key:         存进 config 的键名(如 "api_key" / "app_id")。
    label:       UI 显示名(如 "API Key" / "密钥" / "APPID")。
    kind:        控件类型,见下表。决定 UI 怎么渲染、config 默认值取什么。
                   password  - 密码框(api_key/secret_key 等)
                   text      - 单行文本(app_id/endpoint 等)
                   url       - 单行文本,保存时校验 http(s)
                   multiline - 多行文本(自定义请求模板),保存时校验 JSON
                   checkbox  - 复选框(布尔开关,如高精度)
    placeholder: 输入框占位提示(可空)。
    secret:      是否需要本地加密存储(只有 True 的字段进 config 的加密集)。
    required:    是否必填。派发引擎用它判断接口「是否已配置」。
    """
    key: str
    label: str
    kind: str = "text"
    placeholder: str = ""
    secret: bool = False
    required: bool = False


def field_default(kind: str):
    """字段在「默认配置」里的初始值:复选框为 False,其余为空串。"""
    return False if kind == "checkbox" else ""


def is_configured(cfg: dict, fields) -> bool:
    """所有 required 字段都非空 → 已配置。

    无 required 字段的接口(理论上不存在)视为已配置。
    复选框类不应标 required(布尔永远「有值」),这里按通用真值判断即可。
    """
    for f in fields:
        if f.required and not cfg.get(f.key):
            return False
    return True


def secret_keys(*field_groups) -> set:
    """从若干组 Field 中收集所有 secret 字段的 key,去重成集合。"""
    keys = set()
    for fields in field_groups:
        for f in fields:
            if f.secret:
                keys.add(f.key)
    return keys
