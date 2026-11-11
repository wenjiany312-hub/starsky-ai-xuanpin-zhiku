"""本地 OCR 封装（PaddleOCR）。

未安装 paddleocr 时优雅降级：is_available() 返回 False，调用方应跳过 OCR。
PaddleOCR 实例懒加载并复用（首次约 5-10s 加载模型，后续每图 1-3s）。
"""
import os

# 关闭 oneDNN，绕开 paddlepaddle 3.x 在 Windows 上的 PIR 执行器 bug
# (NotImplementedError: ConvertPirAttribute2RuntimeAttribute not support ...)
# 必须在 import paddle 之前设置。
os.environ.setdefault("FLAGS_use_mkldnn", "0")
os.environ.setdefault("FLAGS_use_cinn", "0")


_OCR_INSTANCE = None
_OCR_INIT_FAILED = False


def is_available() -> bool:
    """paddleocr 是否能用。装了就 True，没装/导入失败就 False。"""
    global _OCR_INIT_FAILED
    if _OCR_INIT_FAILED:
        return False
    try:
        import paddleocr  # noqa: F401
        return True
    except Exception:
        _OCR_INIT_FAILED = True
        return False


def _get_ocr(lang: str = "ch"):
    """获取 / 创建单例 OCR 实例。失败返回 None。"""
    global _OCR_INSTANCE, _OCR_INIT_FAILED
    if _OCR_INSTANCE is not None:
        return _OCR_INSTANCE
    if _OCR_INIT_FAILED:
        return None
    try:
        from paddleocr import PaddleOCR
        # 关键参数说明：
        # enable_mkldnn=False        绕开 paddle 3.x 在 Win 上的 PIR/onednn bug
        # use_doc_orientation_classify=False  关掉文档方向分类器，否则会把 1688
        #                            平面长图误判成 180° 旋转，导致检测框全空
        # use_doc_unwarping=False    关掉文档去扭曲，否则会把平面规格图"展平"扭曲
        try:
            _OCR_INSTANCE = PaddleOCR(
                lang=lang,
                enable_mkldnn=False,
                use_textline_orientation=False,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
            )
        except TypeError:
            # 老版本 PaddleOCR API（无这些 doc 预处理参数）
            try:
                _OCR_INSTANCE = PaddleOCR(
                    use_angle_cls=False, lang=lang, enable_mkldnn=False
                )
            except TypeError:
                _OCR_INSTANCE = PaddleOCR(use_angle_cls=False, lang=lang)
        return _OCR_INSTANCE
    except Exception:
        _OCR_INIT_FAILED = True
        return None


def _collect_text(result) -> list:
    """从 PaddleOCR 各版本输出里抽出 (text, score)。"""
    texts = []
    if result is None:
        return texts
    if isinstance(result, dict):
        rec_texts = result.get("rec_texts") or []
        rec_scores = result.get("rec_scores") or []
        for i, text in enumerate(rec_texts):
            if text:
                score = rec_scores[i] if i < len(rec_scores) else None
                texts.append((text, score))
        return texts
    if isinstance(result, list):
        for item in result:
            if isinstance(item, dict):
                texts.extend(_collect_text(item))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                payload = item[1]
                if isinstance(payload, (list, tuple)) and payload:
                    text = payload[0]
                    score = payload[1] if len(payload) > 1 else None
                    if text:
                        texts.append((text, score))
                elif isinstance(payload, str):
                    texts.append((payload, None))
    return texts


def _read_image_as_array(image_path: str):
    """用 PIL 读图，绕开 PaddleOCR 在 Windows 中文路径上的坑。"""
    try:
        import numpy as np
        from PIL import Image
        img = Image.open(image_path)
        if img.mode != "RGB":
            img = img.convert("RGB")
        # PaddleOCR 期望 BGR (cv2 风格)，PIL 是 RGB
        return np.array(img)[:, :, ::-1].copy()
    except Exception:
        return None


def ocr_image(image_path: str, min_score: float = 0.0) -> str:
    """OCR 单张图，返回拼接好的多行文字。失败返回 ""。"""
    ocr = _get_ocr()
    if ocr is None or not os.path.exists(image_path):
        return ""
    # Windows 中文路径喂给 paddle 底层 C++ 会触发"用提供的模式无法找到文件"，
    # 改成先用 PIL 读成 numpy array 再喂。
    img_array = _read_image_as_array(image_path)
    if img_array is None:
        return ""
    try:
        if hasattr(ocr, "predict"):
            result = ocr.predict(img_array)
        else:
            result = ocr.ocr(img_array, cls=True)
        lines = []
        for text, score in _collect_text(result):
            if score is not None and score < min_score:
                continue
            t = str(text).strip()
            if t:
                lines.append(t)
        return "\n".join(lines)
    except Exception:
        return ""


def ocr_spec_images(image_paths, max_images: int = 12) -> dict:
    """批量 OCR 详情图，返回 {path: 文本}。

    1688 卖家详情图布局：前 N 张卖点话术 + 末尾 1-2 张产品参数 / 箱规表。
    所以采用"前 8 张 + 末 4 张"双端覆盖策略，避免漏掉压在最后的规格表。
    总数 ≤ max_images 时全 OCR；超过时取前 8 + 后 max_images-8。
    """
    if not is_available():
        return {}
    paths = sorted([p for p in (image_paths or []) if "detail" in p.lower()])
    if not paths:
        return {}
    if len(paths) <= max_images:
        targets = paths
    else:
        head = paths[:8]
        tail = paths[-(max_images - 8):]
        targets = head + tail
    recognized = {}
    for path in targets:
        text = ocr_image(path)
        if text:
            recognized[path] = text
    return recognized
