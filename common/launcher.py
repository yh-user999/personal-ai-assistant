"""快捷启动器：别名 → 常用软件/网页/搜索模板 的本地注册表（第 14 课）。

存储：JSON 文件（%APPDATA%/PersonalAI/launcher.json，可用环境变量 LAUNCHER_STORE 覆盖）。
桌面本地执行器（秒开）与采集器远程执行器（未来 QQ 远程指挥）跑在同一台
Windows 机器上，共用这一份文件——无需服务器中转，离线可用。

对话接口（由 desktop/local_exec.py 解析）：
  记住 打开B站 = https://www.bilibili.com          → 注册网页
  记住 打开微信 = D:/Program/WeChat/WeChat.exe      → 注册应用（可为白名单外路径：
      经用户对话显式注册 = 用户亲口授权的快捷方式）
  记住 在B站搜索 = https://search.bilibili.com/all?keyword={q} → 注册搜索模板
  记住 用chrome打开B站 = https://...               → 指定浏览器打开
  忘掉B站 / 我的常用                                → 删除 / 列表
  打开B站 / 在B站搜索 ZCode / 用chrome打开B站       → 使用

安全模型：
- app 类目标可指向白名单外路径，但只能经上述对话注册产生；
  未注册目标的 open 仍走脚本扩展名黑名单。
- url/shell 仅接受 http(s) / ms-settings: / shell: 协议，防任意协议处理器注入。
"""
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import quote_plus

# 允许注册的 URI scheme（防"打开"被注册成任意协议处理器）
_ALLOWED_SCHEME_PREFIXES = ("http://", "https://", "ms-settings:", "shell:")
_TEMPLATE_KEY = "{q}"

