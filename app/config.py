# -*- coding: utf-8 -*-
import json
import re
import logging
from pathlib import Path
from cryptography.fernet import Fernet

from app.providers import default_ocr_providers, ocr_secret_keys
from app.translators import default_translators, translation_secret_keys

_log = logging.getLogger(__name__)

CONFIG_PATH = Path.home() / ".ocr_tool" / "config.json"
KEY_PATH = Path.home() / ".ocr_tool" / ".key"

# 需加密存储的敏感字段:从 OCR + 翻译两个接口注册表的 secret 字段派生(对两类接口同时生效)。
# 当前派生结果 == {"api_key", "secret_key"},与历史手写值一致,故老的加密 config 照常解密。
# 新增接口若声明了新的 secret 字段,会自动纳入加密,无需改这里。
_SECRET_FIELDS = tuple(sorted(ocr_secret_keys() | translation_secret_keys()))

# 宏名合法字符:中英文、数字、空格、下划线、连字符、点。用于防路径穿越。
_SAFE_MACRO_NAME = re.compile(r"^[\w一-鿿 \-.]+$")


def _safe_macro_path(name: str) -> Path:
    """把宏名解析为 MACROS_DIR 下的文件路径,拒绝任何路径穿越/分隔符/.. 。

    非法名抛 ValueError,而不是静默拼出目录外路径。
    """
    name = (name or "").strip()
    if not name or name in (".", "..") or not _SAFE_MACRO_NAME.match(name):
        raise ValueError(f"非法宏名:{name!r}")
    path = (MACROS_DIR / f"{name}.json").resolve()
    # 二次兜底:解析后必须仍在 MACROS_DIR 内
    if path.parent != MACROS_DIR.resolve():
        raise ValueError(f"非法宏名(越界):{name!r}")
    return path


def _quarantine(path: Path, reason: str) -> None:
    """把损坏文件改名隔离备份(<name>.corrupt-<时间><suffix>),留证据但不挡启动。

    与 load_macro 的处理一致:坏文件不删、改名留痕,然后调用方回退默认值。
    改名失败(如权限)时只记日志,不抛出。
    """
    import time as _t
    try:
        backup = path.with_name(f"{path.name}.corrupt-{int(_t.time())}")
        path.rename(backup)
        _log.error("%s 损坏(%s),已隔离备份到 %s,按默认值处理", path.name, reason, backup.name)
    except OSError as e:
        _log.error("%s 损坏(%s)且无法备份(%s),按默认值处理", path.name, reason, e)


def _atomic_write_json(path: Path, data) -> None:
    """原子写 JSON:先写临时文件 + fsync,再 os.replace 覆盖。

    使「写到一半被强杀/掉电」也不会损坏正式文件(要么旧内容、要么新内容完整),
    与 save_macro 的写法一致。
    """
    import os
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _get_fernet():
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if KEY_PATH.exists():
        try:
            return Fernet(KEY_PATH.read_bytes())
        except (ValueError, OSError) as e:
            # .key 损坏(磁盘坏道/手工改/写一半掉电):隔离备份后重新生成。
            # 代价:旧密文(API Key 等)再也解不开,_decrypt_list 会清空并留痕,
            # 用户需重填密钥——但 app 总能启动,优于卡死在 ValueError。
            _quarantine(KEY_PATH, str(e))
    KEY_PATH.write_bytes(Fernet.generate_key())
    return Fernet(KEY_PATH.read_bytes())


def _decrypt_list(items: list, fernet):
    for item in items:
        for field in _SECRET_FIELDS:
            enc = item.get(f"{field}_enc")
            if enc:
                try:
                    item[field] = fernet.decrypt(enc.encode()).decode()
                except Exception:
                    # 解密失败(换了 .key 或文件损坏):保留空串但务必留痕,
                    # 否则表现为「接口莫名未配置」,极难定位。
                    item[field] = ""
                    _log.warning("解密字段 %s 失败(接口 id=%s),已清空,"
                                 "可能是 .key 变更或配置损坏",
                                 field, item.get("id", "?"))
                del item[f"{field}_enc"]


