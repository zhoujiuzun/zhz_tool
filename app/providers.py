# -*- coding: utf-8 -*-
"""OCR provider abstractions and built-in implementations.

每个接口类自描述(ID / DISPLAY_NAME / PROBE_URL / FIELDS),
文件末尾的 _REGISTRY 驱动派生:工厂表、探测 URL 表、默认配置、字段查询、已配置判断。
加一个内置接口 = 写一个类 + 加进 _REGISTRY,其余自动生效。
"""
import base64
import json
import time
import hmac
import hashlib
from urllib.parse import urlparse
import requests
from abc import ABC, abstractmethod

from app.fields import Field, field_default, is_configured as _is_cfg, secret_keys


def image_to_base64(image_bytes: bytes) -> str:
    return base64.b64encode(image_bytes).decode()


def _validate_external_url(url: str) -> str:
    """校验自定义接口 URL:只允许 http/https,且必须有主机名。校验失败抛 ValueError。

    本工具是桌面应用,URL 由用户自己在设置里填写(信任输入),连 localhost/局域网
    自部署 OCR 服务是核心用法,故不拦截内网/回环地址——SSRF 威胁模型(攻击者控制
    URL 诱导服务器请求内网)在桌面场景不成立。这里只挡明显的协议/格式错误。
    """
    if not url:
        raise ValueError("自定义接口未配置 URL")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"URL 协议必须是 http/https:{parsed.scheme!r}")
    if not parsed.hostname:
        raise ValueError("URL 缺少主机名")
    return url


class OCRProvider(ABC):
    def __init__(self, config: dict):
        self.config = config
        self.timeout = config.get("_timeout", 30)

    @property
    def name(self) -> str:
        return self.config["name"]

    @abstractmethod
    def recognize(self, image_bytes: bytes) -> str:
        pass

    @classmethod
    def _get_test_image(cls) -> bytes:
        import sys, os
        # 打包态:test_image.png 被 spec 放进 _MEIPASS/app/(datas=('app/test_image.png','app')),
        # 故 frozen 下 base 必须含 app 子目录——否则打包后「测试连通性」找不到测试图。
        if getattr(sys, "frozen", False):
            base = os.path.join(sys._MEIPASS, "app")
        else:
            base = os.path.dirname(__file__)
        path = os.path.join(base, "test_image.png")
        with open(path, "rb") as f:
            return f.read()


# ── Mistral ──────────────────────────────────────────────────────────────────
class MistralOCR(OCRProvider):
    ID = "mistral"
    DISPLAY_NAME = "Mistral OCR 3"
    PROBE_URL = "https://api.mistral.ai"
    FIELDS = [Field("api_key", "API Key", "password", secret=True, required=True)]

    def recognize(self, image_bytes: bytes) -> str:
        b64 = image_to_base64(image_bytes)
        resp = requests.post(
            "https://api.mistral.ai/v1/ocr",
            headers={"Authorization": f"Bearer {self.config['api_key']}"},
            json={"model": "mistral-ocr-latest", "document": {"type": "image_url", "image_url": f"data:image/png;base64,{b64}"}},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        pages = resp.json().get("pages", [])
        return "\n".join(p.get("markdown", "") for p in pages).strip()


# ── Gemini Vision ─────────────────────────────────────────────────────────────
class GeminiOCR(OCRProvider):
    ID = "google"
    DISPLAY_NAME = "Gemini 2.5 Flash"
    PROBE_URL = "https://generativelanguage.googleapis.com"
    FIELDS = [Field("api_key", "API Key", "password", secret=True, required=True)]

    def recognize(self, image_bytes: bytes) -> str:
        b64 = image_to_base64(image_bytes)
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={self.config['api_key']}",
            json={"contents": [{"parts": [
                {"text": "提取图片中所有文字，保持原始排版，只输出文字内容，不要任何解释。"},
                {"inline_data": {"mime_type": "image/png", "data": b64}},
            ]}]},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError, TypeError):
            # Gemini 触发安全过滤/空响应时不含 candidates,给可读错误而非 KeyError
            reason = data.get("promptFeedback", {}).get("blockReason")
            raise RuntimeError(f"Gemini 未返回文本{(':' + reason) if reason else ''}")


