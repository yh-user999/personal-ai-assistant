"""小说工作台静态页面的 DOM XSS 回归测试。"""
from pathlib import Path  # noqa: I001


INDEX_HTML = Path(__file__).parents[1] / "app" / "web" / "static" / "index.html"


def test_novel_rendering_uses_dom_text_and_event_listeners():
    source = INDEX_HTML.read_text(encoding="utf-8")
    novel_script = source[source.index("function clearNode"):source.index("async function rebuildNovelIndex()")]

    assert "select.innerHTML" not in novel_script
    assert "novel-chapters').innerHTML" not in novel_script
    assert "novel-jobs').innerHTML" not in novel_script
    assert "onclick=\"jobAction" not in novel_script
    assert "document.createElement('option')" in novel_script
    assert "option.textContent" in novel_script
    assert "element.textContent" in novel_script
    assert "button.addEventListener('click'" in novel_script


def test_novel_search_snippets_are_written_as_text():
    source = INDEX_HTML.read_text(encoding="utf-8")
    search_script = source[source.index("function appendNovelCard"):source.index("async function rebuildNovelIndex()")]

    assert "x.snippet" in search_script
    assert "textContent" in search_script
    assert "appendNovelCard(chaptersNode" in search_script
    assert "innerHTML" not in search_script
