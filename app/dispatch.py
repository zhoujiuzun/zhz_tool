# -*- coding: utf-8 -*-
"""通用派发引擎.

封装一套对「多接口」的派发策略,供 OCR 与翻译共用:
  - 优先级排序(priority 升序)
  - 失败回退(当前接口抛错则试下一个)
  - 可用性预探测(warmup:并行 HEAD 探测各接口可达性)
  - 首选记忆(记住上次成功的接口,下次提到最前;重置即忘)
  - 分级超时(首个接口给较长超时,其余较短)

状态全部为实例属性,不使用模块级全局变量。OCR / 翻译各持有一个实例。
"""
import threading
import concurrent.futures
import re
import requests


# 错误信息脱敏:requests 的 HTTPError 文本含完整 URL,Google/Gemini 把 key 放在 ?key=... 查询串里,
# 直接展示到 UI/状态栏会泄露密钥(可能被截图/转写进别的日志)。返回给用户前先打码。
# 与 main.py 的崩溃日志脱敏同源。
_SCRUB = [
    (re.compile(r'((?:access_token|api_key|secret_key|token|key|password|sign|signature)'
                r'["\']?\s*[=:]\s*["\']?)([^\s"\'&,}]+)', re.I), r'\1<redacted>'),
    (re.compile(r'(Authorization["\']?\s*[=:]\s*["\']?)([^"\'},\n]+)', re.I), r'\1<redacted>'),
]


def _scrub(text: str) -> str:
    for pat, repl in _SCRUB:
        text = pat.sub(repl, text)
    return text


def humanize_error(e: Exception) -> str:
    """把底层异常翻译成用户能看懂的中文短句。

    覆盖最常见的网络故障(超时/连不上/DNS/HTTP 状态码)。
    业务层抛的 RuntimeError(已是可读中文)原样返回。
    """
    if isinstance(e, requests.exceptions.Timeout):
        return "请求超时,可能网络较慢或接口无响应,请重试或换个接口"
    if isinstance(e, requests.exceptions.SSLError):
        return "安全连接(SSL)失败,请检查网络或系统时间"
    if isinstance(e, requests.exceptions.ConnectionError):
        return "无法连接到接口,请检查网络连接或接口地址是否正确"
    if isinstance(e, requests.exceptions.HTTPError):
        resp = getattr(e, "response", None)
        code = getattr(resp, "status_code", None)
        mapping = {
            400: "请求格式有误(400)",
            401: "鉴权失败(401),请检查 API Key / 密钥是否正确",
            403: "无访问权限(403),请检查密钥权限或配额",
            404: "接口地址不存在(404),请检查 URL",
            429: "请求过于频繁或配额已用尽(429),请稍后再试",
        }
        if code in mapping:
            return mapping[code]
        if code and 500 <= code < 600:
            return f"接口服务器错误({code}),请稍后再试"
        return f"接口返回错误{f'({code})' if code else ''}"
    msg = str(e).strip()
    return _scrub(msg) if msg else e.__class__.__name__


def _default_is_configured(p: dict) -> bool:
    """默认「已配置」判定:有 api_key,或自定义类型。"""
    return bool(p.get("api_key")) or p.get("type") == "custom"