def _encrypt_list(items: list, fernet):
    for item in items:
        for field in _SECRET_FIELDS:
            val = item.pop(field, "")
            if val:
                item[f"{field}_enc"] = fernet.encrypt(val.encode()).decode()


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return _default_config()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("config.json 顶层不是对象")
    except (json.JSONDecodeError, OSError, ValueError) as e:
        # 文件截断/损坏(写一半被强杀、掉电、磁盘坏道):隔离备份后回退默认,
        # 而不是让 json.load 抛 JSONDecodeError 把 app 卡死在启动。
        _quarantine(CONFIG_PATH, str(e))
        return _default_config()
    fernet = _get_fernet()
    _decrypt_list(data.get("providers", []), fernet)
    _decrypt_list(data.get("translators", []), fernet)
    # 兼容旧配置:补齐缺失的翻译接口默认段
    if "translators" not in data:
        data["translators"] = _default_translators()
    # 兼容旧配置:补齐缺失的宏配置段(宏序列本身存独立文件,这里只存轻量设置)
    if "macro" not in data:
        data["macro"] = _default_macro_settings()
    else:
        # 补齐宏配置里缺失的单个键(如旧配置没有 enabled 开关)
        for k, v in _default_macro_settings().items():
            data["macro"].setdefault(k, v)
    # 兼容旧配置:补主题色 + 功能可见性(逐键补,将来加模块也兼容)
    data.setdefault("theme_color", DEFAULT_THEME_COLOR)
    data.setdefault("window_top_hotkey", "Ctrl+Alt+T")
    data.setdefault("file_search_hotkey", "")
    data.setdefault("file_search_engine", "")   # 文件搜索引擎:""=首次按检测决定;"native"/"everything"=用户已选(见 ADR-0005)
    data.setdefault("keep_helper_alive", False)  # 文件搜索 helper 常驻:false(默认)=搜索窗关闭后退出(零后台);true=继续运行
    fv = data.setdefault("feature_visibility", _default_feature_visibility())
    for k, v in _default_feature_visibility().items():
        fv.setdefault(k, v)
    return data


