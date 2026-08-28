"""机器人守护进程（pythonw 运行，零控制台窗口）。

职责：常驻监视桌面机器人，崩溃后秒级拉起（取代每分钟触发一次的
powershell 看门狗——那个方案有两个问题：powershell 控制台每 60s
闪一次、崩溃后有最长 60s 空窗）。

拉起策略：指数退避（3s→6s→…→60s 封顶）；机器人健康运行 ≥60s 后
再崩溃视为偶发，退避重置为 3s。每次拉起/退出都记录 uptime，
配合 desktop/logs/faulthandler.log 做崩溃取证。
"""
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

DESKTOP_DIR = Path(__file__).resolve().parents[1] / "desktop"
LOG_DIR = DESKTOP_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [supervisor] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "supervisor.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("supervisor")

BACKOFF_INIT = 3        # 首次重启等待秒数
BACKOFF_MAX = 60        # 退避封顶
HEALTHY_UPTIME = 60     # 运行超过该秒数视为健康，退避重置


def find_pythonw() -> str:
    """pythonw.exe（无控制台）与当前解释器同目录。"""
    exe = Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    if cand.exists():
        return str(cand)
    return str(exe)


def main() -> None:
    pythonw = find_pythonw()
    logger.info("守护进程启动：pythonw=%s 工作目录=%s", pythonw, DESKTOP_DIR)
    backoff = BACKOFF_INIT
    restarts = 0
    while True:
        t0 = time.time()
        try:
            kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW
            proc = subprocess.Popen(
                [pythonw, "main.py", "--robot"],
                cwd=str(DESKTOP_DIR),
                **kwargs,
            )
            proc.wait()
        except Exception as e:
            logger.error("拉起失败：%s", e)
        uptime = time.time() - t0
        restarts += 1
        if uptime >= HEALTHY_UPTIME:
            backoff = BACKOFF_INIT  # 健康运行后退出：视为正常/偶发，快速拉起
        logger.warning(
            "机器人退出（uptime %.1fs，累计重启 %d 次，%ds 后拉起）",
            uptime, restarts, backoff,
        )
        time.sleep(backoff)
        backoff = min(backoff * 2, BACKOFF_MAX)


if __name__ == "__main__":
    main()
