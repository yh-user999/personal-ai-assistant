"""聊天面板 / 悬浮球的后台 QThread 工作线程（从 chat_panel.py、floating_ball.py 拆出）。

职责统一：所有网络调用都在后台线程跑，完成后 Signal 回 UI 线程，避免阻塞。

生命周期规范（Qt6Core!sizedFree 堆损坏崩溃的防御）：
worker 用完必须走 retire()——wait() 收尸后再 deleteLater()。
原因：QThread 的 C++ 对象在线程真正退出前就被 deleteLater 销毁时，
Qt 内部分配的线程栈/事件结构会被二次释放，崩溃点固定在
QtPrivate::sizedFree（生产 minidump 五份同址实锤）。
"""
from api_client import ApiClient
from PySide6.QtCore import QThread, Signal


def retire(worker: QThread) -> None:
    """规范回收一个 QThread：等线程真正结束，再安全销毁。

    信号槽已断开（调用方在槽内调用时 Qt 自动断开），wait 保证 run()
    完全退出，deleteLater 此时只会释放已静止的对象——堆不再损坏。
    """
    worker.wait(3000)  # 网络调用已在 done.emit 前结束，wait 通常立即返回
    worker.deleteLater()


class _ChatWorker(QThread):
    """后台线程调服务器，避免阻塞 UI。"""
    done = Signal(str, str)  # (role, text)

    def __init__(self, client: ApiClient, message: str) -> None:
        super().__init__()
        self.client = client
        self.message = message

    def run(self) -> None:
        try:
            reply = self.client.chat(self.message)
            self.done.emit("assistant", reply)
        except Exception as e:
            self.done.emit("assistant", f"[连接失败] {e}")


class _GreetingWorker(QThread):
    """后台拉个性化问候。"""
    done = Signal(str)

    def __init__(self, client: ApiClient) -> None:
        super().__init__()
        self.client = client

    def run(self) -> None:
        try:
            self.done.emit(self.client.greeting())
        except Exception:
            self.done.emit("")  # 失败静默，保留默认问候


class _ExecResultWorker(QThread):
    """后台轮询执行器结果（since_id 之后的已执行指令）。"""
    done = Signal(int, list)

    def __init__(self, client: ApiClient, since_id: int) -> None:
        super().__init__()
        self.client = client
        self.since_id = since_id

    def run(self) -> None:
        try:
            self.done.emit(self.since_id, self.client.executor_results(self.since_id))
        except Exception:
            self.done.emit(self.since_id, [])


class _HistoryWorker(QThread):
    """后台加载历史消息。"""
    done = Signal(list)

    def __init__(self, client: ApiClient) -> None:
        super().__init__()
        self.client = client

    def run(self) -> None:
        try:
            self.done.emit(self.client.recent_messages(10))  # 只加载最近 10 条
        except Exception:
            self.done.emit([])


class _ApiWorker(QThread):
    """后台执行快捷查询（统计/周报），避免 UI 线程网络阻塞。"""
    done = Signal(str, str)  # (mode, text)

    def __init__(self, client: ApiClient, mode: str) -> None:
        super().__init__()
        self.client = client
        self.mode = mode

    def run(self) -> None:
        try:
            if self.mode == "stats":
                d = self.client.stats_summary(7)
                text = (
                    f"📊 近 7 天统计：<br>对话 {d['messages']} 条 · git 提交 {d['git_commits']} 次 · "
                    f"工作日志 {d['work_logs']} 条<br><br><b>应用时长 Top：</b><br>"
                    + "".join(f"· {a['name']}: {a['hours']}h<br>" for a in d.get("top_apps", []))
                    + "<br><b>浏览域名 Top：</b><br>"
                    + "".join(f"· {b['name']}: {b['count']} 次<br>" for b in d.get("top_domains", []))
                )
                self.done.emit("stats", text)
            elif self.mode == "daily":
                d = self.client.latest_daily()
                if not d:
                    text = "暂无今日小结。每晚 22:00 自动生成。"
                else:
                    text = f"🌙 {d['date']} 小结：<br>" + d["content"].replace("\n", "<br>")
                self.done.emit("daily", text)
            else:  # report
                r = self.client.latest_report()
                if not r:
                    text = "暂无周报。每周日 21:00 自动生成，敬请期待。"
                else:
                    text = f"📋 周报 {r.get('week', '')}：<br>{r.get('content', '')}"
                self.done.emit("report", text)
        except Exception as e:
            self.done.emit(self.mode, f"[获取失败] {e}")


class _HealthWorker(QThread):
    """后台健康检查线程：断线时机器人亮红灯。"""
    result = Signal(bool)

    def __init__(self, client) -> None:
        super().__init__()
        self.client = client

    def run(self) -> None:
        self.result.emit(self.client.health())