def save_config(data: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    save_data = json.loads(json.dumps(data))
    fernet = _get_fernet()
    _encrypt_list(save_data.get("providers", []), fernet)
    _encrypt_list(save_data.get("translators", []), fernet)
    _atomic_write_json(CONFIG_PATH, save_data)


def _default_translators() -> list:
    """翻译接口默认段:从 translators 注册表派生(顺序即优先级)。"""
    return default_translators()


# 主题色默认值(与 style.DEFAULT_THEME 保持一致;此处硬编码避免 config 依赖 QtGui)
DEFAULT_THEME_COLOR = "#6FB3EC"


def _default_feature_visibility() -> dict:
    """功能模块可见性默认(全开)。隐藏一个模块=同时隐其托盘菜单项+设置窗对应 Tab。
    红线项(设置/退出菜单、通用 Tab)不在此列,永远显示。见 CONTEXT.md「功能可见性」。"""
    return {
        "ocr": True,         # 截图识别菜单 + OCR 接口 Tab
        "translate": True,   # 翻译菜单 + 翻译接口 Tab
        "macro": True,       # 宏 Tab
        "pin": True,         # 截图贴图菜单
        "window_top": True,  # 窗口置顶全局热键(菜单项已移除,此开关控制热键注册与否)
        "autostart": True,   # 开机自启动菜单
        "reset_engine": True,  # 重置接口状态菜单
        "file_search": True,   # 文件搜索菜单(Everything 式;首次用会装提权计划任务)
    }


def _default_config() -> dict:
    return {
        "clipboard_monitor": False,
        "auto_translate": False,
        "theme_color": DEFAULT_THEME_COLOR,
        "window_top_hotkey": "Ctrl+Alt+T",   # 窗口置顶 toggle 全局热键(可改,留空禁用)
        "file_search_hotkey": "",             # 文件搜索全局热键,默认关闭(空=不注册);可在通用里自定义
        "feature_visibility": _default_feature_visibility(),
        "providers": default_ocr_providers(),
        "translators": _default_translators(),
        "macro": _default_macro_settings(),
    }


# ── 宏(动作序列)──────────────────────────────────────────────────────────────
# 宏序列本身(动作列表,轨迹动辄上千条)存独立文件 ~/.ocr_tool/macros/<name>.json,
# 不混进 config.json。config 里只存这些轻量设置。
MACROS_DIR = Path.home() / ".ocr_tool" / "macros"


def _default_macro_settings() -> dict:
    return {
        "enabled": False,       # 宏总开关:关闭时不注册任何回放热键,不干扰正常操作
        "current": "",          # 当前选中的宏名(录制/编辑目标;对应 macros/<name>.json)
        "stop_record_hotkey": "F9",  # 启停录制热键(全局单键 toggle;键名沿用旧名以兼容老配置)
    }
    # 注:回放热键 + 循环设置已下放到每条宏自己的文件(见 load_macro 的 hotkey/loop_mode/
    # loop_count),不再放全局段——这样每条宏可配各自的回放热键与循环方式,互不干扰。


def list_macros() -> list:
    """返回已保存的宏名列表(按文件名,去掉 .json)。

    排除 `.corrupt-*` 隔离备份(load_macro 遇损坏文件改名留证用),它们不是真宏。
    """
    if not MACROS_DIR.exists():
        return []
    return sorted(p.stem for p in MACROS_DIR.glob("*.json")
                  if ".corrupt-" not in p.name)


def _macro_defaults() -> dict:
    """每条宏文件应有的字段默认值(回放热键 + 循环设置,均每宏独立)。"""
    return {"hotkey": "", "loop_mode": "once", "loop_count": 1}


def load_macro(name: str) -> dict:
    """读一条宏。返回 {name, screen:[w,h], actions:[...], hotkey, loop_mode, loop_count}。

    不存在返回空壳;旧宏文件缺少的 hotkey/loop_* 字段在此补默认(向后兼容)。
    文件损坏(JSON 解析失败,如写盘途中被强杀截断)时:**不崩调用方**,把坏文件
    改名隔离备份(<name>.corrupt-<时间>.json)留证,返回空壳,使 app 仍能启动。
    """
    path = _safe_macro_path(name)
    if not path.exists():
        return {"name": name, "screen": [0, 0], "actions": [], **_macro_defaults()}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        import time as _t
        backup = path.with_suffix(f".corrupt-{int(_t.time())}.json")
        try:
            path.rename(backup)
            _log.error("宏「%s」文件损坏(%s),已隔离备份到 %s,按空宏处理", name, e, backup.name)
        except OSError:
            _log.error("宏「%s」文件损坏(%s)且无法备份,按空宏处理", name, e)
        return {"name": name, "screen": [0, 0], "actions": [], **_macro_defaults()}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("name", name)
    for k, v in _macro_defaults().items():
        data.setdefault(k, v)
    return data


def save_macro(name: str, data: dict):
    """存一条宏到独立文件。**原子写**:先写临时文件再 os.replace 覆盖,

    使「写到一半被强杀/掉电」也不会损坏正式文件(要么旧内容、要么新内容完整)。
    宏轨迹动辄上千点、文件大,这一点尤为重要。
    """
    import os
    MACROS_DIR.mkdir(parents=True, exist_ok=True)
    path = _safe_macro_path(name)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())   # 落盘后再 replace,避免元数据更新而内容未落
    os.replace(tmp, path)      # 同盘原子替换


def delete_macro(name: str):
    path = _safe_macro_path(name)
    if path.exists():
        path.unlink()


def migrate_macro_play_hotkey():
    """一次性迁移:旧 config 的全局 play_hotkey/loop_* → current 宏自己的文件。

    回放热键与循环设置已下放到每条宏。老配置里这些键还在全局段,且 current 宏文件
    尚无 hotkey 时,把旧值搬给 current 宏并存盘,再从 config 删除这些全局键。
    幂等:无旧键 / 已迁过 则什么都不做。
    """
    if not CONFIG_PATH.exists():
        return
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return
    except (json.JSONDecodeError, OSError, ValueError):
        # config 损坏:不在迁移阶段处理(此函数在 load_config 之前跑),
        # 静默跳过,交给随后的 load_config 统一隔离备份+回退默认。
        return
    macro = raw.get("macro", {})
    old_keys = ("play_hotkey", "loop_mode", "loop_count")
    if not any(k in macro for k in old_keys):
        return  # 已迁过或本就是新配置

    current = (macro.get("current") or "").strip()
    if current and current in list_macros():
        m = load_macro(current)
        if not m.get("hotkey"):   # 不覆盖该宏已有的设置
            m["hotkey"] = macro.get("play_hotkey", "") or ""
            m["loop_mode"] = macro.get("loop_mode", "once")
            m["loop_count"] = macro.get("loop_count", 1)
            save_macro(current, m)

    for k in old_keys:
        macro.pop(k, None)
    raw["macro"] = macro
    # 直接回写原始 json(不经 save_config 的加密路径,宏段无敏感字段);原子写防截断
    _atomic_write_json(CONFIG_PATH, raw)
