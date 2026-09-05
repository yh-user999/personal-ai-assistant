"""聊天面板 / 悬浮球的后台 QThread 工作线程（从 chat_panel.py、floating_ball.py 拆出）。

职责统一：所有网络调用都在后台线程跑，完成后 Signal 回 UI 线程，避免阻塞。

生命周期规范（Qt6Core!sizedFree 堆损坏崩溃的防御）：
worker 用完必须走 retire()——wait() 收尸后再 deleteLater()。
原因：QThread 的 C++ 对象在线程真正退出前就被 deleteLater 销毁时，
Qt 内部分配的线程栈/事件结构会被二次释放，崩溃点固定在
QtPrivate::sizedFree（生产 minidump 五份同址实锤）。

周报/小结展示统一走 render_report_html()：Markdown → 主题化 HTML。
"""
import html as html_lib

import markdown as md_lib
import theme
from api_client import ApiClient
from PySide6.QtCore import QThread, Signal


def render_report_html(subtitle: str, markdown_text: str) -> str:
    """周报/小结统一渲染：Markdown → HTML，带主题化排版。

    安全：先转义 & 与 <（与聊天气泡同策略，阻断 HTML 注入，保留 > 供
    Markdown 引用语法），再走 markdown 渲染。
    """
    safe = markdown_text.replace("&", "&amp;").replace("<", "&lt;")
    body = md_lib.markdown(safe, extensions=["tables", "fenced_code"])
    accent = theme.token("accent")
    border = theme.token("border")
    body = body.replace("<h2>", f"<h2 style='color:{accent};font-size:15px;margin:16px 0 6px;padding-bottom:4px;border-bottom:1px solid {border};'>")
    body = body.replace("<h3>", f"<h3 style='color:{theme.token('text_main')};font-size:13px;margin:12px 0 4px;'>")
    body = body.replace("<hr", f"<hr style='border:none;border-top:1px solid {border};margin:14px 0'")
    body = body.replace("<li>", "<li style='margin:3px 0;'>")
    body = body.replace("<table", "<table style='border-collapse:collapse;'")
    body = body.replace("<th", f"<th style='border:1px solid {border};padding:4px 8px;background:{theme.token('btn_bg')};'")
    body = body.replace("<td", f"<td style='border:1px solid {border};padding:4px 8px;'")
    return (
        f"<div style='color:{theme.token('text_sub')};font-size:12px;margin-bottom:10px;'>{html_lib.escape(subtitle)}</div>"
        f"<div style='font-size:13px;line-height:1.8;color:{theme.token('text_main')};'>{body}</div>"
    )


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

    def __init__(self, client: ApiClient, message: str, image_path: str | None = None) -> None:
        super().__init__()
        self.client = client
        self.message = message
        self.image_path = image_path

    def run(self) -> None:
        try:
            reply = self.client.chat(self.message, image_path=self.image_path)
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


class _SearchWorker(QThread):
    """后台消息全文检索（第 6.26 课）。"""
    done = Signal(object)  # dict 结果或 None（请求失败）

    def __init__(self, client: ApiClient, query: str) -> None:
        super().__init__()
        self.client = client
        self.query = query

    def run(self) -> None:
        self.done.emit(self.client.search_messages(self.query))


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
                # 应用名/域名来自行为采集的窗口标题——外部数据进 HTML 必须转义，
                # 否则恶意/意外的标题片段会被 QTextBrowser 当标签渲染
                esc = html_lib.escape
                text = (
                    f"📊 近 7 天统计：<br>对话 {d['messages']} 条 · git 提交 {d['git_commits']} 次 · "
                    f"工作日志 {d['work_logs']} 条<br><br><b>应用时长 Top：</b><br>"
                    + "".join(f"· {esc(a['name'])}: {a['hours']}h<br>" for a in d.get("top_apps", []))
                    + "<br><b>浏览域名 Top：</b><br>"
                    + "".join(f"· {esc(b['name'])}: {b['count']} 次<br>" for b in d.get("top_domains", []))
                )
                self.done.emit("stats", text)
            elif self.mode == "daily":
                d = self.client.latest_daily()
                if not d or not d.get("content"):
                    text = "暂无今日小结。每晚 22:00 自动生成。"
                else:
                    text = render_report_html(
                        f"🌙 {d['date']} 今日小结", d["content"]
                    )
                self.done.emit("daily", text)
            else:  # report
                r = self.client.latest_report()
                if not r or not r.get("content"):
                    text = "暂无周报。每周日 21:00 自动生成，敬请期待。"
                else:
                    text = render_report_html(
                        f"📋 周报 {r.get('week', '')}", r["content"]
                    )
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


class _LocalExecWorker(QThread):
    """后台执行本地指令（文件操作/脚本/文件搜索）。

    为什么必须离开 UI 线程：try_execute 内部可能跑
    subprocess.run(timeout=120) 的脚本、或 search_files_impl 扫最多 3000 个
    文件并打开 150 个读 64KB。这些原先直接在 _send 里同步调用，面板会假死
    到操作结束——项目本身已有完整 worker 体系，只有这条路径绕过了。

    done 携带 handled：False 表示"这不是本地指令"，UI 侧据此转发给服务器。
    """
    done = Signal(bool, str)  # (handled, text)

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def run(self) -> None:
        try:
            from local_exec import try_execute

            handled, text = try_execute(self.message)
            self.done.emit(handled, text)
        except Exception as e:
            # 本地执行异常不该静默：报回聊天流，且视为已处理（避免又发给服务器）
            self.done.emit(True, f"❌ 本地执行出错：{e}")
