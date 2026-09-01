"""本地隐私过滤器：事件在离开本机前做敏感信息脱敏。

规则可配置（settings.privacy_filter），默认开启。
过滤字段：detail（窗口标题/页面标题/commit message）+ name（域名/窗口名——
浏览器地址栏里的公网 IP 也会被打码；内网/本机地址保持原样）。

规则本体在 common/redact.py，与服务端 app/services/sanitize.py 共用。
此前两端各维护一份正则，已经漂移出实际漏洞（中文键名"密码：xxx"、
GitHub/AWS/JWT token、中文相邻的手机号全部漏脱敏），故合并为单一来源。
"""
import sys
from pathlib import Path

# 采集器以 collector/ 或仓库根为工作目录运行，显式把仓库根加进 sys.path
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.redact import RE_IP as RE_PUBLIC_IP  # noqa: E402,F401  （兼容旧引用）
from common.redact import redact  # noqa: E402


def sanitize(text: str) -> str:
    """对一段文本做脱敏。空文本原样返回。"""
    return redact(text)


def sanitize_event(event: dict) -> dict:
    """对事件的 detail 与 name 字段脱敏（就地修改并返回）。"""
    if "detail" in event:
        event["detail"] = sanitize(event["detail"])[:200]
    if "name" in event:
        event["name"] = sanitize(event["name"])[:200]
    return event
