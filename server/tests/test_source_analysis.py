"""来源独立性核查测试：区分载体（域名）与原始信源。

背景：检索结果的 source 字段是平台域名，转载同一篇稿子会得到多个不同域名，
"N 条报道"于是被误当成"N 方印证"。这里锁定判定逻辑。
"""
import pytest

from app.chat import source_analysis as sa


def _item(title, summary="", domain="news.example.com"):
    return {"title": title, "summary": summary, "source": domain}


# ── 出处抽取 ────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("人民日报评“4岁男童被指摸臀”", "人民日报"),
    ("人民锐评：四岁男童的肢体接触", "人民日报"),          # 栏目名归一
    ("据《法制日报》报道，当地已介入", "法制日报"),
    ("据封面新闻等媒体报道", "封面新闻"),
    ("人民日报客户端发表评论文章", "人民日报"),
    ("来源：新京报", "新京报"),
])
def test_extract_origin_from_patterns(text, expected):
    assert sa.extract_origin(text) == expected


def test_mention_is_not_origin():
    """提及 ≠ 原创。腾讯自采稿里提到人民日报，不能算人民日报是信源。

    实测踩坑：放宽成"出现媒体名即算"后，两篇独立报道被并入人民日报名下，
    4 条转载被误算成 6 条，还抹掉了两个真正独立的信源。
    """
    text = "被指摸臀4岁男孩已返校，父亲发声；人民日报发声"
    assert sa.extract_origin(text) == ""
    assert sa.extract_origin("中国官媒《人民日报》刊文批评，复旦教授称…") == ""


# ── 域名映射：只认原创媒体 ──────────────────────────────────

def test_original_media_domains_map_to_origin():
    assert sa.origin_from_domain("www.zaobao.com.sg") == "联合早报"
    assert sa.origin_from_domain("people.com.cn") == "人民日报"
    assert sa.origin_from_domain("www.thepaper.cn") == "澎湃新闻"


@pytest.mark.parametrize("domain", [
    "finance.sina.com.cn", "news.qq.com", "www.sohu.com", "i.ifeng.com", "163.com",
])
def test_portals_are_not_origins(domain):
    """门户是转载载体，把它当信源等于把"被转载"当"原创"。"""
    assert sa.origin_from_domain(domain) == ""


# ── 归并：真实场景 ──────────────────────────────────────────

def test_reprints_of_one_article_collapse_into_one_origin():
    body = "近日，湖南长沙一起因幼童肢体触碰引发的纠纷持续引发关注。一女子称8月26日在某餐厅内被一名4岁男童摸了屁股"
    items = [
        _item("人民日报评“4岁男童被指摸臀”事件", body, "finance.sina.com.cn"),
        _item("人民锐评：四岁男童的肢体接触", body, "finance.sina.com.cn"),
        _item("人民日报评“女子称被4岁男童摸臀”", body, "i.ifeng.com"),
        _item("人民日报评“4岁男童被指摸臀”", body, "news.qq.com"),
    ]
    a = sa.analyze_sources(items)
    assert a.total == 4
    assert a.confirmed_count == 1
    assert a.max_reprint == 4
    assert a.clusters[0]["origin"] == "人民日报"
    assert len(a.clusters[0]["domains"]) == 3


def test_distinct_origins_stay_separate():
    items = [
        _item("人民日报评某事", "人民日报客户端发表评论文章《某事》", "finance.sina.com.cn"),
        _item("复旦教授：这事或载入史册", "湖南长沙一起纠纷引爆舆论，复旦大学中国研究院副院长称", "www.zaobao.com.sg"),
    ]
    a = sa.analyze_sources(items)
    assert a.confirmed_count == 2


def test_unattributed_items_go_to_unknown_not_independent():
    """没有出处的条目不能默认算独立信源。"""
    items = [
        _item("拿4岁小孩博流量？", "找茬我都想不出来这个说法，他只是跑了一下", "www.sohu.com"),
    ]
    a = sa.analyze_sources(items)
    assert a.confirmed_count == 0
    assert a.unknown_count == 1


def test_same_item_without_origin_merges_by_text():
    body = "同一段逐字复制开头的转载正文，用于验证文本重合判定是否生效，长度需要足够"
    a = sa.analyze_sources([
        _item("标题略有不同甲", body, "a.example.com"),
        _item("标题略有不同乙", body, "b.example.com"),
    ])
    assert a.unknown_count == 2
    assert len(a.unknown) == 1, "同稿无出处转载应归为一簇"


# ── 渲染文本 ────────────────────────────────────────────────

def test_render_warns_when_reprints_dominate():
    body = "近日，湖南长沙一起因幼童肢体触碰引发的纠纷持续引发关注，双方各执一词"
    a = sa.analyze_sources([
        _item("人民日报评某事", body, "finance.sina.com.cn"),
        _item("人民日报评某事", body, "news.qq.com"),
    ])
    text = a.render()
    assert "同一篇的转载" in text
    assert "不要用条数当作多方印证" in text


def test_render_avoids_fake_precision():
    """不把"无法判定"折算进一个漂亮的独立信源数。"""
    a = sa.analyze_sources([_item("无出处标题", "无出处摘要内容", "www.sohu.com")])
    text = a.render()
    assert "无法判定原始出处" in text
    assert "没有一条能确认原始出处" in text


def test_render_empty_for_no_results():
    assert sa.analyze_sources([]).render() == ""
