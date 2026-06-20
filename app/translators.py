# -*- coding: utf-8 -*-
"""翻译接口抽象与内置实现.

与 OCR 接口是两套独立的接口池。每个接口把「规范语言码」映射成自己的语言码。
规范语言码(canonical):zh / en / ja / ko / fr / de / ru / es。

每个接口类自描述(ID / DISPLAY_NAME / PROBE_URL / FIELDS),文件末尾的 _REGISTRY
驱动派生:工厂表、探测 URL 表、默认配置、字段查询、已配置判断。
加一个内置翻译接口 = 写一个类 + 加进 _REGISTRY,其余自动生效。
"""
import time
import hashlib
import random
import requests
from abc import ABC, abstractmethod

from app.fields import Field, field_default, is_configured as _is_cfg, secret_keys


# 规范语言码 → 人类可读名(用于 UI 下拉与 Gemini prompt)
LANG_NAMES = {
    "zh": "中文", "en": "英文", "ja": "日文", "ko": "韩文",
    "fr": "法文", "de": "德文", "ru": "俄文", "es": "西班牙文",
}

# 各接口的语言码映射:canonical → provider 专属码
_BAIDU_LANG = {"zh": "zh", "en": "en", "ja": "jp", "ko": "kor", "fr": "fra", "de": "de", "ru": "ru", "es": "spa"}
_DEEPL_LANG = {"zh": "ZH", "en": "EN", "ja": "JA", "ko": "KO", "fr": "FR", "de": "DE", "ru": "RU", "es": "ES"}
_GOOGLE_LANG = {"zh": "zh-CN", "en": "en", "ja": "ja", "ko": "ko", "fr": "fr", "de": "de", "ru": "ru", "es": "es"}
_YOUDAO_LANG = {"zh": "zh-CHS", "en": "en", "ja": "ja", "ko": "ko", "fr": "fr", "de": "de", "ru": "ru", "es": "es"}


def _is_chinese(text: str) -> bool:
    """判定文本是否以中文为主(用于默认方向规则)。"""
    han = sum(1 for ch in text if "一" <= ch <= "鿿")
    latin = sum(1 for ch in text if ("a" <= ch.lower() <= "z"))
    return han > latin


def resolve_target_lang(text: str) -> str:
    """默认方向规则:源为中文→翻英文;源非中文→翻中文。返回规范语言码。"""
    return "en" if _is_chinese(text) else "zh"


class TranslationProvider(ABC):
    def __init__(self, config: dict):
        self.config = config
        self.timeout = config.get("_timeout", 8)

    @property
    def name(self) -> str:
        return self.config["name"]

    @abstractmethod
    def translate(self, text: str, target_lang: str) -> str:
        """text + 规范目标语言码 → 译文。"""

    def test_connection(self) -> bool:
        try:
            return bool(self.translate("hello", "zh"))
        except Exception:
            return False


# ── 百度翻译 ────────────────────────────────────────────────────────────────
class BaiduTranslator(TranslationProvider):
    """鉴权:appid + 密钥,MD5 签名 = md5(appid + q + salt + 密钥)。"""
    ID = "baidu_tr"
    DISPLAY_NAME = "百度翻译"
    PROBE_URL = "https://fanyi-api.baidu.com"
    FIELDS = [
        Field("api_key", "密钥", "password", secret=True, required=True),
        Field("app_id", "APPID", "text", required=True),
    ]

    def translate(self, text: str, target_lang: str) -> str:
        appid = self.config.get("app_id", "")
        secret = self.config.get("api_key", "")  # 百度翻译「密钥」存于 api_key 字段
        to_lang = _BAIDU_LANG.get(target_lang, "zh")
        salt = str(random.randint(10000, 99999))
        sign = hashlib.md5(f"{appid}{text}{salt}{secret}".encode("utf-8")).hexdigest()
        r = requests.get(
            "https://fanyi-api.baidu.com/api/trans/vip/translate",
            params={"q": text, "from": "auto", "to": to_lang,
                    "appid": appid, "salt": salt, "sign": sign},
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        if "error_code" in data:
            raise RuntimeError(f"{data.get('error_code')}: {data.get('error_msg', '百度翻译错误')}")
        return "\n".join(item["dst"] for item in data.get("trans_result", []))


# ── DeepL ─────────────────────────────────────────────────────────────────────
class DeepLTranslator(TranslationProvider):
    ID = "deepl"
    DISPLAY_NAME = "DeepL"
    PROBE_URL = "https://api-free.deepl.com"
    FIELDS = [Field("api_key", "API Key", "password", secret=True, required=True)]

    def translate(self, text: str, target_lang: str) -> str:
        key = self.config.get("api_key", "")
        # 免费版 key 以 ":fx" 结尾,走 api-free 域名
        host = "https://api-free.deepl.com" if key.endswith(":fx") else "https://api.deepl.com"
        r = requests.post(
            f"{host}/v2/translate",
            headers={"Authorization": f"DeepL-Auth-Key {key}"},
            data={"text": text, "target_lang": _DEEPL_LANG.get(target_lang, "ZH")},
            timeout=self.timeout,
        )
        r.raise_for_status()
        return "\n".join(t["text"] for t in r.json().get("translations", []))


# ── Google 翻译(Cloud Translation v2) ───────────────────────────────────────
class GoogleTranslator(TranslationProvider):
    ID = "google_tr"
    DISPLAY_NAME = "Google 翻译"
    PROBE_URL = "https://translation.googleapis.com"
    FIELDS = [Field("api_key", "API Key", "password", secret=True, required=True)]

    def translate(self, text: str, target_lang: str) -> str:
        r = requests.post(
            "https://translation.googleapis.com/language/translate/v2",
            params={"key": self.config.get("api_key", "")},
            data={"q": text, "target": _GOOGLE_LANG.get(target_lang, "zh-CN"), "format": "text"},
            timeout=self.timeout,
        )
        r.raise_for_status()
        try:
            items = r.json()["data"]["translations"]
        except (KeyError, TypeError):
            raise RuntimeError("Google 翻译返回结构异常")
        return "\n".join(t["translatedText"] for t in items)


# ── Gemini(复用 OCR 的 key,prompt 方式翻译) ────────────────────────────────
class GeminiTranslator(TranslationProvider):
    ID = "gemini_tr"
    DISPLAY_NAME = "Gemini 翻译"
    PROBE_URL = "https://generativelanguage.googleapis.com"
    FIELDS = [Field("api_key", "API Key", "password", secret=True, required=True)]

    def translate(self, text: str, target_lang: str) -> str:
        target_name = LANG_NAMES.get(target_lang, "中文")
        prompt = f"把下面的文字翻译成{target_name},只输出译文,不要任何解释:\n\n{text}"
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={self.config.get('api_key', '')}",
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError, TypeError):
            reason = data.get("promptFeedback", {}).get("blockReason")
            raise RuntimeError(f"Gemini 翻译未返回文本{(':' + reason) if reason else ''}")


