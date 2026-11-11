"""
自有数据上传：CSV / XLSX（Sorftime、卖家精灵、西柚导出，或自己整理的竞品表）。

只做表头识别与数值清洗，转换成和 SIF 竞品列表同形的记录，交给同一套事实层与规则。
识别不出的列不会被猜；文件原样保存在 data_storage/uploads，可回溯。
"""
import csv
import datetime
import io
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.facts import _f, clean_asin
from app.core.jsonio import lock_for, read_json, write_json

_MAX_BYTES = 15 * 1024 * 1024
_MAX_ROWS = 5000

# 统一字段 → 常见表头（中英混合，小写匹配，去空格）
_ALIASES: Dict[str, List[str]] = {
    "asin": ["asin", "子asin", "父asin", "商品asin"],
    "title": ["title", "标题", "商品标题", "产品标题", "product title", "品名", "商品名称", "产品名称", "商品名"],
    "brand": ["brand", "品牌"],
    "price": ["price", "价格", "售价", "价格($)", "price($)", "价格(美元)", "buybox价格"],
    "review_count": ["reviews", "review count", "ratings", "评论数", "评分数", "评价数", "review_count", "ratings count"],
    "rating": ["rating", "star", "stars", "评分", "星级"],
    "monthly_orders": ["monthly sales", "月销量", "月销", "近30天销量", "monthly_orders", "sales", "销量", "子体销量"],
    "listed_date": ["上架时间", "上架日期", "available date", "first available", "date first available", "listing date"],
    "listed_age_days": ["上架天数", "listed_age_days", "days on market"],
    "keyword": ["keyword", "关键词", "搜索词", "search term"],
    "search_volume": ["搜索量", "月搜索量", "周搜索量", "search volume", "searches"],
}


def _norm(h: Any) -> str:
    return re.sub(r"\s+", "", str(h or "")).lower()


def _map_headers(headers: List[Any]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    normed = [_norm(h) for h in headers]
    for field, names in _ALIASES.items():
        targets = [_norm(n) for n in names]
        for i, h in enumerate(normed):
            if h in targets or any(h.startswith(t) for t in targets if len(t) >= 3):
                out.setdefault(field, i)
                break
    return out


def _rows_from_bytes(name: str, data: bytes) -> List[List[Any]]:
    suffix = Path(name).suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        rows = [list(r) for _, r in zip(range(_MAX_ROWS + 50), ws.iter_rows(values_only=True))]
        wb.close()
        return rows
    if suffix in (".csv", ".tsv", ".txt"):
        text = None
        for enc in ("utf-8-sig", "gb18030", "utf-16"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ValueError("无法识别文件编码")
        dialect = csv.excel_tab if suffix == ".tsv" or text.count("\t") > text.count(",") else csv.excel
        return [r for _, r in zip(range(_MAX_ROWS + 50), csv.reader(io.StringIO(text), dialect))]
    raise ValueError("只支持 .csv / .tsv / .xlsx")


def _header_row(rows: List[List[Any]]) -> int:
    """导出表常有标题/说明行：取前 10 行里能识别字段最多的一行作表头。"""
    best, idx = -1, 0
    for i, r in enumerate(rows[:10]):
        n = len(_map_headers(r))
        if n > best:
            best, idx = n, i
    return idx


class UploadService:
    def __init__(self):
        self.dir = settings.UPLOADS_DIR
        self.index = self.dir / "index.json"

    def save(self, filename: str, data: bytes) -> Dict[str, Any]:
        if len(data) > _MAX_BYTES:
            raise ValueError("文件超过 15MB")
        safe = re.sub(r"[^\w.\-一-鿿]+", "_", Path(filename).name)[:80] or "upload.csv"
        rows = _rows_from_bytes(safe, data)
        if len(rows) < 2:
            raise ValueError("表格是空的")
        h = _header_row(rows)
        mapping = _map_headers(rows[h])
        if not mapping:
            raise ValueError("没识别出任何已知列（需要 ASIN/价格/评论数/月销量 等表头）")
        records: List[Dict[str, Any]] = []
        today = datetime.date.today()
        for r in rows[h + 1:h + 1 + _MAX_ROWS]:
            if not any(c not in (None, "") for c in r):
                continue
            rec: Dict[str, Any] = {}
            for field, i in mapping.items():
                v = r[i] if i < len(r) else None
                if field in ("asin",):
                    rec[field] = clean_asin(v)
                elif field in ("title", "brand", "keyword"):
                    rec[field] = str(v).strip() if v not in (None, "") else None
                elif field == "listed_date":
                    d = v if isinstance(v, (datetime.date, datetime.datetime)) else None
                    if d is None and v:
                        m = re.search(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", str(v))
                        if m:
                            d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                    if d:
                        d = d.date() if isinstance(d, datetime.datetime) else d
                        rec["listed_age_days"] = (today - d).days
                else:
                    rec[field] = _f(v)
            if any(rec.get(k) is not None for k in ("asin", "price", "keyword")):
                records.append(rec)
        uid = f"UP-{datetime.datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:4]}"
        (self.dir / uid).mkdir(parents=True, exist_ok=True)
        (self.dir / uid / safe).write_bytes(data)
        # 有 ASIN 必是竞品表；没 ASIN 但有价格/评论/销量、又没有关键词列的，也是竞品表
        # （国内卖家自己整理的表常只有「品名/售价/评论数/月销量」，判成关键词表会被运行静默丢掉）
        has_comp_cols = any(f in mapping for f in ("price", "review_count", "monthly_orders"))
        is_comp = any(r.get("asin") for r in records) or (has_comp_cols and "keyword" not in mapping)
        kind = "competitors" if is_comp else "keywords"
        meta = {"id": uid, "filename": safe, "rows": len(records), "kind": kind,
                "columns": {k: str(rows[h][i]) for k, i in mapping.items()},
                "uploaded_at": datetime.datetime.now().isoformat(timespec="seconds")}
        write_json(self.dir / uid / "records.json", {"meta": meta, "records": records})
        with lock_for(self.index):  # 读-改-写要在同一把锁里，并发上传才不会丢索引
            idx = read_json(self.index, default=[]) or []
            idx.insert(0, meta)
            write_json(self.index, idx[:200])
        return meta

    def list(self) -> List[Dict[str, Any]]:
        return read_json(self.index, default=[]) or []

    def load(self, upload_id: str) -> Optional[Dict[str, Any]]:
        if not re.fullmatch(r"UP-[\w\-]+", upload_id or ""):
            return None
        return read_json(self.dir / upload_id / "records.json", default=None)

    def as_competition_payload(self, upload_id: str) -> Optional[Dict[str, Any]]:
        """转换成与 SIF market_get_keyword_competition 同形的结构，复用同一套解析与规则。"""
        data = self.load(upload_id)
        if not data or data["meta"]["kind"] != "competitors":
            return None
        # 上传表没有自然排名，用月销量降序近似“头部”
        recs = sorted(data["records"], key=lambda r: -(r.get("monthly_orders") or 0))
        return {"source": "upload", "upload_id": upload_id,
                "market_structure": {"visible_asin_count": None},
                "top_competitors": [{k: r.get(k) for k in ("asin", "price", "rating", "review_count",
                                                            "monthly_orders", "listed_age_days")} for r in recs]}


upload_service = UploadService()
