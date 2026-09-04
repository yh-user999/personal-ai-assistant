"""小说写作增强二期测试：章节分析 + 逻辑一致性 + 跨章记忆。

覆盖：命令解析正反例（与冲突检查/续写不互吞）、残留检测、章节号中文数字
转换、chapter_notes upsert 幂等、build_continuity_block 空表/有数据、
分析 JSON 容错、LLM 失败兜底、分析后自动存档、被动抓取门槛、
dispatch 短路、回复含字数对照与类型排序。
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.models.database import connect, init_db  # noqa: E402
from app.services import chapter_analysis as ca  # noqa: E402


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    init_db()
    yield


# ── ① 命令解析 ─────────────────────────────────────────────

def test_parse_analysis_command_positives():
    assert ca.parse_analysis_command("分析章节：李羽推开门") == "李羽推开门"
    assert ca.parse_analysis_command("章节分析：这段节奏太赶") == "这段节奏太赶"
    assert ca.parse_analysis_command("请帮我分析章节：测试正文") == "测试正文"
    assert ca.parse_analysis_command("帮我章节合理性：xxx") == "xxx"
    assert ca.parse_analysis_command("逻辑分析：前后矛盾") == "前后矛盾"


def test_parse_analysis_command_negatives():
    # 冒号必须：不带冒号不吞（防误吞普通聊天）
    assert ca.parse_analysis_command("分析章节这章写得怎么样") is None
    assert ca.parse_analysis_command("帮我分析一下剧情") is None
    # 与冲突检查不互吞（路由顺序保证"检查设定冲突："先命中）
    assert ca.parse_analysis_command("检查设定冲突：李羽会飞") is None
    assert ca.parse_analysis_command("设定冲突检查：李羽用命丛") is None
    # 与续写不互吞
    assert ca.parse_analysis_command("续写：李羽握紧刀") is None
    assert ca.parse_analysis_command("帮我续写一段") is None
    assert ca.parse_analysis_command("今天天气不错") is None


def test_parse_archive_command():
    a = ca.parse_archive_command("章节存档：第一章 李羽穿越乱世")
    assert a == ("1", "李羽穿越乱世", [])
    a2 = ca.parse_archive_command("章节存档：第12章 少爷上门（伏笔：地契下落；老人来历）")
    assert a2 == ("12", "少爷上门", ["地契下落", "老人来历"])
    # 缺摘要/缺章号不吞
    assert ca.parse_archive_command("章节存档：第X章") is None
    assert ca.parse_archive_command("章节存档：第章 测试") is None
    assert ca.parse_archive_command("随便聊聊") is None


# ── ② 章节号与残留检测（零 LLM）────────────────────────────

def test_cn_to_int():
    assert ca.cn_to_int("1") == 1
    assert ca.cn_to_int("12") == 12
    assert ca.cn_to_int("十") == 10
    assert ca.cn_to_int("十二") == 12
    assert ca.cn_to_int("二十") == 20
    assert ca.cn_to_int("一百零五") == 105
    assert ca.cn_to_int("二百三十") == 230
    assert ca.cn_to_int("两百零二") == 202
    assert ca.cn_to_int("abc") is None
    assert ca.cn_to_int("") is None


def test_extract_chapter_no():
    assert ca.extract_chapter_no("第一章 少年") == "1"
    assert ca.extract_chapter_no("第12章 试探") == "12"
    assert ca.extract_chapter_no("第一章") == "1"
    assert ca.extract_chapter_no("第三回 风起") == "3"
    assert ca.extract_chapter_no("正文直接开始") is None
    assert ca.extract_chapter_no("开头没有章节号\n第一章") is None  # 只认正文头部


def test_detect_residue():
    # 章节尾标记
    r = ca.detect_residue("李羽站起来。\n本章完\n他望向远方。")
    assert r == [("章节尾标记", "本章完")]
    assert ca.detect_residue("正文一\n完。\n正文二")[0][0] == "章节尾标记"
    # AI 元话语
    r2 = ca.detect_residue("写完了。你看看节奏对不对。\n正文内容。")
    assert r2 and r2[0][0] == "AI元话语"
    assert ca.detect_residue("以下是本章正文")[0][0] == "AI元话语"
    assert ca.detect_residue("希望这段符合你的预期")[0][0] == "AI元话语"
    # 干净文本零误报
    assert ca.detect_residue("李羽望着田里的庄稼，心里盘算着收成。") == []
    assert ca.detect_residue("") == []


# ── ③ chapter_notes 存档 ───────────────────────────────────

def test_upsert_chapter_note_idempotent(db):
    ca.upsert_chapter_note("1", "旧摘要", ["伏笔A"], source="manual")
    ca.upsert_chapter_note("1", "新摘要：李羽穿越乱世", ["伏笔A"], source="analysis")
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM chapter_notes WHERE chapter='1'").fetchall()
        assert len(rows) == 1, "同章 upsert 不堆重复行"
        assert rows[0]["summary"] == "新摘要：李羽穿越乱世"
        assert rows[0]["source"] == "analysis"
    finally:
        conn.close()


def test_get_chapter_note_roundtrip(db):
    assert ca.get_chapter_note("9") is None
    ca.upsert_chapter_note("9", "测试摘要", ["线头一", "线头二"])
    note = ca.get_chapter_note("9")
    assert note["summary"] == "测试摘要"
    assert note["threads"] == ["线头一", "线头二"]
    assert note["source"] == "manual"


def test_build_continuity_block_empty(db):
    assert ca.build_continuity_block() == ""


def test_build_continuity_block_with_data(db):
    ca.upsert_chapter_note("1", "李羽穿越乱世，田被豪强盯上", ["地契下落"])
    ca.upsert_chapter_note("2", "少爷上门收地，李羽隐忍", ["老人来历", "地契下落"])
    block = ca.build_continuity_block()
    assert "【前情提要" in block and "不得与之矛盾" in block
    assert "第1章：李羽穿越乱世" in block and "第2章" in block
    assert "【未回收伏笔" in block
    # 跨章伏笔去重
    assert block.count("地契下落") == 1
    # 章节升序呈现
    assert block.index("第1章") < block.index("第2章")


# ── ④ 分析主流程 ───────────────────────────────────────────

def _patch_llm(monkeypatch, fn):
    import app.core.llm as llm

    monkeypatch.setattr(llm, "chat", fn)


def test_parse_problems_json_tolerant():
    out = '噪声 {"summary":"剧情","problems":[{"type":"称谓","quote":"孙家少爷","problem":"设定是赵家","suggestion":"统一"},{"type":"乱写的","problem":"过滤"}],"pacing":"ok","threads":["线头"]} 噪声'
    problems = ca.parse_problems_json(out)
    assert len(problems) == 2
    # 白名单外的类型归为"逻辑"（不丢条目），且按类型排序后逻辑在前
    assert problems[0]["type"] == "逻辑"
    assert problems[1]["type"] == "称谓"
    assert ca.parse_problems_json("没有json") == []
    assert ca.parse_problems_json('{"problems": []}') == []
    assert ca.parse_problems_json('{"problems": "notalist"}') == []
    threads = ca.parse_threads_json(out)
    assert threads == ["线头"]


def test_analyze_chapter_llm_failure(db, monkeypatch):
    async def fail_chat(messages, **kw):
        raise TimeoutError("boom")

    _patch_llm(monkeypatch, fail_chat)
    result = asyncio.run(ca.analyze_chapter("第一章 开始\n写完了。你看看"))
    assert "章节分析暂时没跑通" in result["reply"]
    assert "AI元话语" in result["reply"], "LLM 挂了也要带上零 LLM 预检结果"


def test_analyze_chapter_success_and_auto_archive(db, monkeypatch):
    async def fake_chat(messages, **kw):
        assert any("权威设定" in m["content"] for m in messages)
        return (
            '{"summary":"李羽穿越乱世，发现田地被豪强盯上",'
            '"problems":[{"type":"称谓","quote":"孙家少爷","problem":"与前文赵家不一致","suggestion":"统一为赵家"},'
            '{"type":"逻辑","quote":"他死了又活了","problem":"前后矛盾","suggestion":"删除"}],'
            '"pacing":"3个重大事件，超载","threads":["地契下落","老人来历"]}'
        )

    _patch_llm(monkeypatch, fake_chat)
    text = "第一章 风起\n" + "李羽在田里干活，孙家少爷带人上门。" * 3 + "\n本章完"
    result = asyncio.run(ca.analyze_chapter(text, user_id=""))
    reply = result["reply"]
    # 预检（残留 + 字数）
    assert "章节尾标记" in reply and "本章完" in reply
    assert "字数：约" in reply
    # 问题清单（带引句与建议）
    assert "发现 2 处问题" in reply
    assert "【称谓】" in reply and "赵家" in reply
    assert "你写的：孙家少爷" in reply and "建议：统一为赵家" in reply
    # 严重度排序：逻辑在前、称谓在后
    assert reply.index("【逻辑】") < reply.index("【称谓】")
    # 节奏 / 剧情 / 伏笔
    assert "节奏评估" in reply and "超载" in reply
    assert "一句话剧情" in reply and "豪强" in reply
    assert "地契下落" in reply and "老人来历" in reply
    # 自动存档（幂等：重复分析更新不重复）
    note = ca.get_chapter_note("1")
    assert note is not None and note["source"] == "analysis"
    assert "豪强" in note["summary"] and note["threads"] == ["地契下落", "老人来历"]
    asyncio.run(ca.analyze_chapter(text, user_id=""))
    conn = connect()
    try:
        rows = conn.execute("SELECT id FROM chapter_notes WHERE chapter='1'").fetchall()
        assert len(rows) == 1
    finally:
        conn.close()


def test_analyze_chapter_no_problems(db, monkeypatch):
    async def fake_chat(messages, **kw):
        return '{"summary":"无事发生","problems":[],"pacing":"1个事件，节奏正常","threads":[]}'

    _patch_llm(monkeypatch, fake_chat)
    result = asyncio.run(ca.analyze_chapter("第一章 平静\n李羽干了一天农活。"))
    assert "未发现问题" in result["reply"]
    assert "一句话剧情" in result["reply"]


def test_analyze_chapter_sepia_prompt_and_separate_problems(db, monkeypatch):
    async def fake_chat(messages, **kw):
        assert any("Sepia 小说审校规则" in m["content"] for m in messages)
        assert any("sepia_problems" in m["content"] for m in messages)
        return (
            '{"summary":"李羽推门发现屋内无人", "problems":[], '
            '"sepia_problems":[{"type":"表层","quote":"# 说明",'
            '"problem":"正文混入标题式 Markdown","suggestion":"删除标题，只保留正文"}], '
            '"pacing":"1个事件，节奏正常", "threads":[]}'
        )

    _patch_llm(monkeypatch, fake_chat)
    result = asyncio.run(ca.analyze_chapter("第一章 平静\n# 说明\n李羽推门发现屋内无人。"))
    reply = result["reply"]
    assert "Sepia 发现 1 处" in reply
    assert "【表层】" in reply
    assert "正文混入标题式 Markdown" in reply
    assert "你写的：# 说明" in reply


def test_analyze_chapter_word_count_note(db, monkeypatch):
    """无 facts 字数目标时只报实测字数，不做偏差判断。"""
    async def fake_chat(messages, **kw):
        return '{"summary":"s","problems":[],"pacing":"","threads":[]}'

    _patch_llm(monkeypatch, fake_chat)
    result = asyncio.run(ca.analyze_chapter("第一章 平静\n正文"))
    assert "字数：约" in result["reply"]
    assert "字数偏差" not in result["reply"]


def test_word_count_target_is_user_scoped(db):
    """章节分析读取写作原则 facts 时不能把主人目标泄露给访客。"""
    from app.core.memory import normalize_user_id

    owner_id = normalize_user_id(None)
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO facts "
            "(user_id, subject, predicate, object, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (owner_id, "小说", "写作字数", "每章约3000字", "2026-09-03T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()

    assert "字数偏差" in "\\n".join(ca._word_count_block("第一章\\n正文", owner_id))
    assert "字数偏差" not in "\\n".join(ca._word_count_block("第一章\\n正文", "10086"))


# ── ⑤ 被动抓取 ─────────────────────────────────────────────

def test_capture_chapter_reply_success(db, monkeypatch):
    async def fake_chat(messages, **kw):
        return '{"summary":"少爷带人抢田","threads":["老人异常"]}'

    _patch_llm(monkeypatch, fake_chat)
    asyncio.run(ca.capture_chapter_reply("2", "第二章 抢地\n" + "少爷带人踏进田里。" * 100))
    note = ca.get_chapter_note("2")
    assert note and note["source"] == "auto" and "抢田" in note["summary"]


def test_capture_chapter_reply_failure_silent(db, monkeypatch):
    async def fail_chat(messages, **kw):
        raise RuntimeError("down")

    _patch_llm(monkeypatch, fail_chat)
    asyncio.run(ca.capture_chapter_reply("3", "第三章 测试\n内容"))  # 不抛异常
    assert ca.get_chapter_note("3") is None


def test_capture_chapter_reply_no_json(db, monkeypatch):
    async def bad_chat(messages, **kw):
        return "模型没吐 JSON"

    _patch_llm(monkeypatch, bad_chat)
    asyncio.run(ca.capture_chapter_reply("4", "第四章 测试\n内容"))
    assert ca.get_chapter_note("4") is None


# ── ⑥ 路由与门槛 ───────────────────────────────────────────

def test_maybe_capture_chapter_gates(monkeypatch):
    """被动抓取三重门槛：生成档 + 长回复 + 正文头部含章节号。"""
    from app.chat.pipeline import _maybe_capture_chapter
    from app.chat.prompting import PromptAssembly

    # track_background 需要 running loop：测试里同步跑完协程
    monkeypatch.setattr(
        "app.chat.pipeline.retrieval.track_background",
        lambda rt, aw: asyncio.run(aw),
    )

    class _S:
        calls: list = []
        extract_chapter_no = staticmethod(ca.extract_chapter_no)

        async def capture_chapter_reply(self, ch, reply, uid):
            self.calls.append((ch, uid))

    class _FakeRuntime:
        services = type("S", (), {"chapter_analysis": _S()})()

    def _assembly(gen_profile):
        return PromptAssembly(system="", llm_messages=[], gen_messages=[], gen_profile=gen_profile)

    runtime = _FakeRuntime()

    # 非生成档不触发
    _maybe_capture_chapter(_assembly(False), "第一章 " + "字" * 1300, runtime, "u1")
    assert runtime.services.chapter_analysis.calls == []
    # 生成档但回复太短不触发
    _maybe_capture_chapter(_assembly(True), "第一章 短回复", runtime, "u1")
    assert runtime.services.chapter_analysis.calls == []
    # 生成档长回复但无章节号不触发
    _maybe_capture_chapter(_assembly(True), "没有章节号的" + "字" * 1300, runtime, "u1")
    assert runtime.services.chapter_analysis.calls == []
    # 全部满足才触发
    _maybe_capture_chapter(_assembly(True), "第一章 开端\n" + "字" * 1300, runtime, "u1")
    assert len(runtime.services.chapter_analysis.calls) == 1
    assert runtime.services.chapter_analysis.calls[0][0] == "1"
    assert runtime.services.chapter_analysis.calls[0][1] == "u1"


def test_dispatch_novel_analysis_short_circuit(db, monkeypatch):
    """「分析章节：…」经 _novel 短路返回，不落主流水线。"""
    from app.chat.context import ChatContext, ChatRequest
    from app.chat import routing

    async def fake_chat(messages, **kw):
        return '{"summary":"剧情","problems":[],"pacing":"","threads":[]}'

    _patch_llm(monkeypatch, fake_chat)

    ctx = ChatContext(
        request=type("Request", (), {"state": type("State", (), {})()})(),
        request_model=ChatRequest(message="分析章节：第一章 李羽在田里干活"),
        message="分析章节：第一章 李羽在田里干活",
        uid="",
        is_owner=True,
    )

    class _NW:
        parse_writing_log = staticmethod(lambda msg: None)
        looks_like_file_path = staticmethod(lambda s: False)
        parse_conflict_command = staticmethod(lambda msg: None)
        parse_continue_command = staticmethod(lambda msg: None)

    runtime = type(
        "Runtime",
        (),
        {
            "services": type(
                "S", (), {"novel_writing": _NW(), "chapter_analysis": ca}
            )(),
        },
    )()
    resp = asyncio.run(routing._novel(ctx, runtime))
    assert resp is not None and "一句话剧情" in resp.reply
    assert ca.get_chapter_note("1") is not None, "路由层调用同样自动存档"


def test_dispatch_novel_archive_short_circuit(db):
    """「章节存档：…」零 LLM 入库并确认。"""
    from app.chat.context import ChatContext, ChatRequest
    from app.chat import routing

    ctx = ChatContext(
        request=type("Request", (), {"state": type("State", (), {})()})(),
        request_model=ChatRequest(message="章节存档：第五章 李羽夺回田地（伏笔：地契）"),
        message="章节存档：第五章 李羽夺回田地（伏笔：地契）",
        uid="",
        is_owner=True,
    )

    class _NW:
        parse_writing_log = staticmethod(lambda msg: None)
        parse_conflict_command = staticmethod(lambda msg: None)
        parse_continue_command = staticmethod(lambda msg: None)

    runtime = type(
        "Runtime",
        (),
        {
            "services": type(
                "S", (), {"novel_writing": _NW(), "chapter_analysis": ca}
            )(),
        },
    )()
    resp = asyncio.run(routing._novel(ctx, runtime))
    assert resp is not None and "第5章已存档" in resp.reply and "伏笔 1 条" in resp.reply
    assert ca.get_chapter_note("5")["summary"] == "李羽夺回田地"


def test_dispatch_novel_analysis_rejects_file_path(db):
    """「分析章节：F:/稿子.txt」提示粘贴正文而不是烧 LLM。"""
    from app.chat.context import ChatContext, ChatRequest
    from app.chat import routing

    ctx = ChatContext(
        request=type("Request", (), {"state": type("State", (), {})()})(),
        request_model=ChatRequest(message="分析章节：F:/wfy/第10章.txt"),
        message="分析章节：F:/wfy/第10章.txt",
        uid="",
        is_owner=True,
    )

    class _NW:
        parse_writing_log = staticmethod(lambda msg: None)
        looks_like_file_path = staticmethod(lambda s: True)
        parse_conflict_command = staticmethod(lambda msg: None)
        parse_continue_command = staticmethod(lambda msg: None)

    runtime = type(
        "Runtime",
        (),
        {
            "services": type(
                "S", (), {"novel_writing": _NW(), "chapter_analysis": ca}
            )(),
        },
    )()
    resp = asyncio.run(routing._novel(ctx, runtime))
    assert resp is not None and "粘贴正文" in resp.reply