# 常见浏览器标准安装路径（首次启动自动探测）
_BROWSER_CANDIDATES = {
    "chrome": [
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
        "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    ],
    "edge": [
        "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
        "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    ],
    "firefox": [
        "C:/Program Files/Mozilla Firefox/firefox.exe",
        "C:/Program Files (x86)/Mozilla Firefox/firefox.exe",
    ],
}

# 出厂预置（首次创建存储时写入，之后用户可改可删——删空不会重新注入）
_PRESET_ITEMS = {
    "b站": {"alias": "B站", "url": "https://www.bilibili.com",
            "template": "https://search.bilibili.com/all?keyword={q}"},
    "知乎": {"alias": "知乎", "url": "https://www.zhihu.com",
             "template": "https://www.zhihu.com/search?type=content&q={q}"},
    "github": {"alias": "GitHub", "url": "https://github.com",
               "template": "https://github.com/search?q={q}"},
    "百度": {"alias": "百度", "url": "https://www.baidu.com",
             "template": "https://www.baidu.com/s?wd={q}"},
}


# ── 存储 ──────────────────────────────────────────────────

def store_path() -> Path:
    env = os.environ.get("LAUNCHER_STORE")
    if env:
        return Path(env)
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "PersonalAI" / "launcher.json"


def save(data: dict) -> None:
    p = store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def load() -> dict:
    """读存储；文件不存在时创建并写入预置网页 + 探测到的浏览器。"""
    p = store_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass  # 损坏则重建（预置会回来，用户自注册项丢失——可接受）
    data = {"version": 1, "items": dict(_PRESET_ITEMS), "browsers": detect_browsers()}
    save(data)
    return data


def _key(alias: str) -> str:
    return alias.strip().casefold()


# ── 注册表操作 ────────────────────────────────────────────

def add_item(alias: str, *, url: str = "", app: str = "", shell: str = "",
             template: str = "", browser: str = "") -> tuple[bool, str]:
    """注册/合并一个别名。同一别名可同时有 url 与 template（网页 + 搜索）。"""
    alias = alias.strip()
    if not alias or len(alias) > 50:
        return False, "别名不能为空（且不超过 50 字）"
    key = _key(alias)
    data = load()
    item = data["items"].get(key, {"alias": alias, "use_count": 0})
    item["alias"] = alias

    if url:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        item["url"] = url
    if template:
        if not template.startswith(("http://", "https://")):
            template = "https://" + template
        if _TEMPLATE_KEY not in template:
            return False, "搜索模板需要包含 {q} 占位符，例如 https://www.baidu.com/s?wd={q}"
        item["template"] = template
    if shell:
        if not shell.startswith(_ALLOWED_SCHEME_PREFIXES):
            return False, "仅支持 http(s)://、ms-settings:、shell: 开头的目标"
        item["shell"] = shell
    if app:
        item["app"] = app.replace("\\", "/")  # 允许白名单外路径：显式注册 = 用户授权
    if browser:
        item["browser"] = browser.strip().casefold()

    data["items"][key] = item
    save(data)
    return True, _describe(item)


def remove_item(alias: str) -> tuple[bool, str]:
    data = load()
    key = _key(alias)
    item = data["items"].pop(key, None)
    if item is None:
        # 模糊找一下，提示相近项
        near = find_suggestions(alias)
        hint = f"（你是要忘掉：{'、'.join(near)}？）" if near else ""
        return False, f"没有找到『{alias}』{hint}"
    save(data)
    return True, item["alias"]


def list_items() -> list[dict]:
    """按使用次数降序的全部条目。"""
    items = sorted(load()["items"].values(), key=lambda it: -it.get("use_count", 0))
    return items


def find_item(alias: str, want: str = "open") -> dict | None:
    """按别名找条目。want='open' 需有可打开目标（url/app/shell）；
    want='template' 需有搜索模板。精确命中 → 模糊包含（用得多、别名短者优先）。"""
    if not alias.strip():
        return None
    data = load()
    key = _key(alias)
    exact = data["items"].get(key)
    field = "template" if want == "template" else None
    if exact is not None and (field is None or field in exact):
        return exact
    # 模糊：query 包含别名或别名包含 query
    cands = []
    for it in data["items"].values():
        if field is not None and field not in it:
            continue
        if field is None and not ({"url", "app", "shell"} & set(it)):
            continue
        k = _key(it["alias"])
        if k == key:
            continue
        if k in key or key in k:
            cands.append(it)
    if not cands:
        return None
    cands.sort(key=lambda it: (-it.get("use_count", 0), len(_key(it["alias"]))))
    return cands[0]


def find_suggestions(alias: str, limit: int = 3) -> list[str]:
    """失败时的相近别名提示。"""
    data = load()
    key = _key(alias)
    scored = []
    for it in data["items"].values():
        k = _key(it["alias"])
        overlap = sum(1 for ch in set(key) if ch in k)
        if overlap:
            scored.append((-overlap, len(k), it["alias"]))
    scored.sort()
    return [name for _, _, name in scored[:limit]]


def bump(alias: str) -> None:
    """使用次数 +1（接受显示别名，内部归一化）。"""
    data = load()
    item = data["items"].get(_key(alias))
    if item is not None:
        item["use_count"] = item.get("use_count", 0) + 1
        save(data)


# ── 浏览器与启动 ──────────────────────────────────────────

def detect_browsers() -> dict:
    """探测常见浏览器标准安装路径。"""
    found = {}
    local = os.environ.get("LOCALAPPDATA")
    for name, paths in _BROWSER_CANDIDATES.items():
        cands = list(paths)
        if local and name == "chrome":
            cands.append(str(Path(local) / "Google/Chrome/Application/chrome.exe"))
        for pth in cands:
            if os.path.exists(pth):
                found[name] = pth.replace("\\", "/")
                break
    return found


def expand_template(template: str, query: str) -> str:
    return template.replace(_TEMPLATE_KEY, quote_plus(query))


def launch_item(item: dict, browser: str = "") -> tuple[bool, str]:
    """执行一个条目（app/url/shell；template 需先 expand 不能直开）。"""
    key = _key(item["alias"])
    try:
        if "app" in item:
            os.startfile(item["app"])
        elif "shell" in item:
            os.startfile(item["shell"])
        elif "url" in item:
            _open_url(item["url"], browser or item.get("browser", ""))
        else:
            return False, "该条目没有可打开的目标（搜索模板请说'在{}搜索 关键词'）".format(item["alias"])
    except Exception as e:
        return False, f"打开失败：{e}"
    bump(key)
    return True, f"已打开 {item['alias']}"


def _open_url(url: str, browser: str = "") -> None:
    """browser 为注册的浏览器别名 → 用它开；否则系统默认浏览器。"""
    path = load()["browsers"].get(browser.casefold()) if browser else None
    if path and os.path.exists(path):
        kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW
        subprocess.Popen([path, url], **kwargs)
    else:
        os.startfile(url)


def try_launch(target: str, browser: str = "") -> tuple[bool, bool, str]:
    """open 动作的别名解析入口。返回 (是否命中注册项, 是否成功, 结果文本)。"""
    item = find_item(target, want="open")
    if item is None:
        return False, False, ""
    ok, text = launch_item(item, browser)
    return True, ok, text


def _describe(item: dict) -> str:
    if "app" in item:
        return f"{item['alias']}（应用）→ {item['app']}"
    if "shell" in item:
        return f"{item['alias']}（系统）→ {item['shell']}"
    parts = []
    if "url" in item:
        parts.append(f"网页 → {item['url']}")
    if "template" in item:
        parts.append(f"搜索 → {item['template'].replace(_TEMPLATE_KEY, '关键词')}")
    if item.get("browser"):
        parts.append(f"浏览器 {item['browser']}")
    return f"{item['alias']}：" + "；".join(parts)


def format_list() -> str:
    items = list_items()
    if not items:
        return "还没有注册任何常用项。试试：记住 打开B站 = https://www.bilibili.com"
    lines = ["📋 我的常用（按使用排序）："]
    for it in items:
        kind = "应用" if "app" in it else "系统" if "shell" in it else "网页"
        extra = " / 可搜索" if "template" in it else ""
        n = it.get("use_count", 0)
        lines.append(f"· {it['alias']}（{kind}{extra}，用过 {n} 次）")
    lines.append("（补充：记住 打开X = 网址/程序路径；忘掉X 删除）")
    return "\n".join(lines)
