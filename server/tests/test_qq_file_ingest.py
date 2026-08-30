"""QQ 插件文件入库纯函数测试：文本提取 / 安全文件名 / 大小与格式边界。

不依赖 astrbot 包——从 main.py 源码截取纯函数段执行（与 test_qq_plugin_gate 同法）。
"""
import importlib.util
import os
import sys
from pathlib import Path


def _load_pure_functions():
    """截取 main.py 中 astrbot import 之后的纯函数段执行。"""
    src = Path(__file__).resolve().parents[2] / "qq" / "astrbot_plugin_xy" / "main.py"
    text = src.read_text(encoding="utf-8")
    start = text.index("REPLY_MAX_CHARS")
    end = text.index("@register")
    ns: dict = {"os": os, "Path": Path, "re": __import__("re")}
    exec(text[start:end], ns)  # noqa: S102 - 受控源码片段
    return ns


ns = _load_pure_functions()
extract_text = ns["extract_text"]
safe_doc_name = ns["safe_doc_name"]
FILE_MAX_BYTES = ns["FILE_MAX_BYTES"]
find_file_id = ns["find_file_id"]
to_host_path = ns["to_host_path"]


def test_extract_txt(tmp_path):
    f = tmp_path / "笔记.txt"
    f.write_text("第一行设定\n第二行事实", encoding="utf-8")
    text, err = extract_text(str(f))
    assert err == ""
    assert "第二行事实" in text


def test_extract_md_and_csv(tmp_path):
    md = tmp_path / "设定.md"
    md.write_text("# 修炼体系\n练气 → 筑基", encoding="utf-8")
    text, err = extract_text(str(md))
    assert err == "" and "筑基" in text

    csv = tmp_path / "台账.csv"
    csv.write_text("日期,体重\n08-30,75.2", encoding="utf-8")
    text, err = extract_text(str(csv))
    assert err == "" and "75.2" in text


def test_extract_docx(tmp_path):
    pytest = __import__("pytest")
    docx = pytest.importorskip("docx")
    f = tmp_path / "文档.docx"
    d = docx.Document()
    d.add_paragraph("正文段落一")
    table = d.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "角色"
    table.cell(0, 1).text = "林渊"
    d.save(str(f))
    text, err = extract_text(str(f))
    assert err == ""
    assert "正文段落一" in text and "林渊" in text  # 表格文本也提取


def test_extract_pdf(tmp_path):
    pytest = __import__("pytest")
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    import io

    buf = io.BytesIO()
    writer.add_blank_page(595, 842)  # 空白页：验证不崩即可（无文本 → 空内容分支）
    writer.write(buf)
    f = tmp_path / "空白.pdf"
    f.write_bytes(buf.getvalue())
    text, err = extract_text(str(f))
    assert err == ""
    assert text.strip() == ""  # 空白 PDF → 空文本（由调用方提示"没有提取到文本"）


def test_extract_unsupported_ext(tmp_path):
    f = tmp_path / "程序.exe"
    f.write_bytes(b"MZ")
    text, err = extract_text(str(f))
    assert text == ""
    assert "不支持" in err and ".exe" in err


def test_extract_broken_docx(tmp_path):
    f = tmp_path / "坏.docx"
    f.write_bytes(b"not a zip")
    text, err = extract_text(str(f))
    assert text == "" and "解析失败" in err


def test_safe_doc_name_strips_path_and_illegal():
    assert safe_doc_name("D:/资料/设定:卡?.txt") == "设定_卡_.txt"  # 去目录+非法字符
    assert safe_doc_name("..\\..\\evil.txt") == "evil.txt"  # 防穿越
    assert safe_doc_name("///") == "未命名文档"  # 全非法字符兜底


def test_file_size_limit_constant():
    assert FILE_MAX_BYTES == 10 * 1024 * 1024


def test_find_file_id_matches_name_latest():
    msgs = [
        {"message": [{"type": "file", "data": {"file": "a.pdf", "file_id": "old"}}]},
        {"message": [{"type": "file", "data": {"file": "b.pdf", "file_id": "new"}}]},
    ]
    assert find_file_id(msgs, "b.pdf") == ("new", None)
    assert find_file_id(msgs, "a.pdf") == ("old", None)


def test_to_host_path_translates_container_paths():
    assert to_host_path("/app/.config/QQ/NapCat/temp/a.pdf") == "/opt/napcat/qq_config/NapCat/temp/a.pdf"
    assert to_host_path("/app/napcat/cache/x.png") == "/opt/napcat/cache/x.png"
    assert to_host_path("/tmp/other.txt") == "/tmp/other.txt"  # 非容器路径原样返回


def test_find_file_id_does_not_guess_other_file():
    msgs = [
        {"message": [{"type": "file", "data": {"file": "a.pdf", "file_id": "old"}}]},
        {"message": [{"type": "file", "data": {"file": "c.pdf", "file_id": "latest"}}]},
    ]
    assert find_file_id(msgs, "不存在的名字.pdf") is None
    assert find_file_id([], "x") is None
    assert find_file_id([{"message": [{"type": "text", "data": {"text": "hi"}}]}], "x") is None


def test_find_file_id_returns_declared_size():
    msgs = [{"message": [{"type": "file", "data": {
        "file": "guide.pdf", "file_id": "fid", "file_size": "9126420"
    }}]}]
    assert find_file_id(msgs, "guide.pdf") == ("fid", 9126420)


def test_whitelist_gates_file_channel_too():
    """文件通道与文本消息同闸门：陌生人/群聊/空配置一律不处理。"""
    should_handle = _load_should_handle()
    assert should_handle("10001", "", "10001") is True  # 主人私聊：可发文件
    assert should_handle("99999", "", "10001") is False  # 陌生人私聊：拒绝
    assert should_handle("10001", "888", "10001") is False  # 群聊：拒绝（即使主人）
    assert should_handle("10001", "", "") is False  # 未配置 owner：全拒


def _load_should_handle():
    src = Path(__file__).resolve().parents[2] / "qq" / "astrbot_plugin_xy" / "main.py"
    text = src.read_text(encoding="utf-8")
    start = text.index("def should_handle")
    end = text.index("@register")
    ns: dict = {}
    exec(text[start:end], ns)  # noqa: S102
    return ns["should_handle"]
