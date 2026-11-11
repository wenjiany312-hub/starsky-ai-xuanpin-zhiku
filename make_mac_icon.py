# -*- coding: utf-8 -*-
"""
生成 macOS 风格应用图标 v2（1024px）
设计：琥珀金斜向渐变底 + 白色放大镜（选品检索）+ 镜片内上涨柱状图（数据研判）+ 星光点缀
细节：内描边高级感 / 玻璃镜面高光 / 元素投影 / 星光辉光
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter


def sparkle(draw, cx, cy, r, color, ratio=0.28):
    k = r * ratio
    pts = [
        (cx, cy - r), (cx + k, cy - k), (cx + r, cy), (cx + k, cy + k),
        (cx, cy + r), (cx - k, cy + k), (cx - r, cy), (cx - k, cy - k),
    ]
    draw.polygon(pts, fill=color)


def create_mac_icon(out_png: Path, size: int = 1024) -> Path:
    S = size
    final = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # 圆角方形蒙版（macOS 比例 22.5%）
    radius = int(S * 0.225)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S, S], radius=radius, fill=255)

    # ---- 1. 背景：斜向渐变 琥珀金 → 深橙红 ----
    bg = Image.new("RGBA", (S, S))
    c_tl = (253, 199, 61)    # 亮琥珀
    c_br = (221, 73, 14)     # 深橙红
    px = bg.load()
    for y in range(S):
        for x in range(0, S, 4):  # 步进4加速，后面模糊抹平
            t = (x + y) / (2 * S)
            row = tuple(int(c_tl[i] + (c_br[i] - c_tl[i]) * t) for i in range(3)) + (255,)
            for dx in range(4):
                if x + dx < S:
                    px[x + dx, y] = row
    bg = bg.filter(ImageFilter.GaussianBlur(3))

    # 顶部径向柔光（舞台感）
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([S * 0.05, -S * 0.4, S * 0.95, S * 0.5], fill=(255, 241, 199, 90))
    glow = glow.filter(ImageFilter.GaussianBlur(60))
    bg = Image.alpha_composite(bg, glow)

    final.paste(bg, (0, 0), mask)

    # ---- 2. 内描边（Apple 高级感） ----
    ring = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(ring).rounded_rectangle(
        [S * 0.022, S * 0.022, S * 0.978, S * 0.978],
        radius=int(radius * 0.96),
        outline=(255, 255, 255, 80), width=int(S * 0.012))
    final = Image.alpha_composite(final, ring)

    work = final.copy()

    # ---- 3. 元素投影层 ----
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    # 放大镜位置
    lens_cx, lens_cy, lens_r = S * 0.445, S * 0.415, S * 0.235
    sd.ellipse([lens_cx - lens_r, lens_cy - lens_r, lens_cx + lens_r, lens_cy + lens_r],
               fill=(120, 40, 0, 110))
    hx1, hy1 = lens_cx + lens_r * 0.72, lens_cy + lens_r * 0.72
    hx2, hy2 = lens_cx + lens_r * 1.85, lens_cy + lens_r * 1.85
    sd.line([(hx1, hy1), (hx2, hy2)], fill=(120, 40, 0, 110), width=int(S * 0.085))
    sd.ellipse([hx2 - S * 0.042, hy2 - S * 0.042, hx2 + S * 0.042, hy2 + S * 0.042],
               fill=(120, 40, 0, 110))
    shadow = shadow.filter(ImageFilter.GaussianBlur(int(S * 0.028)))
    shadow = shadow.transform((S, S), Image.AFFINE, (1, 0.06, -S * 0.02, 0, 1, S * 0.03))
    work = Image.alpha_composite(work, shadow)

    # ---- 4. 放大镜 ----
    mg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(mg)

    # 4.1 手柄（白色，圆头）
    hw = int(S * 0.085)
    d.line([(hx1, hy1), (hx2, hy2)], fill=(255, 255, 255, 255), width=hw)
    d.ellipse([hx2 - hw / 2, hy2 - hw / 2, hx2 + hw / 2, hy2 + hw / 2], fill=(255, 255, 255, 255))

    # 4.2 镜片磨砂玻璃底
    lens_box = [lens_cx - lens_r, lens_cy - lens_r, lens_cx + lens_r, lens_cy + lens_r]
    d.ellipse(lens_box, fill=(255, 250, 235, 88))

    # 4.3 镜片内：上涨柱状图（白色，剪裁进镜片）
    bars = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bars)
    bw = S * 0.066
    gap = S * 0.034
    base_y = lens_cy + lens_r * 0.46
    heights = [S * 0.115, S * 0.175, S * 0.245]
    x0 = lens_cx - (bw * 3 + gap * 2) / 2
    for i, h in enumerate(heights):
        bx = x0 + i * (bw + gap)
        bd.rounded_rectangle([bx, base_y - h, bx + bw, base_y],
                             radius=int(bw * 0.32), fill=(255, 255, 255, 250))
    # 剪裁进镜片
    lens_mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(lens_mask).ellipse(
        [lens_cx - lens_r * 0.92, lens_cy - lens_r * 0.92, lens_cx + lens_r * 0.92, lens_cy + lens_r * 0.92],
        fill=255)
    mg.paste(bars, (0, 0), Image.composite(bars.split()[3], Image.new("L", (S, S), 0), lens_mask))

    # 4.4 玻璃高光弧（左上）
    d.arc([lens_cx - lens_r * 0.78, lens_cy - lens_r * 0.78, lens_cx + lens_r * 0.78, lens_cy + lens_r * 0.78],
          start=200, end=305, fill=(255, 255, 255, 200), width=int(S * 0.020))

    # 4.5 镜圈（白色粗边 + 内侧淡金描边）
    d.ellipse(lens_box, outline=(255, 255, 255, 255), width=int(S * 0.044))
    d.ellipse([lens_cx - lens_r * 0.90, lens_cy - lens_r * 0.90, lens_cx + lens_r * 0.90, lens_cy + lens_r * 0.90],
              outline=(253, 230, 138, 160), width=int(S * 0.008))

    work = Image.alpha_composite(work, mg)

    # ---- 5. 星光（辉光 + 本体） ----
    glow2 = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sparkle(ImageDraw.Draw(glow2), S * 0.775, S * 0.245, S * 0.105, (255, 255, 255, 170))
    sparkle(ImageDraw.Draw(glow2), S * 0.265, S * 0.71, S * 0.052, (255, 255, 255, 130))
    glow2 = glow2.filter(ImageFilter.GaussianBlur(int(S * 0.016)))
    work = Image.alpha_composite(work, glow2)

    fg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    fd = ImageDraw.Draw(fg)
    sparkle(fd, S * 0.775, S * 0.245, S * 0.075, (255, 255, 255, 250))
    sparkle(fd, S * 0.265, S * 0.71, S * 0.038, (255, 255, 255, 220))
    # 星光中心蓝点
    fd.ellipse([S * 0.775 - S * 0.012, S * 0.245 - S * 0.012, S * 0.775 + S * 0.012, S * 0.245 + S * 0.012],
               fill=(14, 165, 233, 255))
    work = Image.alpha_composite(work, fg)

    # ---- 6. 回贴圆角蒙版 ----
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(work, (0, 0), mask)
    out.save(out_png, format="PNG")
    print(f"[OK] macOS 图标 v2 已生成: {out_png}")
    return out_png


if __name__ == "__main__":
    out = Path(__file__).resolve().parent / "assets" / "app_icon_mac.png"
    create_mac_icon(out)
