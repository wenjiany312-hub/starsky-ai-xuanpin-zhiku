"""离线冒烟自检：不调数据源、不调模型、不花任何额度，几秒跑完。

用途：装好之后先跑一遍，确认依赖齐全、内置方法论能加载、判定引擎和报告渲染正常。
    backend/.venv/bin/python -I backend/test_smoke.py        （Windows：backend\\.venv\\Scripts\\python.exe）
全部通过退出码 0；任何一项失败打印原因并退出 1。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


@check("依赖与应用可导入")
def _imports():
    from main import app  # noqa: F401
    import openpyxl, yaml, httpx  # noqa: F401,E401


@check("内置方法论全部能加载、没有写错的阈值")
def _packs():
    from app.service.method_pack_service import method_pack_service
    packs = [p for p in method_pack_service.list() if p["source"] == "builtin"]
    assert len(packs) >= 5, f"内置方法论只加载到 {len(packs)} 套"
    bad = {p["id"]: p["warnings"] for p in packs if p.get("warnings")}
    assert not bad, f"内置方法论有没生效的阈值：{bad}"


@check("没有数据时判成「未研判」而不是「暂缓」")
def _no_data_verdict():
    from app.core import rules, scoring
    outcomes = rules.evaluate({})
    axes = scoring.score_axes(outcomes, {}, None)
    v = scoring.decide({}, outcomes, axes, evidence_ratio=0.0)
    assert v["verdict"] == "未研判", v["verdict"]


@check("估算出来的毛利不触发一票否决")
def _estimated_margin():
    from app.core import facts, scoring, rules
    eco = facts.economics(sale_price=12.0, purchase_cost_cny=40.0)["facts"]
    assert eco.get("economics_estimated"), eco
    t = rules.thresholds()
    blockers = scoring._hard_blockers([], eco, t)
    assert not [b for b in blockers if b["rule"] in ("R11", "R12")], blockers


@check("错误信息里的密钥会被打码")
def _redact():
    from app.core.redact import redact
    out = redact("401 for url 'https://x.com/mcp?key=SECRET1234567890&keyword=cat'")
    assert "SECRET" not in out and "keyword=cat" in out, out


@check("数据源把错误写在正文里时不算取数成功")
def _error_body():
    from app.core.connectors import _error_body
    assert _error_body('{"code":"QUOTA_EXCEEDED","message":"额度用完"}')
    assert _error_body({"code": 200, "data": {"x": 1}}) is None


@check("JSON 文件损坏时回退到上一版，不清空历史")
def _jsonio():
    from app.core.jsonio import read_json, write_json
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.json"
        write_json(p, {"a": 1})
        write_json(p, {"a": 1, "b": 2})
        p.write_text("{broken", encoding="utf-8")
        assert read_json(p, default={}) == {"a": 1}


@check("主文件丢失时从 .bak 恢复任务，不写空覆盖历史")
def _storage_bak_recovery():
    import json
    from app.repository.storage import TaskRepository
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "tasks.json.bak").write_text(
            json.dumps({"T1": {"task_id": "T1", "keyword": "cat"}}), encoding="utf-8")
        repo = TaskRepository(storage_dir=root)
        assert "T1" in repo._read_all_tasks(), "只剩 .bak 时任务历史被清空"
        repo._write_all_tasks({"T1": {"task_id": "T1"}, "T2": {"task_id": "T2"}})
        bak = json.loads((root / "tasks.json.bak").read_text(encoding="utf-8"))
        assert "T1" in bak, "第一次保存把唯一的完好备份覆盖成了空"


@check("数据源读回响应时断线不自动重发（避免付费工具重复扣点）")
def _mcp_no_double_charge():
    import asyncio
    import httpx
    from app.core.mcp_client import MCPHttpClient, MCPRequestError

    def run(exc_factory):
        calls = {"n": 0}

        class Boom:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k):
                calls["n"] += 1
                raise exc_factory()

        real = httpx.AsyncClient
        httpx.AsyncClient = Boom
        try:
            c = MCPHttpClient("sif", "https://example.invalid/mcp")
            c._initialized, c._session_id = True, "s-1"
            try:
                asyncio.run(c.call_tool("market_get_keyword_competition", {"keyword": "cat"}))
            except MCPRequestError as exc:
                return exc, calls["n"]
            raise AssertionError("断线没有报错")
        finally:
            httpx.AsyncClient = real

    err, n = run(lambda: httpx.ReadError("connection reset"))
    assert err.session_expired is False, "读回阶段断线被当成会话失效，会自动重发付费调用"
    assert n == 1, f"读回阶段断线后重发了 {n} 次，付费工具会被重复扣点"
    err2, _ = run(lambda: httpx.ConnectError("unreachable"))
    assert err2.session_expired is True, "连不上数据源时应该允许重连一次"


@check("报告 HTML 能渲染（含中英对照子行）")
def _report():
    from app.core.report_html import render
    html = render("# 测试\n> 结论：**做**\n\n## 用户声音\n- 中文译文\n  原文：English\n")
    assert 'class="sub"' in html and "<h2>" in html


@check("接口路由齐全")
def _routes():
    from main import app
    paths = set(app.openapi().get("paths", {}))
    for need in ("/api/v1/workflows/execute", "/api/v1/method-packs/generate", "/api/v1/system/preflight",
                 "/api/v1/license/status", "/api/v1/license/activate"):
        assert need in paths, f"缺少接口 {need}"


@check("授权码：乱填 / 改过的签名都进不来")
def _license_tamper():
    import base64
    import json as _json
    from app.core import license as lic

    def b64e(b):
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    body = b64e(_json.dumps({"v": 1, "p": "pro", "n": "t", "e": "2099-01-01", "d": "", "i": "2026-11-02"}).encode())
    zero_sig = b64e(b"\x00" * (lic._K))
    for bad in ("", "XZ2." + body + "." + zero_sig, "XZ1." + body, "XZ1." + body + ".!!!"):
        data, error = lic.decode_key(bad)
        assert data is None and error, f"坏码 {bad[:16]}… 被当成有效"
    data, error = lic.decode_key("XZ1." + body + "." + zero_sig)
    assert data is None and "校验" in error, error


@check("试用到期或次数用完 → 拦截跑数，但列表接口仍可读（到期只读）")
def _license_read_only():
    import json as _json
    import os
    from fastapi import HTTPException
    from app.core import license as lic

    old = os.environ.get("AIXP_LICENSE_DIR")
    with tempfile.TemporaryDirectory() as d:
        os.environ["AIXP_LICENSE_DIR"] = d
        lic._device_cache = lic._device_cache  # 设备码与目录无关，不动
        try:
            # 7 天前开始、已跑 0 次 → 到期
            (Path(d) / "trial.json").write_text(_json.dumps(
                {"started_at": "2020-01-01T00:00:00", "runs_used": 0}), encoding="utf-8")
            st = lic.status()
            assert st["state"] == "expired" and not st["can_run"], st
            try:
                lic.require_active("发起选品运行")
                raise AssertionError("试用到期了还能发起运行")
            except HTTPException as exc:
                assert exc.status_code == 402 and "只读" in str(exc.detail) or "记录" in str(exc.detail), exc.detail

            # 今天开始、已跑满 3 次 → 次数用完
            today = __import__("datetime").date.today().isoformat()
            (Path(d) / "trial.json").write_text(_json.dumps(
                {"started_at": today + "T00:00:00", "runs_used": 3}), encoding="utf-8")
            st = lic.status()
            assert st["state"] == "expired" and "3 次" in st["reason"], st

            # 只读接口不受影响：授权状态和运行列表都能打开
            from fastapi.testclient import TestClient
            from main import app
            client = TestClient(app)
            assert client.get("/api/v1/license/status").status_code == 200
            assert client.get("/api/v1/workflows/tasks").status_code == 200
        finally:
            if old is None:
                os.environ.pop("AIXP_LICENSE_DIR", None)
            else:
                os.environ["AIXP_LICENSE_DIR"] = old


@check("发码工具签出的码能验签、能激活、能拒掉别的设备（仅发行方机器）")
def _license_issue_roundtrip():
    import os
    import subprocess
    from app.core import license as lic

    priv = Path(os.environ.get("AIXP_SIGNING_DIR") or (Path.home() / ".aixp-license-signing")) / "private.pem"
    if not priv.exists():
        print("    （本机没有签发私钥，跳过——只对发行方有意义的检查）", end="")
        return
    root = Path(__file__).resolve().parent.parent
    tool = root / "tools" / "license_key.py"
    if not tool.exists():
        return
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "k.txt"
        subprocess.run([sys.executable, "-I", str(tool), "issue", "--name", "自检", "--days", "1",
                        "--any-machine", "--out", str(out)], cwd=str(root), check=True,
                       capture_output=True, text=True)
        key = out.read_text(encoding="utf-8").strip()
        data, error = lic.decode_key(key)
        assert data and data["n"] == "自检", error
        wrong = key[:-2] + ("AA" if key[-2:] != "AA" else "BB")
        assert lic.decode_key(wrong)[0] is None, "改过结尾的码还能用"

        old = os.environ.get("AIXP_LICENSE_DIR")
        os.environ["AIXP_LICENSE_DIR"] = str(Path(d) / "state")
        try:
            st = lic.activate(key)
            assert st["state"] == "licensed" and st["can_run"], st
            lic.require_active("发起选品运行")  # 不抛错
            assert lic.deactivate()["state"] != "licensed"
        finally:
            if old is None:
                os.environ.pop("AIXP_LICENSE_DIR", None)
            else:
                os.environ["AIXP_LICENSE_DIR"] = old

        # 绑到别的设备的码，本机激活必须报错
        out2 = Path(d) / "k2.txt"
        subprocess.run([sys.executable, "-I", str(tool), "issue", "--name", "别人的机", "--days", "1",
                        "--device", "1111-2222-3333", "--out", str(out2)], cwd=str(root), check=True,
                       capture_output=True, text=True)
        os.environ["AIXP_LICENSE_DIR"] = str(Path(d) / "state2")
        try:
            try:
                lic.activate(out2.read_text(encoding="utf-8").strip())
                raise AssertionError("绑定别的设备的码在本机激活成功了")
            except Exception as exc:  # HTTPException(400)
                assert getattr(exc, "status_code", None) == 400, exc
        finally:
            if old is None:
                os.environ.pop("AIXP_LICENSE_DIR", None)
            else:
                os.environ["AIXP_LICENSE_DIR"] = old


@check("软性未过项不管几条只降一档，不重复罚分")
def _soft_fail_one_tier():
    from app.core import scoring
    from app.core.rules import RuleOutcome
    axes = {k: {"score": 4.0, "basis": ""} for k in
            ("market_capacity", "competition", "trend", "differentiation", "margin_barrier")}  # 75 分
    outs = [RuleOutcome(id=f"R0{i}", name="x", axis="competition", status="PASS") for i in range(1, 7)]
    outs += [RuleOutcome(id="R14", name="难度", axis="competition", status="FAIL"),
             RuleOutcome(id="R16", name="y", axis="margin_barrier", status="FAIL")]
    v = scoring.decide({"ip_findings_count": 0}, outs, axes, evidence_ratio=1.0)
    assert v["points"] >= 70 and v["verdict"] == "有条件做", (v["points"], v["verdict"], v["reason"])


@check("六道门：缺数据封顶「观察」、表单字符串不误判、用的是用户填的采购价")
def _six_gates_gates():
    from app.core import six_gates as hk
    # 只有发货风险不过、其余全缺：不能跳到「小批量试款」
    assert hk.from_facts({}, {"max_loss_ok": False})["conclusion"] == "观察"
    # 表单传来的 "false" 不是「可承受」
    g = hk.from_facts({}, {"max_loss_ok": "false"})
    assert next(x for x in g["gates"] if x["index"] == 6)["status"] == "fail", g["gates"]
    # 采购价：用户填的 ¥14.3 优先于 1688 中位价 ¥80
    g = hk.from_facts({"supply_price_cny_median": 80, "sale_price_usd": 25},
                      {"purchase_price_cny": 14.3, "cny_usd_rate": 7.2})
    assert g["inputs"]["cost_cny"] == 14.3 and g["inputs"]["rate"] == 7.2, g["inputs"]
    # 六门齐全全过 → 做
    full = {"ip_reviewed": True, "max_loss_ok": True, "product_reason": "两片装性价比",
            "purchase_price_cny": 10, "head_sales": 3000, "target_orders": 300}
    facts = {"sale_price_usd": 29.99, "fba_fee_usd": 5, "search_volume_trend_pct": 5}
    assert hk.from_facts(facts, full)["conclusion"].startswith("做"), hk.from_facts(facts, full)["reason"]


@check("六道门·方法论口径：运营异常封顶、同特征不足封顶、首批用同特征 S、高成本不再打折")
def _six_gates_method_rules():
    from app.core import six_gates as hk
    facts = {"sale_price_usd": 29.99, "fba_fee_usd": 5, "search_volume_trend_pct": 5}
    full = {"ip_reviewed": True, "max_loss_ok": True, "product_reason": "两片装性价比",
            "purchase_price_cny": 10, "head_sales": 3000, "target_orders": 300}
    # 真实口径：全过的案例，加一个运营异常 → 必须降为观察，且写明原因
    g = hk.from_facts(facts, {**full, "anomalies": "合并评论、烧广告"})
    assert g["conclusion"] == "观察" and "运营异常" in g["reason"], g["reason"]
    assert g["anomalies"] == ["合并评论", "烧广告"], g["anomalies"]
    # 同特征只有 1 个 → 观察、置信度低
    g = hk.from_facts(facts, {**full, "peer_count": 1})
    assert g["conclusion"] == "观察" and g["confidence"] == "低", (g["conclusion"], g["confidence"])
    # 同特征 3 个、全过 → 做，但不会是「高」以外的降级误判；2 个 → 置信度「中」
    assert hk.from_facts(facts, {**full, "peer_count": 3})["conclusion"] == "做"
    assert hk.from_facts(facts, {**full, "peer_count": 2})["confidence"] == "中"
    # 首批：给了同特征 S=100 → 100 × k1 × 2 = 200，且口径写明是 S
    g = hk.from_facts(facts, {**full, "peer_avg_monthly": 100})
    assert g["first_batch"] == 200 and "同特征" in g["first_batch_detail"]["basis"], g["first_batch_detail"]
    # 没给 S：回落目标月销 300 × 2 = 600，并标注偏高
    g = hk.from_facts(facts, full)
    assert g["first_batch"] == 600 and "偏高" in g["first_batch_detail"]["basis"], g["first_batch_detail"]
    # 高成本（采购 ¥100）不再打 ×0.7：方法里没有这条线
    g = hk.from_facts(facts, {**full, "purchase_price_cny": 100, "peer_avg_monthly": 100})
    assert g["first_batch"] == 200 and g["first_batch_detail"]["factor"] == 1.0, g["first_batch_detail"]
    # 无产品原因且未饱和不成立 → 产品原因门 fail → 不做（方法 §4 取舍规则）
    no_reason = {k: v for k, v in full.items() if k != "product_reason"}
    g = hk.from_facts(facts, {**no_reason, "low_review_avg": 2, "ref_reviews_median": 100})
    assert g["conclusion"] == "不做" and "产品原因门" in g["reason"], (g["conclusion"], g["reason"])


@check("数据源路由：市场筛选卖家精灵优先、关键词西柚优先、用户指定可覆盖")
def _source_routing():
    from app.core import connectors as C
    # 内置顺序：六道门的筛选动作卖家精灵优先回落 Sorftime；关键词数据西柚（免费）优先
    assert C.CAPABILITY_ROUTES["market.product_search"][:2] == ["sellersprite", "sorftime"], \
        C.CAPABILITY_ROUTES["market.product_search"]
    assert C.CAPABILITY_ROUTES["market.keyword_trend"][0] == "xydc"
    assert C.CAPABILITY_ROUTES["market.keyword_competition"][0] == "sif", "竞争格局必须 SIF 优先（月销字段只有它有）"
    # 用户设置 > 方法包显式指定 > 内置默认
    assert C.resolve_routes("market.product_search") == ["sellersprite", "sorftime", "xydc"]
    assert C.resolve_routes("market.product_search", prefer=["sorftime"]) == ["sorftime"]
    assert C.resolve_routes("market.product_search", prefer=["不存在的源"]) == ["sellersprite", "sorftime", "xydc"]
    old = C.settings_store

    class _Fake:
        def get(self, *a, **k):
            return {"market.product_search": ["sorftime"], "market.keyword_trend": ["bogus", "sif"]}

    C.settings_store = _Fake()
    try:
        assert C.resolve_routes("market.product_search") == ["sorftime"], "用户指定的路由没有生效"
        # 用户手动指定压过方法包声明：用户的选择最先
        assert C.resolve_routes("market.product_search", prefer=["xydc"]) == ["sorftime"], "用户设置应压过包声明"
        # 写错的连接器 id 被丢掉，只留合法的那部分（["bogus","sif"] → ["sif"]）
        assert C.resolve_routes("market.keyword_trend") == ["sif"], C.resolve_routes("market.keyword_trend")
    finally:
        C.settings_store = old
    # 全部候选都没接入时，错误信息要写明该接谁
    msg = C.missing_source_error("market.product_search", ["sellersprite", "sorftime", "xydc"])
    assert "卖家精灵" in msg and "Sorftime" in msg and "接入" in msg, msg
    # 卖家精灵商品筛选参数：月销降序、词组匹配
    req = C._ss_products({"keyword": "silicone baking mat", "country": "US", "size": 100})["request"]
    assert req["order"] == {"field": "total_units", "desc": True} and req["matchType"] == 1, req


@check("同特征竞品筛查：三源样本解析与六道门上游数字")
def _peer_screen_metrics():
    from app.core import facts as F
    ss = {"data": {"items": [
        {"asin": "A1", "title": "mat 1", "price": 20.0, "rating": 4.6, "ratings": 10,
         "units": 1000, "availableDate": 1735689600000},
        {"asin": "A2", "title": "mat 2", "price": 22.0, "rating": 4.5, "ratings": 150,
         "units": 800},
        {"asin": "A3", "title": "mat 3", "price": 18.0, "rating": 4.4, "ratings": 20,
         "units": 600},
        {"asin": "A4", "title": "mat 4", "price": 25.0, "rating": 4.7, "ratings": 900,
         "units": 100},
    ]}}
    rows = F.screen_rows(ss, "sellersprite")
    assert len(rows) == 4 and rows[0]["monthly_orders"] == 1000 and rows[0]["reviews"] == 10, rows[0]
    d = F.peer_metrics(rows)
    assert d["peer_count"] == 4 and d["peer_avg_monthly"] == 625 and d["head_monthly_sales"] == 1000, d
    assert d["low_review_avg"] == 800 and d["low_review_sellers"] == 2, d          # 评论 ≤30：1000、600
    assert d["ref_reviews_median"] == 800 and d["unsaturation_ref_n"] == 1, d      # 100–200：800
    assert d["review_band_difficulty_pct"] == 128.0 and d["unsaturation_ratio"] == 1.0, d
    cliff = d["peer_cliff"]
    assert cliff["ratio"] == 1.33 and cliff["head_tier"] == 2, cliff               # 800 → 600 断崖
    # Sorftime / 西柚的行结构也认得出（-1 = 无数据要变成缺值）
    st = {"data": [{"asin": "B1", "title": "x", "price": 19.9, "ratings": 4.5, "rating_count": 15,
                    "monthly_sales_volume": 700, "online_date": "2026-01-15"},
                   {"asin": "B2", "monthly_sales_volume": -1}]}
    rows2 = F.screen_rows(st, "sorftime")
    assert len(rows2) == 2 and rows2[1]["monthly_orders"] is None and rows2[0]["listed_age_days"] is not None
    xy = {"data": {"list": [
        {"asin": "C1", "asinInfo": {"price": 21.0, "stars": 4.5, "ratings": 12},
         "asinsTrends": {"periods": [{"period": "last30days", "sales": {"value": 900}}]}},
        {"asin": "C2", "asinInfo": {"price": 21.0, "stars": 4.5, "ratings": 120}}]}}
    rows3 = F.screen_rows(xy, "xydc")
    assert rows3[0]["monthly_orders"] == 900 and rows3[1]["monthly_orders"] is None, rows3
    # peer_params：注入参数并带 peer_auto 口径；build() 里样本指标要压过 SIF 竞争格局的小样本
    src = {"peer_screen": {"payload": ss, "connector": "sellersprite", "eid": "EV-9"},
           "low_review_max": 30, "ref_reviews_min": 100, "ref_reviews_max": 200}
    pp = F.peer_params(src)
    assert pp["peer_count"] == 4 and pp["peer_avg_monthly"] == 625 and pp["peer_auto"] is True, pp
    src["competition"] = {"payload": {"top_competitors": [{"monthly_orders": 500, "review_count": 50,
                                                           "price": 20}]}, "connector": "sif", "eid": "EV-8"}
    facts = F.build(src)["facts"]
    assert facts["head_monthly_sales"] == 1000 and facts["review_band_difficulty_pct"] == 128.0, \
        "100 个样本的口径应压过 SIF top10 的小样本"
    assert facts.get("peer_screen_source", "").startswith("sellersprite")


@check("六道门：自动竞品样本出数但置信度封顶「中」")
def _six_gates_peer_auto():
    from app.core import six_gates as hk
    facts = {"sale_price_usd": 29.99, "fba_fee_usd": 5, "search_volume_trend_pct": 5}
    full = {"ip_reviewed": True, "max_loss_ok": True, "product_reason": "两片装性价比",
            "purchase_price_cny": 10, "head_sales": 3000, "target_orders": 300,
            "peer_count": 100, "peer_avg_monthly": 250, "peer_auto": True}
    g = hk.from_facts(facts, full)
    assert g["conclusion"] == "做" and g["confidence"] == "中", (g["conclusion"], g["confidence"])
    assert any("人工确认" in x for x in g["todo"]), g["todo"]
    # 人工口径（不带 peer_auto）仍是「高」——封顶只针对自动样本
    manual = hk.from_facts(facts, {k: v for k, v in full.items() if k != "peer_auto"})
    assert manual["confidence"] == "高", manual["confidence"]


@check("0 条是事实：Reddit / 1688 查成功但为空，记 0 而不是缺数据")
def _empty_is_fact():
    from app.core import facts as F
    f = F.build({"voc": {"payload": [], "eid": "EV-01", "error": None},
                 "supply": {"payload": [], "eid": "EV-02", "error": None, "query": "x"},
                 "ip": {"payload": None, "eid": "EV-03", "error": "超时"}})["facts"]
    assert f.get("voc_count") == 0 and f.get("has_voc") is False, f
    assert f.get("supply_offers") == 0, f
    assert f.get("ip_findings_count") is None, "失败的专利检索不能记成 0 命中"


@check("没有 ASIN 列的中文竞品表也认成竞品表")
def _upload_cn_competitors():
    from app.service.upload_service import UploadService
    with tempfile.TemporaryDirectory() as d:
        u = UploadService()
        u.dir, u.index = Path(d), Path(d) / "index.json"
        m = u.save("t.csv", "品名,售价,评论数,月销量\n甲,12.99,1102,3000\n乙,17.99,611,900\n".encode("utf-8"))
        assert m["kind"] == "competitors", m
        assert u.save("k.csv", "关键词,周搜索量\ndish mat,3000\n".encode())["kind"] == "keywords"


@check("图片地址只许公网：本机 / 局域网地址直接拒")
def _image_url_public_only():
    import asyncio
    from app.service.image_service import fetch_remote
    for url in ("http://127.0.0.1:8006/api/v1/license/status", "http://localhost:8006/x.jpg",
                "http://198.18.0.1/a.jpg", "http://192.168.1.1/a.jpg",
                "http://169.254.169.254/latest/meta-data/", "ftp://example.com/a.jpg"):
        try:
            asyncio.run(fetch_remote(url))
            raise AssertionError(f"{url} 没被拦")
        except ValueError:
            pass


@check("发布包只带买家文档，不带变现方案 / 路线图 / 发码流程")
def _release_docs():
    import importlib.util
    root = Path(__file__).resolve().parents[1]
    tool = root / "tools" / "package_release.py"
    if not tool.exists():
        print("    （买家包里本来就不带 tools/，跳过——只对发行方源码树有意义）", end="")
        return
    spec = importlib.util.spec_from_file_location("package_release", tool)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    docs = {str(p) for p in mod.collect("mac") if str(p).startswith("docs")}
    for bad in ("变现方案", "roadmap", "授权码", "v3_完善规划", "architecture_blueprint", "分发与安装"):
        assert not any(bad in x for x in docs), (bad, docs)
    assert any("安装与使用" in x for x in docs), docs
    assert not any("license_key" in str(p) or "workspace_navigator" in str(p) for p in mod.collect("windows"))



@check("社媒 VOC 前导选品：方法包加载、三重验证三道门与本地语料匹配")
def _voc_lead_method():
    from app.service.method_pack_service import method_pack_service
    from app.core import voc as voc_mod
    from app.service import voc_service
    # 1. 方法包加载
    pack = method_pack_service.get("voc-reddit-lead")
    assert pack is not None, "未找到 voc-reddit-lead 方法包"
    assert pack.get("decision", {}).get("mode") == "voc", "方法包 decision.mode 必须为 voc"
    assert pack.get("thresholds", {}).get("voc", {}).get("min_mentions") is not None

    # 2. 三道门判定
    facts_pass = {
        "voc_lead": {"ok": True, "mentions": 150, "trend_pct": 15.0, "pain_rate": 0.35, "top_pain_pct": 28.0, "top_pain_name": "挑食拒食"},
        "search_volume_weekly": 8800, "search_volume_trend_pct": 25.0, "top3_click_share": 0.22,
    }
    v_pass = voc_mod.decide(facts_pass)
    assert v_pass["verdict"] == "做", f"全过应判「做」，实际判「{v_pass['verdict']}」"
    assert len(v_pass["gates"]) == 3
    assert all(g["status"] == "pass" for g in v_pass["gates"])

    # 平台门不过 -> 证伪「不做」
    facts_fail = dict(facts_pass, search_volume_weekly=200)  # 周搜 < 300 否决
    v_fail = voc_mod.decide(facts_fail)
    assert v_fail["verdict"] == "不做", f"平台门不及格应判「不做」，实际判「{v_fail['verdict']}」"

    # 3. 本地语料匹配（语料库是各人自己的，发布包不带；没有语料就跳过这一步）
    if not any(d.exists() for d in (voc_service.VOC_DATA_DIR / "pet-us" / "patterns" / "samples",
                                    voc_service.VOC_DIR / "corpus")):
        print("    （本机没有本地 VOC 语料库，跳过语料匹配——语料需自行准备）", end="")
        return
    docs, fname = voc_service.find_local_samples("freeze dried food topper")
    assert docs and len(docs) >= 50, f"应匹配到本地语料样本，实际={fname}"


def main() -> int:
    failed = 0
    for name, fn in CHECKS:
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ✗ {name}：{type(exc).__name__} {exc}")
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} 项通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
