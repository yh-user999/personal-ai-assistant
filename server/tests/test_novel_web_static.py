"""小说工作台静态页回归测试：结构、安全写入方式与关键文案存在性。

背景：工作台曾整体用 innerHTML 拼接章节/任务内容，小说正文来自 LLM 生成，
任意 `<script>` 都会被执行。此文件保证新增页面维持 DOM API 写入。
"""
from pathlib import Path

STATIC = Path(__file__).parents[1] / "app" / "web" / "static"
INDEX_HTML = STATIC / "index.html"
NOVEL_HTML = STATIC / "novel" / "novel.html"
NOVEL_JS = STATIC / "novel" / "novel.js"
STYLES_CSS = STATIC / "styles.css"
CHAT_JS = STATIC / "chat.js"
APP_JS = STATIC / "app.js"


def test_files_exist():
    for path in (INDEX_HTML, NOVEL_HTML, NOVEL_JS, STYLES_CSS, CHAT_JS, APP_JS):
        assert path.exists(), path.name


def _script_bodies(path: Path) -> str:
    import re
    text = path.read_text(encoding="utf-8")
    return "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", text, re.S))


def test_no_innerhtml_in_pages():
    for path in (INDEX_HTML, NOVEL_HTML, CHAT_JS, NOVEL_JS):
        assert "innerHTML" not in path.read_text(encoding="utf-8"), path.name
        assert "document.write" not in path.read_text(encoding="utf-8"), path.name


def test_all_dynamic_text_uses_dom_api():
    js = NOVEL_JS.read_text(encoding="utf-8")
    for safe in ("textContent", "createElement"):
        assert safe in js


def test_shared_token_carried_via_authorization_header():
    app_js = APP_JS.read_text(encoding="utf-8")
    assert "Bearer " in app_js
    assert "localStorage" in app_js


def test_novel_status_labels_are_localized():
    js = NOVEL_JS.read_text(encoding="utf-8")
    for status in ("queued", "generating", "awaiting_confirmation", "published", "failed", "cancelled", "reviewing"):
        assert status in js, status
    for label in ("排队中", "生成中", "待确认", "已发布", "失败", "已取消", "审阅中"):
        assert label in js, label


def test_search_does_not_trigger_rebuild():
    """前端搜索只调只读接口，不隐式重建索引。"""
    js = NOVEL_JS.read_text(encoding="utf-8")
    assert "chapters/search" in js
    assert "index/rebuild" in js  # 重建仅由显式按钮触发
    # 搜索路径与重建路径必须是两个不同函数
    assert "function searchNovel" in js or "const searchNovel" in js
    assert "function rebuildNovelIndex" in js or "const rebuildNovelIndex" in js


def test_index_page_links_to_workbench():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "/novel/" in html or "novel/novel.html" in html


def test_token_not_hardcoded():
    for path in (APP_JS, CHAT_JS, NOVEL_JS):
        text = path.read_text(encoding="utf-8")
        # 不允许出现看起来像真实 token 的长硬编码字符串
        import re
        assert not re.search(r"['\"][A-Za-z0-9_\-]{32,}['\"]", text), path.name


def test_old_inline_blocks_removed_from_index():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "novel-panel" not in html, "小说面板应已迁移到独立页面"
    assert "styles.css" in html and "chat.js" in html
