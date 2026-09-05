"""脱敏核心（第 6.14 课）：所有入库文本统一先过这里再落库。

原则："服务器不该存用户的明文敏感信息"——
- 自动识别：手机号（131****2351）、邮箱（wfy***@163.com）、
  公网 IP（101.33.*.*）、身份证号（前6后4保留）
- 自定义词：.env SENSITIVE_TERMS（姓名/公司/学校等，分号分隔）→ 替换为"已脱敏"
- 内网/本机地址（127.x、10.x、172.16-31.x、192.168.x）不算敏感，保持原样
  （技术文档里的 curl 127.0.0.1 示例不能被打码）

接入点：记忆写入 / 知识库入库 / 文档保存 / 行为事件入库。
"""
import sys
from pathlib import Path

from app.config import settings

# 规则本体在 common/redact.py（与采集器共用，防止两端规则漂移）。
# 服务端以 server/ 为工作目录运行（systemd WorkingDirectory），仓库根不在
# sys.path 上，这里显式加入——部署时 common/ 与 server/ 同级，必然存在。
_REPO_ROOT = str(Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# 兼容原有引用（旧测试/脚本按名字导入这些正则）
from common.redact import redact


def _custom_terms() -> list[str]:
    return [
        t.strip()
        for t in settings.sensitive_terms.replace(",", ";").split(";")
        if t.strip()
    ]


def sanitize(text: str) -> str:
    """对文本做统一脱敏。不可逆，原值只在用户本地保存。

    通用规则（手机/邮箱/IP/身份证/各类 token/键值型密码）走 common.redact，
    自定义敏感词（.env SENSITIVE_TERMS）是服务端专属，在这里追加。
    """
    if not text:
        return text
    t = redact(text)
    for term in _custom_terms():
        t = t.replace(term, "已脱敏")
    return t
