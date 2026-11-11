"""把 report.md 渲染成单文件 HTML：内嵌样式、离线能看、可直接打印。

报告最终是给人看的。Markdown 的表格和层级在浏览器里几乎等于纯文本，
而这份报告的读者是要拿它做决策的——所以正文照旧由规则引擎生成，
只在交付形态上换成排版好的 HTML。

不引入 markdown 依赖：这里的输入是我们自己拼出来的子集
（标题、表格、有序/无序列表、引用、分隔线、段落，行内粗体/行内代码/链接），
自己写解析反而可控——表格和状态列要特殊处理，通用渲染器给不了。
"""
from __future__ import annotations

import html
import re
from typing import List, Optional, Tuple

# 规则状态 → 徽标样式与中文说法。报告里 PASS/WARN 是给阈值表对账用的，
# 读者要的是“过没过”，两个都给会挤爆单元格。
_STATUS_STYLE = {
    "PASS": ("ok", "通过"),
    "WARN": ("warn", "警戒"),
    "FAIL": ("bad", "未过"),
    "NO_DATA": ("muted", "缺数据"),
}
_STATUS_RE = re.compile(r"^[\s✅⚠️❌➖⚪·]*\b(PASS|WARN|FAIL|NO_DATA)\b[\s❌]*$")
_NUM_RE = re.compile(r"^[¥$€]?-?[\d,]+(\.\d+)?\s*(%|次/周|条|个|件|USD|天|in|lb)?$")
_REF_RE = re.compile(r"^(EV|R|KC|PK)-?\d+$")


# ---------------------------------------------------------------- 行内

def _inline(text: str) -> str:
    """转义后再还原行内语法。顺序要紧：行内代码先抽走，否则里面的 ** 会被当粗体。"""
    t = html.escape(text, quote=False)
    codes: List[str] = []

    def stash(m: "re.Match[str]") -> str:
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    t = re.sub(r"`([^`]+)`", stash, t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"!\[([^\]]*)\]\((https?://[^)\s]+)\)",
               r'<img class="pimg" src="\2" alt="\1" loading="lazy" referrerpolicy="no-referrer">', t)
    t = re.sub(r"(?<!!)\[([^\]]+)\]\((https?://[^)\s]+)\)",
               r'<a href="\2" target="_blank" rel="noreferrer">\1</a>', t)
    t = re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{codes[int(m.group(1))]}</code>", t)
    return t


def _cell_html(raw: str) -> str:
    text = raw.strip()
    if not text:
        return ""
    m = _STATUS_RE.match(text)
    if m:
        cls, label = _STATUS_STYLE[m.group(1)]
        return f'<span class="pill {cls}">{label}</span>'
    if _REF_RE.match(text):
        return f'<span class="ref">{_inline(text)}</span>'
    if _NUM_RE.match(text) and re.search(r"\d", text):
        return f'<span class="num">{_inline(text)}</span>'
    return _inline(text)


# ---------------------------------------------------------------- 块级

def _is_sep(line: str) -> bool:
    s = line.strip().strip("|")
    return bool(s) and set(s.replace("|", "").replace(":", "").strip()) <= {"-", " "} \
        and "-" in s


