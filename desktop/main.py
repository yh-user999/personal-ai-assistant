"""桌面悬浮机器人 · 入口：悬浮球 + 聊天面板。

退出方式：右键机器人 → 退出；或 Ctrl+C（干净退出）。
自启排查：所有异常写入 logs/desktop.log（pythonw 无窗口时也能查）。
"""
import logging
import signal
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 仓库根：common 共享包

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# 黑匣子日志：开机自启（pythonw 无控制台）时排查唯一依据
_LOG_DIR = Path(__file__).resolve().parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_LOG_DIR / "desktop.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("desktop")

# 原生崩溃取证：Python excepthook 抓不到原生层崩溃（Qt/Win32 段错误 =
# 进程无声消失、日志戛然而止）。faulthandler 会把原生栈写进同一黑匣子。
import faulthandler

_fault_log = open(_LOG_DIR / "faulthandler.log", "a", encoding="utf-8", buffering=1)
faulthandler.enable(file=_fault_log)
logger.info("faulthandler 已启用 → logs/faulthandler.log")


def _excepthook(exc_type, exc_value, exc_tb):
    """未捕获异常写入黑匣子（pythonw 下不打印，只能靠文件）。"""
    logger.critical(
        "未捕获异常:\n%s",
        "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
    )
    faulthandler_log_path = _LOG_DIR / "faulthandler.log"
    logger.critical("原生崩溃取证见: %s", faulthandler_log_path)


sys.excepthook = _excepthook

from PySide6.QtCore import QLockFile  # noqa: E402
from floating_ball import FloatingBall  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from tray import TrayIcon  # noqa: E402


def _acquire_single_instance_lock() -> QLockFile | None:
    """单实例锁：重复启动的实例在创建任何窗口前静默退出。

    治两类问题：① 看门狗/任务计划/手动启动叠加 → 多个机器人同时挂桌面
    ② 重复实例启动-退出时窗口一闪而过（先锁后开窗，闪窗消失）。
    进程异常死亡时锁文件由 QLockFile 的 PID 判活自动视为陈旧，不阻塞重启。
    """
    lock = QLockFile(str(Path(tempfile.gettempdir()) / "paa-robot.lock"))
    lock.setStaleLockTime(60_000)  # 60 秒无心跳视为陈旧（进程崩溃后锁自动失效）
    if not lock.tryLock(100):
        logger.info("已有机器人实例在运行（单实例锁被持有），本实例静默退出")
        return None
    logger.info("单实例锁已获取")
    return lock


def main() -> None:
    # 单实例锁必须先于 QApplication/任何窗口：重复实例零闪烁退出
    lock = _acquire_single_instance_lock()
    if lock is None:
        return
    logger.info("机器人启动中…")
    app = QApplication(sys.argv)
    app.setApplicationName("Personal AI Assistant")
    app.setQuitOnLastWindowClosed(False)  # 面板关闭不退出，托盘常驻
    # Ctrl+C 干净退出（Windows 控制台场景）：把 SIGINT 转成 Qt 退出
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    ball = FloatingBall()
    ball.show()
    tray = TrayIcon(ball)
    tray.show()
    ball._tray = tray  # 保持引用防止被回收
    logger.info("机器人已就绪")
    sys.exit(app.exec())


if __name__ == "__main__":
    # --robot 标记：采集器与机器人命令行同为 pythonw main.py，
    # 唯一参数让看门狗判活查询可区分两者（否则会误判/双拉）
    if "--robot" not in sys.argv:
        sys.argv.append("--robot")
    main()
