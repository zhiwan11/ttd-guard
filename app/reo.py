# -*- coding: utf-8 -*-
"""reo 链接生成（S6 第一步，白皮书 §5.6 / D6）。

设计边界：
- **绝不猜 reo 的网址格式**（D6）：三个 URL 模板全部由用户在设置页填入
  （reo_tv_url / reo_movie_url / reo_search_url），默认空字符串——全空时
  ``build_url`` 一律返回 ''，前端据此不渲染「在 reo 找资源」按钮；
- 占位符只支持 {tmdb_id} / {title} / {year}（§5.6 设计 2）；
- **TMDB 的电影和剧集 ID 空间独立**（§5.6 已知）：同一数字在两边是两部不同
  作品——tv 一律用 reo_tv_url、movie 一律用 reo_movie_url，互不串用（A9）；
- 优先 TMDB ID 模板；tmdb_id 缺失时才回退 reo_search_url 关键词搜索（{title}/{year}）；
- 第一步只做「生成并打开链接」：链接在用户浏览器打开，TTD Guard 不保存、
  不使用 reo 账号，不抓取页面。第二步「推送手动 strm 频道」不在本阶段
  （可选且依赖用户确认）。

只依赖 config（URL 模板来源），不依赖 Emby/TMDB，便于单测。
"""
import logging
import urllib.parse

from . import config as _cfg

log = logging.getLogger('media_agent')

# 允许的占位符（超出的一律原样保留并告警——不静默吞，方便发现模板填错）
PLACEHOLDERS = ('{tmdb_id}', '{title}', '{year}')


def _template_for(media_type):
    """按 media_type 取对应模板——类型即路由，TV/Movie 两套模板互不串用。"""
    cfg = _cfg.load_config()
    return (cfg.get('reo_movie_url') or '') if str(media_type) == 'movie' \
        else (cfg.get('reo_tv_url') or '')


def _search_template():
    cfg = _cfg.load_config()
    return cfg.get('reo_search_url') or ''


def _fill(template, tmdb_id='', title='', year=''):
    """占位符替换：{tmdb_id}/{title}/{year}；title 做 URL 编码（关键词搜索路径）。"""
    out = template
    out = out.replace('{tmdb_id}', str(tmdb_id or ''))
    out = out.replace('{title}', urllib.parse.quote(str(title or ''), safe=''))
    out = out.replace('{year}', str(year or ''))
    return out


def build_url(media_type, tmdb_id='', title='', year='', force_search=False):
    """生成「在 reo 找资源」的链接。

    - media_type: 'tv' | 'movie'（决定用哪个模板——类型即路由，A9 口径）；
    - 优先 TMDB ID 模板；tmdb_id 为空或 force_search=True 时回退 reo_search_url；
    - 回退也拿不到（无搜索模板 / 标题为空）或模板全空 → 返回 ''，
      前端据 ''' 不渲染按钮（三处卡片共用这个约定）；
    - 模板里没有任何占位符时原样使用（用户自己决定放哪），但 tmdb_id 为空仍视为无效。
    """
    media_type = str(media_type or '').strip().lower()
    if media_type not in ('tv', 'movie'):
        return ''
    title = str(title or '').strip()
    year = str(year or '').strip()
    tid = str(tmdb_id or '').strip()

    if not force_search and tid:
        tpl = _template_for(media_type)
        if tpl:
            return _fill(tpl, tmdb_id=tid, title=title, year=year)

    # 兜底：关键词搜索（剧名+年份）——TMDB ID 拿不到或用户选兜底才用（§5.6 设计 3）
    search_tpl = _search_template()
    if search_tpl and title:
        return _fill(search_tpl, tmdb_id=tid, title=title, year=year)
    return ''


def reo_enabled():
    """三个模板是否至少配置了一个（接口下发 reo 字段的前置条件；全空则不产生链接）。"""
    cfg = _cfg.load_config()
    return bool(cfg.get('reo_tv_url') or cfg.get('reo_movie_url') or cfg.get('reo_search_url'))