def _table_html(rows: List[str]) -> str:
    def cells(line: str) -> List[str]:
        return [c for c in line.strip().strip("|").split("|")]

    head, body = cells(rows[0]), [cells(r) for r in rows[2:]]
    out = ['<div class="table-wrap"><table>', "<thead><tr>"]
    out += [f"<th>{_cell_html(c)}</th>" for c in head]
    out.append("</tr></thead><tbody>")
    for row in body:
        row = (row + [""] * len(head))[:len(head)]
        out.append("<tr>" + "".join(f"<td>{_cell_html(c)}</td>" for c in row) + "</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def _ul_html(items: List[str]) -> str:
    out = ['<ul class="list">']
    for raw_item in items:
        it, _, note = raw_item.partition("\x01")
        m = re.match(r"^\[( |x|X)\]\s*(.+)$", it)
        if m:
            mark = "☑" if m.group(1).lower() == "x" else "☐"
            box = f'<li class="task"><span class="box">{mark}</span>{_inline(m.group(2))}'
        else:
            box = f"<li>{_inline(it)}"
        if note:
            box += f'<span class="sub">{_inline(note)}</span>'
        out.append(box + "</li>")
    out.append("</ul>")
    return "".join(out)


def _ol_html(items: List[str]) -> str:
    out = ['<ol class="list ordered">']
    out += [f"<li>{_inline(it)}</li>" for it in items]
    out.append("</ol>")
    return "".join(out)


def _section_id(index: int) -> str:
    return f"sec-{index}"


# 支撑材料：默认折起来。判断、账、待办留在正文里，读者不该翻两屏才看到结论。
# 前端报告页用同一份标题列表折叠（frontend/src/views/RunDetail.tsx），改这里要同步改那边。
DETAIL_TITLES = ("图像识别", "五维评分", "关键数据", "规则清单", "竞品样本", "估算假设", "证据索引")

# 旧报告的章节名 → 现名：历史报告在导出时也统一成同一套名称
_TITLE_ALIAS = {
    "一句话结论": "核心结论", "选的到底是什么产品": "产品与货源", "三个判断": "三项判断",
    "钱怎么算的": "利润测算", "六道门（定档依据）": "定档依据", "待办清单": "待办事项",
    "研判正文": "研判分析", "头部竞品样本": "竞品样本", "以图识货": "图像识别",
    "用户声音样本（公开讨论摘要）": "用户声音", "用户声音（公开讨论）": "用户声音",
}


def _canon_title(title: str) -> str:
    for old, new in _TITLE_ALIAS.items():
        if title.startswith(old):
            return new + title[len(old):]
    return title


def _is_detail(title: str) -> bool:
    return any(title.startswith(t) for t in DETAIL_TITLES)


def _body_html(lines: List[str]) -> Tuple[str, List[Tuple[str, str, int]]]:
    """返回（正文 HTML, 目录项）。按 ## 切节，节号用于目录与标题前缀。"""
    blocks: List[Tuple[str, object]] = []
    i, n = 0, len(lines)
    while i < n:
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        if s.startswith("```"):
            buf, i = [], i + 1
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            blocks.append(("code", "\n".join(buf)))
            continue
        if s.startswith("#"):
            level = len(s) - len(s.lstrip("#"))
            blocks.append((f"h{min(level, 4)}", s[level:].strip()))
            i += 1
            continue
        if s.startswith("|") and i + 1 < n and _is_sep(lines[i + 1]):
            buf = [lines[i], lines[i + 1]]
            i += 2
            while i < n and lines[i].strip().startswith("|"):
                buf.append(lines[i])
                i += 1
            blocks.append(("table", buf))
            continue
        if s.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append(("quote", " ".join(x for x in buf if x)))
            continue
        if s in ("---", "***", "___"):
            blocks.append(("hr", ""))
            i += 1
            continue
        if re.match(r"^[-*]\s+", s):
            buf: List[str] = []
            while i < n:
                cur = lines[i].strip()
                if re.match(r"^[-*]\s+", cur):
                    buf.append(re.sub(r"^[-*]\s+", "", cur))
                    i += 1
                    continue
                # 缩进两格的普通行是上一条的子注（用户声音的英文原文就靠这个层级）
                raw = lines[i]
                if buf and raw[:2] == "  " and raw.strip() and \
                        not raw.strip().startswith(("#", "|", ">", "```")):
                    buf[-1] += "\x01" + raw.strip()
                    i += 1
                    continue
                break
            blocks.append(("ul", buf))
            continue
        if re.match(r"^\d+[.)]\s+", s):
            buf = []
            while i < n and re.match(r"^\d+[.)]\s+", lines[i].strip()):
                buf.append(re.sub(r"^\d+[.)]\s+", "", lines[i].strip()))
                i += 1
            blocks.append(("ol", buf))
            continue
        buf = []
        while i < n:
            cur = lines[i].strip()
            if not cur or cur.startswith(("#", "|", ">", "```")) or re.match(r"^[-*]\s+", cur) \
                    or re.match(r"^\d+[.)]\s+", cur) or cur in ("---", "***", "___"):
                break
            buf.append(cur)
            i += 1
        blocks.append(("p", " ".join(buf)))

    out: List[str] = []
    toc: List[Tuple[str, str, int]] = []
    sec_index = 0
    sec_open = False
    sec_is_fold = False

    def close_sec() -> None:
        out.append("</details>" if sec_is_fold else "</section>")

    for kind, payload in blocks:
        if kind == "h2":
            if sec_open:
                close_sec()
            sec_index += 1
            sec_id = _section_id(sec_index)
            title = _canon_title(str(payload))
            sec_is_fold = _is_detail(title)
            toc.append((sec_id, title, sec_index))  # 目录与正文同一套编号，折叠节也列出
            if sec_is_fold:
                out.append(f'<details class="sec fold" id="{sec_id}">')
                out.append(f'<summary><span class="idx">{sec_index:02d}</span>{_inline(title)}'
                           f'<span class="fold-hint">展开</span></summary>')
            else:
                out.append(f'<section class="sec" id="{sec_id}">')
                out.append(f'<h2><span class="idx">{sec_index:02d}</span>{_inline(title)}</h2>')
            sec_open = True
        elif kind == "h1":
            # 一级标题已在页首 hero 里出现过，正文里不再重复
            continue
        elif kind == "h3":
            out.append(f'<h3 class="sub">{_inline(str(payload))}</h3>')
        elif kind == "h4":
            out.append(f'<h4 class="sub">{_inline(str(payload))}</h4>')
        elif kind == "table":
            out.append(_table_html(payload))  # type: ignore[arg-type]
        elif kind == "ul":
            out.append(_ul_html(payload))  # type: ignore[arg-type]
        elif kind == "ol":
            out.append(_ol_html(payload))  # type: ignore[arg-type]
        elif kind == "quote":
            out.append(f'<blockquote>{_inline(str(payload))}</blockquote>')
        elif kind == "hr":
            out.append('<hr class="rule">')
        elif kind == "code":
            out.append(f"<pre><code>{html.escape(str(payload))}</code></pre>")
        else:
            out.append(f"<p>{_inline(str(payload))}</p>")
    if sec_open:
        close_sec()
    return "".join(out), toc


# ---------------------------------------------------------------- 页首

def _parse_head(head: List[str]) -> Tuple[str, str, List[str]]:
    """从正文第一段抽出标题、裁定行与元信息，页首要单独排版。"""
    title, verdict, meta = "", "", []
    for raw in head:
        s = raw.strip()
        if s.startswith("# "):
            title = s[2:].strip()
        elif s.startswith(">"):
            verdict = verdict or s.lstrip(">").strip()
        elif re.match(r"^[-*]\s+", s):
            meta.append(re.sub(r"^[-*]\s+", "", s))
    return title, verdict, meta


_VERDICT_RE = re.compile(r"\*\*(.+?)\*\*")
_SCORE_RE = re.compile(r"综合\s*([\d.]+)\s*/\s*100")
_COVER_RE = re.compile(r"数据覆盖\s*(\d+)%")
_CONF_RE = re.compile(r"置信度\s*([^（(｜|]+)[（(]([\d.]+)[）)]")

_TIER_CLASS = {"做": "go", "有条件做": "cond", "暂缓": "defer", "不做": "nogo", "未研判": "unknown"}
_TIER_EMOJI = {"🟢": "go", "🟡": "cond", "🟠": "defer", "🔴": "nogo"}


def _hero_html(title: str, verdict: str, meta: List[str]) -> str:
    vm = _VERDICT_RE.search(verdict)
    word = vm.group(1) if vm else "—"
    tier = next((c for e, c in _TIER_EMOJI.items() if e in verdict), _TIER_CLASS.get(word, "unknown"))
    sm = _SCORE_RE.search(verdict)
    cm = _COVER_RE.search(verdict)
    fm = _CONF_RE.search(verdict)

    score = f'<div class="score"><b>{sm.group(1)}</b><span>/100</span><em>综合分</em></div>' \
        if sm else ""
    facts = []
    if cm:
        facts.append(f'<div><dt>数据覆盖</dt><dd>{cm.group(1)}%</dd></div>')
    if fm:
        facts.append(f'<div><dt>置信度</dt><dd>{fm.group(1).strip()} <span class="dim">{fm.group(2)}</span></dd></div>')
    fact_html = f'<div class="facts">{"".join(facts)}</div>' if facts else ""

    meta_html = ""
    if meta:
        meta_html = '<div class="meta">' + "".join(f"<div>{_inline(m)}</div>" for m in meta) + "</div>"

    return f"""<header class="hero">
  <div class="hero-main">
    <div class="eyebrow">Amazon 选品裁定书</div>
    <h1>{_inline(title or '选品报告')}</h1>
    <div class="verdict {tier}">{_inline(word)}</div>
    {meta_html}
  </div>
  <aside class="hero-side">
    {score}
    {fact_html}
  </aside>
</header>"""


_CSS = """
*,*::before,*::after{box-sizing:border-box}
:root{
  --paper:#f6f4ef; --card:#fffdf9; --ink:#1b1a17; --ink2:#46423a; --muted:#7d766a;
  --rule:#e2dbcd; --rule2:#efeae0;
  --accent:#155c4c; --accent2:#0e463a; --gold:#a8802c;
  --ok:#2f7a52; --ok-bg:#e7f1ea; --warn:#a9730f; --warn-bg:#f7eed8;
  --bad:#a63b2c; --bad-bg:#f8e6e2; --mut:#877f72; --mut-bg:#eeeae1;
  --serif:"Songti SC","Noto Serif SC","Source Han Serif SC",Georgia,"Times New Roman",serif;
  --sans:"PingFang SC","Hiragino Sans GB","Microsoft YaHei",-apple-system,"Segoe UI",sans-serif;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.85 var(--sans);
  -webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none;border-bottom:1px solid rgba(21,92,76,.3)}
a:hover{border-bottom-color:var(--accent)}
code{font-family:var(--mono);font-size:.86em;background:#efece4;border-radius:3px;padding:.1em .38em;
  color:#3b372f}

/* 版面 */
.shell{max-width:1240px;margin:0 auto;padding:44px 36px 96px;
  display:grid;grid-template-columns:196px minmax(0,880px);gap:0 52px;justify-content:center}
main{grid-column:2;min-width:0}
.toc{grid-column:1;position:sticky;top:36px;align-self:start;font-size:12px}
.toc .t{font-size:10px;letter-spacing:.18em;color:var(--muted);margin-bottom:12px}
.toc a{display:block;padding:5px 0 5px 12px;color:var(--ink2);border:0;border-left:1px solid var(--rule);
  line-height:1.5}
.toc a:hover{color:var(--accent);border-left-color:var(--accent)}
.toc a .n{font-family:var(--mono);font-size:10px;color:var(--muted);margin-right:7px}

/* 页首 */
.hero{display:grid;grid-template-columns:minmax(0,1fr) 190px;gap:32px;align-items:start;
  padding-bottom:30px;border-bottom:2px solid var(--ink);margin-bottom:8px}
.eyebrow{font-size:10px;letter-spacing:.22em;color:var(--accent);text-transform:uppercase;margin-bottom:14px}
.hero h1{font-family:var(--serif);font-size:38px;line-height:1.24;margin:0 0 18px;letter-spacing:-.01em;
  font-weight:600}
.verdict{display:inline-block;font-family:var(--serif);font-size:19px;letter-spacing:.12em;padding:7px 16px;
  border:1px solid currentColor;border-radius:2px}
.verdict.go{color:#226b4a}.verdict.cond{color:#8f6510}.verdict.defer{color:#a35a1b}
.verdict.nogo{color:#9c3a2b}.verdict.unknown{color:var(--muted)}
.meta{margin-top:18px;display:grid;gap:3px;font-size:12px;color:var(--muted)}
.meta code{background:transparent;padding:0;color:var(--ink2)}
.hero-side{text-align:right;border-left:1px solid var(--rule);padding-left:26px}
.score b{font-family:var(--serif);font-size:58px;line-height:1;font-weight:600;letter-spacing:-.03em;
  font-variant-numeric:tabular-nums}
.score span{font-family:var(--mono);font-size:12px;color:var(--muted);margin-left:3px}
.score em{display:block;font-style:normal;font-size:10px;letter-spacing:.18em;color:var(--muted);
  margin-top:8px}
.facts{margin-top:18px;border-top:1px solid var(--rule);padding-top:12px;display:grid;gap:8px}
.facts dt{font-size:10px;letter-spacing:.12em;color:var(--muted)}
.facts dd{margin:1px 0 0;font-family:var(--mono);font-size:13px;color:var(--ink)}
.facts .dim{color:var(--muted)}

/* 章节 */
.sec{padding:34px 0 6px}
.sec + .sec{border-top:1px solid var(--rule2)}
h2{font-family:var(--serif);font-size:23px;font-weight:600;margin:0 0 16px;line-height:1.35;
  letter-spacing:.01em;scroll-margin-top:24px}
h2 .idx{font-family:var(--mono);font-size:10px;color:var(--gold);letter-spacing:.1em;
  margin-right:12px;vertical-align:.28em}

/* 支撑材料：折叠 */
details.fold{padding:22px 0 8px}
details.fold + .sec, .sec + details.fold{border-top:1px solid var(--rule2)}
details.fold > summary{list-style:none;cursor:pointer;font-family:var(--serif);font-size:19px;
  font-weight:600;line-height:1.4;color:var(--ink2);display:flex;align-items:baseline;
  scroll-margin-top:24px}
details.fold > summary::-webkit-details-marker{display:none}
details.fold > summary::before{content:"▸";font-size:12px;color:var(--gold);margin-right:9px;
  transition:transform .15s}
details.fold[open] > summary::before{transform:rotate(90deg)}
details.fold > summary:hover{color:var(--accent)}
details.fold > summary .idx{font-family:var(--mono);font-size:10px;color:var(--gold);letter-spacing:.1em;
  margin-right:12px}
details.fold .fold-hint{margin-left:auto;font-family:var(--sans);font-size:10.5px;font-weight:400;
  letter-spacing:.12em;color:var(--muted)}
details.fold[open] .fold-hint{visibility:hidden}
details.fold[open] > summary{margin-bottom:14px}
details.fold > :not(summary){padding-left:0}
h3.sub{font-family:var(--serif);font-size:16px;font-weight:600;margin:26px 0 8px;color:var(--ink);
  padding-left:12px;border-left:3px solid var(--accent)}
h4.sub{font-size:13px;margin:18px 0 6px;color:var(--ink2)}
p{margin:0 0 13px;color:var(--ink2)}
strong{color:var(--ink);font-weight:600}

/* 表格 */
.table-wrap{overflow-x:auto;margin:8px 0 20px}
table{width:100%;border-collapse:collapse;font-size:13px;line-height:1.6}
.pimg{width:96px;height:96px;object-fit:cover;border-radius:6px;display:block;background:#f3f3f3}
th{text-align:left;font-weight:500;font-size:10px;letter-spacing:.14em;color:var(--muted);
  padding:0 12px 8px;border-bottom:1px solid var(--ink);white-space:nowrap}
td{padding:9px 12px;border-bottom:1px solid var(--rule2);vertical-align:top;color:var(--ink2)}
tbody tr:hover td{background:#faf7f0}
td .num,td.num{font-family:var(--mono);font-variant-numeric:tabular-nums;color:var(--ink);white-space:nowrap}
td .ref,td.ref{font-family:var(--mono);font-size:11px;color:var(--muted);white-space:nowrap}
.pill{display:inline-block;padding:1px 9px;border-radius:99px;font-size:11px;font-weight:600;
  letter-spacing:.04em;white-space:nowrap}
.pill.ok{color:var(--ok);background:var(--ok-bg)}
.pill.warn{color:var(--warn);background:var(--warn-bg)}
.pill.bad{color:var(--bad);background:var(--bad-bg)}
.pill.muted{color:var(--mut);background:var(--mut-bg)}

/* 列表 */
.list{margin:0 0 16px;padding:0;list-style:none}
.list li{position:relative;padding:4px 0 4px 20px;color:var(--ink2)}
.list li::before{content:"";position:absolute;left:5px;top:15px;width:5px;height:5px;border-radius:50%;
  background:var(--accent);opacity:.55}
.list.ordered{counter-reset:o}
.list.ordered li{padding-left:28px}
.list.ordered li::before{counter-increment:o;content:counter(o);width:auto;height:auto;background:none;
  opacity:1;font-family:var(--mono);font-size:11px;color:var(--gold);top:6px;left:2px}
.list li.task::before{display:none}
.list li .sub{display:block;margin-top:3px;font-size:12px;line-height:1.6;color:var(--muted);
  font-family:var(--sans);word-break:break-word}
.list li .box{font-family:var(--mono);color:var(--muted);margin-right:8px}

/* 引用/分隔 */
blockquote{margin:0 0 18px;padding:14px 18px;background:var(--card);border-left:3px solid var(--gold);
  color:var(--ink2);font-size:13.5px}
hr.rule{border:0;border-top:1px solid var(--rule);margin:26px 0}

pre{background:#232019;color:#efe9dc;padding:16px 18px;border-radius:4px;overflow-x:auto;
  font-size:12px;line-height:1.7}
pre code{background:none;color:inherit;padding:0}

/* 页脚 */
.foot{margin-top:52px;padding-top:18px;border-top:2px solid var(--ink);font-size:11px;color:var(--muted);
  display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap}

/* 打印 */
@media print{
  :root{--paper:#fff}
  body{background:#fff;font-size:10.5pt;line-height:1.65}
  .shell{display:block;max-width:none;padding:0}
  .toc{display:none}
  .hero{grid-template-columns:1fr 160px}
  h2{page-break-after:avoid}
  .sec{padding:18px 0 2px}
  table{page-break-inside:auto}
  tr,td,th{page-break-inside:avoid}
  .table-wrap{overflow:visible}
  a{color:inherit;border:0}
  /* 打印时把折叠的支撑材料展开：纸上没有「点一下」，折着就等于没内容 */
  details.fold > *:not(summary){display:block !important}
  details.fold > summary::before{content:""}
  details.fold .fold-hint{display:none}
  @page{size:A4;margin:16mm 14mm}
}

@media (max-width:900px){
  .shell{grid-template-columns:minmax(0,1fr);padding:28px 20px 72px}
  .toc{display:none}
  main{grid-column:1}
  .hero{grid-template-columns:1fr;gap:22px}
  .hero-side{text-align:left;border-left:0;border-top:1px solid var(--rule);padding:18px 0 0}
  .hero h1{font-size:28px}
  .score b{font-size:44px}
}
"""


def render(md: str, *, footer_note: str = "") -> str:
    """markdown 报告 → 完整 HTML 文档。"""
    lines = md.replace("\r\n", "\n").split("\n")
    cut = next((i for i, ln in enumerate(lines) if ln.startswith("## ")), len(lines))
    title, verdict, meta = _parse_head(lines[:cut])
    body, toc = _body_html(lines[cut:])

    toc_html = ""
    if len(toc) >= 3:
        items = "".join(
            f'<a href="#{sid}"><span class="n">{i:02d}</span>{html.escape(t)}</a>'
            for sid, t, i in toc)
        toc_html = f'<nav class="toc"><div class="t">目录</div>{items}</nav>'

    note = footer_note or "结论由阈值规则确定（阈值来源：星空 S1 s1_thresholds.yaml 叠加内置默认），AI 只负责解读。"
    doc_title = f"{title} · 选品裁定" if title else "选品裁定"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(doc_title)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="shell">
{toc_html}
<main>
{_hero_html(title, verdict, meta)}
{body}
<footer class="foot"><span>AI 选品智库 · 选品裁定书</span><span>{html.escape(note)}</span></footer>
</main>
</div>
</body>
</html>
"""