# ── Azure Document Intelligence ───────────────────────────────────────────────
class AzureOCR(OCRProvider):
    ID = "azure"
    DISPLAY_NAME = "Azure Document Intelligence"
    PROBE_URL = "https://cognitiveservices.azure.com"
    FIELDS = [
        Field("api_key", "API Key", "password", secret=True, required=True),
        Field("api_endpoint", "Endpoint", "text", required=True,
              placeholder="https://<resource>.cognitiveservices.azure.com"),
    ]

    def recognize(self, image_bytes: bytes) -> str:
        endpoint = self.config.get("api_endpoint", "").rstrip("/")
        headers = {"Ocp-Apim-Subscription-Key": self.config["api_key"], "Content-Type": "image/png"}
        r = requests.post(
            f"{endpoint}/documentintelligence/documentModels/prebuilt-read:analyze?api-version=2024-02-29-preview",
            headers=headers, data=image_bytes, timeout=self.timeout)
        r.raise_for_status()
        op_url = r.headers.get("Operation-Location")
        if not op_url:
            raise RuntimeError("Azure 未返回 Operation-Location,无法轮询结果")
        poll_headers = {"Ocp-Apim-Subscription-Key": self.config["api_key"]}
        # 轮询设总时钟预算(默认 60s),而非「20 次 × 每次最坏 11s = 220s」无界等待。
        # 否则 Azure 慢响应会把 OCR 线程钉死一分多钟,期间其他接口完全没机会回退。
        deadline = time.time() + 60
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("Azure OCR 轮询超时")
            time.sleep(min(1, remaining))
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("Azure OCR 轮询超时")
            # 单次 GET 超时也并入总预算,避免最后一次 GET 又额外阻塞满 10s
            res = requests.get(op_url, headers=poll_headers, timeout=min(10, max(1, remaining)))
            res.raise_for_status()
            result = res.json()
            status = result.get("status")
            if status == "succeeded":
                lines = [line["content"]
                         for page in result.get("analyzeResult", {}).get("pages", [])
                         for line in page.get("lines", [])]
                return "\n".join(lines)
            if status == "failed":
                # 失败立即抛错,不再硬等到超时误导诊断
                err = result.get("error", {})
                raise RuntimeError(f"Azure OCR 失败:{err.get('message', err or '未知错误')}")