class DispatchEngine:
    def __init__(
        self,
        probe_urls: dict,
        build_fn,
        is_configured=None,
        first_timeout: int = 20,
        rest_timeout: int = 10,
        probe_timeout: int = 2,
    ):
        self._probe_urls = probe_urls          # {provider_id: probe_url}
        self._build = build_fn                  # cfg -> provider 实例
        self._is_configured = is_configured or _default_is_configured
        self._first_timeout = first_timeout
        self._rest_timeout = rest_timeout
        self._probe_timeout = probe_timeout

        self._status: dict = {}                 # {provider_id: "testing"|"reachable"|"unreachable"}
        self._lock = threading.Lock()
        self._ready = threading.Event()         # warmup 是否已完成
        self._preferred_id = None               # 上次成功的接口 id

    # ── 探测 ──────────────────────────────────────────────────────────────
    def _reachable(self, url: str) -> bool:
        try:
            requests.head(url, timeout=self._probe_timeout)
            return True
        except Exception:
            return False

    def warmup(self, providers_config: list):
        """并行探测所有「已启用且已配置」接口的可达性。可重复调用(重探测)。"""
        candidates = [
            p for p in providers_config
            if p.get("enabled", True) and self._is_configured(p)
        ]
        # 先全部标记为 testing,未入选的接口从状态表移除(UI 显示「—」)。
        # clear() 与 status 重置放进同一把锁,避免并发 run() 读到旧 ready 配新 status。
        with self._lock:
            self._status = {p.get("id", ""): "testing" for p in candidates}
            self._ready.clear()

        def probe_one(cfg):
            pid = cfg.get("id", "")
            url = self._probe_urls.get(pid) or cfg.get("url", "")
            ok = bool(url) and self._reachable(url)
            with self._lock:
                self._status[pid] = "reachable" if ok else "unreachable"

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(candidates) or 1) as ex:
            list(ex.map(probe_one, candidates))

        self._ready.set()

    def get_status(self) -> dict:
        """返回各接口探测状态的快照。"""
        with self._lock:
            return dict(self._status)

    @property
    def _available_ids(self) -> set:
        """探测确认可达的接口 id 集合(由状态表派生)。"""
        with self._lock:
            return {pid for pid, st in self._status.items() if st == "reachable"}

    # ── 状态重置 ──────────────────────────────────────────────────────────
    def reset_preferred(self):
        """忘记上次成功的接口。"""
        with self._lock:
            self._preferred_id = None

    # ── 派发 ──────────────────────────────────────────────────────────────
    def run(self, providers_config: list, invoke) -> tuple:
        """按策略依次尝试接口,第一个成功的返回 (result, provider_name)。

        invoke: callable(provider) -> result,由调用方提供具体调用方式
                (OCR 传 lambda p: p.recognize(img),翻译传 lambda p: p.translate(...))。
        全部失败抛 RuntimeError,聚合各接口错误信息。
        """
        # 等 warmup 完成(最多 3s);若 warmup 从未启动(status 为空),不白等直接继续
        with self._lock:
            warmup_started = bool(self._status)
        if warmup_started:
            self._ready.wait(timeout=3)

        enabled = [
            p for p in providers_config
            if p.get("enabled", True) and self._is_configured(p)
        ]
        enabled.sort(key=lambda p: p.get("priority", 99))

        # 没有任何「已启用且已配置」的接口:给明确指引,而不是抛空的「所有接口均失败」。
        # 这是新用户零配置时的首次体验,必须告诉他去哪做什么。
        if not enabled:
            raise RuntimeError("没有可用的接口。请在「设置」中启用接口并填写 API Key/密钥。")

        # 首选记忆:把上次成功的接口提到最前
        with self._lock:
            preferred_id = self._preferred_id
        if preferred_id:
            preferred = next((p for p in enabled if p.get("id") == preferred_id), None)
            if preferred:
                enabled = [preferred] + [p for p in enabled if p.get("id") != preferred_id]

        # 可用性分层:warmup 完成且有结果时,把探测可达的接口排到前面、
        # 不可达的排到后面——而不是直接剔除。
        # 探测快照只在启动/设置关闭/手动重置时刷新,网络环境变化(如从国内切到
        # 国外)后会过期。若此处硬性剔除「不可达」接口,过期快照会把实际可用的
        # 接口永久排除,导致全部失败。改成分层后,这些接口仍作为兜底被尝试:
        # 常态下可达接口排前优先尝试(保留速度优化),可达接口全失败时再回退到
        # 「不可达」层,不会永久放弃。custom 视为始终可达,排在前列。
        available = self._available_ids
        if self._ready.is_set() and available:
            reachable = [p for p in enabled
                         if p.get("id") in available or p.get("type") == "custom"]
            reachable_ids = {id(p) for p in reachable}
            unreachable = [p for p in enabled if id(p) not in reachable_ids]
            enabled = reachable + unreachable

        # 分级超时 + 失败回退
        errors = []
        for i, cfg in enumerate(enabled):
            cfg_with_timeout = dict(cfg)
            cfg_with_timeout["_timeout"] = self._first_timeout if i == 0 else self._rest_timeout
            name = cfg.get("name") or cfg.get("id") or "未知接口"
            try:
                provider = self._build(cfg_with_timeout)
                result = invoke(provider)
                with self._lock:
                    self._preferred_id = cfg.get("id")
                return result, name
            except Exception as e:
                errors.append(f"{name}:{humanize_error(e)}")

        raise RuntimeError("所有接口均失败:\n" + "\n".join(errors))