# ── 有道智云 ──────────────────────────────────────────────────────────────────
class YoudaoTranslator(TranslationProvider):
    """鉴权:应用ID(app_id) + 应用密钥(api_key),sha256 签名。"""
    ID = "youdao"
    DISPLAY_NAME = "有道智云"
    PROBE_URL = "https://openapi.youdao.com"
    FIELDS = [
        Field("api_key", "应用密钥", "password", secret=True, required=True),
        Field("app_id", "应用ID", "text", required=True),
    ]

    @staticmethod
    def _truncate(q: str) -> str:
        return q if len(q) <= 20 else q[:10] + str(len(q)) + q[-10:]

    def translate(self, text: str, target_lang: str) -> str:
        app_key = self.config.get("app_id", "")
        app_secret = self.config.get("api_key", "")
        to_lang = _YOUDAO_LANG.get(target_lang, "zh-CHS")
        salt = str(random.randint(10000, 99999))
        curtime = str(int(time.time()))
        sign_str = app_key + self._truncate(text) + salt + curtime + app_secret
        sign = hashlib.sha256(sign_str.encode("utf-8")).hexdigest()
        r = requests.post(
            "https://openapi.youdao.com/api",
            data={"q": text, "from": "auto", "to": to_lang, "appKey": app_key,
                  "salt": salt, "sign": sign, "signType": "v3", "curtime": curtime},
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("errorCode") != "0":
            raise RuntimeError(f"有道错误码 {data.get('errorCode')}")
        return "\n".join(data.get("translation", []))


# ── Registry + 派生 ─────────────────────────────────────────────────────────
# 注册表顺序 = 默认优先级(index+1)。加内置翻译接口:写类 + 加进这个列表,其余全自动。
# 顺序须与历史默认一致:百度→DeepL→有道→Google→Gemini。
_REGISTRY = [BaiduTranslator, DeepLTranslator, YoudaoTranslator,
             GoogleTranslator, GeminiTranslator]

_BUILTIN = {c.ID: c for c in _REGISTRY}

TRANSLATION_PROBE_URLS = {c.ID: c.PROBE_URL for c in _REGISTRY}


def build_translator(cfg: dict) -> TranslationProvider:
    cls = _BUILTIN.get(cfg.get("id"))
    if cls is None:
        raise ValueError(f"Unknown translator: {cfg.get('id')}")
    return cls(cfg)


def default_translators() -> list:
    """从注册表派生默认配置段。顺序即优先级,字段默认值由各 FIELDS 的 kind 决定。"""
    items = []
    for i, c in enumerate(_REGISTRY):
        item = {"id": c.ID, "name": c.DISPLAY_NAME, "enabled": True,
                "priority": i + 1, "type": "builtin"}
        for f in c.FIELDS:
            if f.kind != "checkbox":
                item[f.key] = field_default(f.kind)
        items.append(item)
    return items


def translation_fields_for(cfg: dict) -> list:
    """返回某条翻译配置应渲染的字段列表(翻译接口无自定义类型,全按 id 查)。"""
    cls = _BUILTIN.get(cfg.get("id"))
    return cls.FIELDS if cls else []


def translation_is_configured(cfg: dict) -> bool:
    """所有 required 字段非空即视为已配置。"""
    return _is_cfg(cfg, translation_fields_for(cfg))


def translation_secret_keys() -> set:
    """所有翻译接口需加密的字段 key。"""
    return secret_keys(*(c.FIELDS for c in _REGISTRY))