# ── Baidu ─────────────────────────────────────────────────────────────────────
class BaiduOCR(OCRProvider):
    ID = "baidu"
    DISPLAY_NAME = "百度 OCR"
    PROBE_URL = "https://aip.baidubce.com"
    FIELDS = [
        Field("api_key", "API Key", "password", secret=True, required=True),
        Field("secret_key", "Secret Key", "password", secret=True, required=True),
        Field("high_accuracy", "使用高精度版本（更准确，更慢，消耗更多配额）", "checkbox"),
    ]

    # 进程级 token 缓存:{api_key: (token, 过期时间戳)}。access_token 有效期约 30 天,
    # 缓存避免每次识别都多打一次鉴权请求。按 api_key 区分不同账号。
    _token_cache: dict = {}

    def _get_token(self) -> str:
        import time as _t
        api_key = self.config["api_key"]
        cached = BaiduOCR._token_cache.get(api_key)
        if cached and cached[1] > _t.time():
            return cached[0]
        r = requests.post("https://aip.baidubce.com/oauth/2.0/token",
                          params={"grant_type": "client_credentials",
                                  "client_id": api_key,
                                  "client_secret": self.config["secret_key"]}, timeout=10)
        r.raise_for_status()
        data = r.json()
        token = data["access_token"]
        # 提前 1 天过期,留足余量;缺 expires_in 时按 7 天兜底
        ttl = int(data.get("expires_in", 7 * 86400)) - 86400
        BaiduOCR._token_cache[api_key] = (token, _t.time() + max(60, ttl))
        return token

    @staticmethod
    def _compress(image_bytes: bytes, limit: int = 3 * 1024 * 1024) -> bytes:
        if len(image_bytes) <= limit:
            return image_bytes
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        scale = 0.9
        while True:
            buf = io.BytesIO()
            img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                       Image.LANCZOS).save(buf, format="JPEG", quality=85)
            if buf.tell() <= limit or scale < 0.1:
                return buf.getvalue()
            scale *= 0.9

    def recognize(self, image_bytes: bytes) -> str:
        token = self._get_token()
        image_bytes = self._compress(image_bytes)
        b64 = image_to_base64(image_bytes)
        endpoint = "accurate_basic" if self.config.get("high_accuracy") else "general_basic"
        r = requests.post(
            f"https://aip.baidubce.com/rest/2.0/ocr/v1/{endpoint}?access_token={token}",
            data={"image": b64},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        if "error_code" in data:
            raise RuntimeError(data.get("error_msg", "Baidu OCR error"))
        return "\n".join(w["words"] for w in data.get("words_result", []))


# ── Tencent ───────────────────────────────────────────────────────────────────
class TencentOCR(OCRProvider):
    ID = "tencent"
    DISPLAY_NAME = "腾讯云 OCR"
    PROBE_URL = "https://ocr.tencentcloudapi.com"
    FIELDS = [
        Field("api_key", "API Key", "password", secret=True, required=True),
        Field("secret_key", "Secret Key", "password", secret=True, required=True),
        Field("high_accuracy", "使用高精度版本（更准确，更慢，消耗更多配额）", "checkbox"),
    ]

    def recognize(self, image_bytes: bytes) -> str:
        import datetime
        b64 = image_to_base64(image_bytes)
        host = "ocr.tencentcloudapi.com"
        service = "ocr"
        action = "GeneralAccurateOCR" if self.config.get("high_accuracy") else "GeneralBasicOCR"
        version = "2018-11-19"
        region = "ap-guangzhou"
        timestamp = int(time.time())
        payload = json.dumps({"ImageBase64": b64})

        date = datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).strftime("%Y-%m-%d")
        credential_scope = f"{date}/{service}/tc3_request"
        hashed_payload = hashlib.sha256(payload.encode()).hexdigest()
        canonical = f"POST\n/\n\ncontent-type:application/json\nhost:{host}\n\ncontent-type;host\n{hashed_payload}"
        string_to_sign = f"TC3-HMAC-SHA256\n{timestamp}\n{credential_scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"

        def sign(key, msg):
            return hmac.new(key, msg.encode(), hashlib.sha256).digest()

        sk = self.config["secret_key"].encode()
        signing_key = sign(sign(sign(b"TC3" + sk, date), service), "tc3_request")
        signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
        auth = (f"TC3-HMAC-SHA256 Credential={self.config['api_key']}/{credential_scope}, "
                f"SignedHeaders=content-type;host, Signature={signature}")

        r = requests.post(f"https://{host}", headers={
            "Content-Type": "application/json",
            "Host": host, "X-TC-Action": action, "X-TC-Version": version,
            "X-TC-Timestamp": str(timestamp), "X-TC-Region": region,
            "Authorization": auth,
        }, data=payload, timeout=self.timeout)
        r.raise_for_status()
        result = r.json().get("Response", {})
        if "Error" in result:
            raise RuntimeError(result["Error"].get("Message", "Tencent OCR error"))
        return "\n".join(t["DetectedText"] for t in result.get("TextDetections", []))


# ── Xunfei ────────────────────────────────────────────────────────────────────
class XunfeiOCR(OCRProvider):
    ID = "xunfei"
    DISPLAY_NAME = "讯飞 OCR"
    PROBE_URL = "https://api.xf-yun.com"
    FIELDS = [
        Field("api_key", "API Key", "password", secret=True, required=True),
        Field("secret_key", "Secret Key", "password", secret=True, required=True),
        Field("app_id", "APPID", "text", required=True),
    ]

    def recognize(self, image_bytes: bytes) -> str:
        import datetime
        app_id = self.config.get("app_id", "")
        api_key = self.config["api_key"]
        api_secret = self.config["secret_key"]
        url = "https://api.xf-yun.com/v1/private/sf8e6aca1"
        host = "api.xf-yun.com"
        path = "/v1/private/sf8e6aca1"

        date = datetime.datetime.now(datetime.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        sig_origin = f"host: {host}\ndate: {date}\nPOST {path} HTTP/1.1"
        signature = base64.b64encode(
            hmac.new(api_secret.encode(), sig_origin.encode(), hashlib.sha256).digest()
        ).decode()
        auth = (f'api_key="{api_key}", algorithm="hmac-sha256", '
                f'headers="host date request-line", signature="{signature}"')
        b64 = image_to_base64(image_bytes)
        body = {
            "header": {"app_id": app_id, "status": 3},
            "parameter": {"sf8e6aca1": {"category": "ch_en_public_cloud",
                                         "result": {"encoding": "utf8", "compress": "raw", "format": "json"}}},
            "payload": {"sf8e6aca1_data_1": {"encoding": "jpg", "image": b64, "status": 3}}
        }
        r = requests.post(url, headers={"Content-Type": "application/json", "host": host,
                                        "date": date, "Authorization": auth},
                          json=body, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        payload_data = data.get("payload", {}).get("result", {}).get("text", "")
        if payload_data:
            decoded = json.loads(base64.b64decode(payload_data).decode())
            return "".join(w.get("content", "") for page in decoded.get("pages", [])
                           for line in page.get("lines", []) for w in line.get("words", []))
        return ""


# ── Custom provider ───────────────────────────────────────────────────────────
class CustomOCR(OCRProvider):
    # 自定义接口无固定 id(每个用户实例各一),元信息仅声明字段供对话框渲染。
    # type=="custom" 时探测用 cfg["url"](见 DispatchEngine.warmup),故无 PROBE_URL。
    FIELDS = [
        Field("url", "API URL", "url", required=True),
        Field("request_template", "请求模板 (JSON)", "multiline",
              placeholder='{"image": "{{image_base64}}"}'),
        Field("response_path", "响应路径", "text", placeholder="data.result.text"),
        Field("api_key", "API Key", "password", secret=True),
    ]

    def recognize(self, image_bytes: bytes) -> str:
        url = _validate_external_url(self.config.get("url", ""))
        b64 = image_to_base64(image_bytes)
        payload_str = self.config.get("request_template", "{}").replace("{{image_base64}}", b64)
        payload = json.loads(payload_str)
        headers = {"Content-Type": "application/json"}
        if self.config.get("api_key"):
            headers["Authorization"] = f"Bearer {self.config['api_key']}"
        r = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        try:
            for key in self.config.get("response_path", "").split("."):
                if key:
                    data = data[key]
        except (KeyError, TypeError) as e:
            raise RuntimeError(f"response_path 解析失败: {e}")
        return str(data).strip()


# ── Registry + 派生 ─────────────────────────────────────────────────────────
# 注册表顺序 = 默认优先级(index+1)。加内置接口:写类 + 加进这个列表,其余全自动。
_REGISTRY = [MistralOCR, GeminiOCR, AzureOCR, BaiduOCR, TencentOCR, XunfeiOCR]

# 工厂表:id -> 类
_BUILTIN = {c.ID: c for c in _REGISTRY}

# 可用性探测域名(供 DispatchEngine.warmup 使用)
OCR_PROBE_URLS = {c.ID: c.PROBE_URL for c in _REGISTRY}


def build_provider(cfg: dict) -> OCRProvider:
    if cfg.get("type") == "custom":
        return CustomOCR(cfg)
    cls = _BUILTIN.get(cfg.get("id"))
    if cls is None:
        raise ValueError(f"Unknown provider: {cfg.get('id')}")
    return cls(cfg)


def default_ocr_providers() -> list:
    """从注册表派生默认配置段。顺序即优先级,字段默认值由各 FIELDS 的 kind 决定。

    注意:为与历史配置逐字一致,复选框类(如 high_accuracy)默认值留空不写入,
    其余字段(api_key/secret_key/app_id/api_endpoint)写空串。
    """
    items = []
    for i, c in enumerate(_REGISTRY):
        item = {"id": c.ID, "name": c.DISPLAY_NAME, "enabled": True,
                "priority": i + 1, "type": "builtin"}
        for f in c.FIELDS:
            if f.kind != "checkbox":
                item[f.key] = field_default(f.kind)
        items.append(item)
    return items


def ocr_fields_for(cfg: dict) -> list:
    """返回某条配置应渲染的字段列表(自定义走 CustomOCR.FIELDS,内置按 id 查)。"""
    if cfg.get("type") == "custom":
        return CustomOCR.FIELDS
    cls = _BUILTIN.get(cfg.get("id"))
    return cls.FIELDS if cls else []


def ocr_is_configured(cfg: dict) -> bool:
    """所有 required 字段非空即视为已配置;自定义接口只要填了 URL 即可。"""
    return _is_cfg(cfg, ocr_fields_for(cfg))


def ocr_secret_keys() -> set:
    """所有 OCR 接口(含自定义)需加密的字段 key。"""
    return secret_keys(*(c.FIELDS for c in _REGISTRY), CustomOCR.FIELDS)
