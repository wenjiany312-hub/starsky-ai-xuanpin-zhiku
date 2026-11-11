import os
import sys
from pathlib import Path
from PIL import Image, ImageDraw

def create_selection_icon(output_dir: Path) -> Path:
    """
    动态生成高分辨率的多层级 Windows 专属选品智库图标 (.ico)
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    ico_path = output_dir / "app_icon.ico"
    png_path = output_dir / "app_icon.png"

    # 生成 256x256 高清原图
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 1. 绘制暗黑圆角科技底座
    margin = 12
    draw.rounded_rectangle(
        [margin, margin, size - margin, size - margin],
        radius=48,
        fill=(13, 18, 36, 255),
        outline=(59, 130, 246, 220),
        width=4
    )

    # 2. 绘制星空罗盘微光内外圈
    center = size // 2
    r_outer = 88
    r_inner = 70
    draw.ellipse(
        [center - r_outer, center - r_outer, center + r_outer, center + r_outer],
        outline=(245, 158, 11, 160),
        width=3
    )
    draw.ellipse(
        [center - r_inner, center - r_inner, center + r_inner, center + r_inner],
        outline=(56, 189, 248, 140),
        width=2
    )

    # 3. 绘制核心探索雷达/放大镜镜片与把柄 (金色与天蓝)
    lens_center_x = center - 14
    lens_center_y = center - 14
    lens_r = 44
    # 镜片发光填充
    draw.ellipse(
        [lens_center_x - lens_r, lens_center_y - lens_r, lens_center_x + lens_r, lens_center_y + lens_r],
        fill=(37, 99, 235, 120),
        outline=(245, 158, 11, 255),
        width=5
    )
    # 放大镜手柄 (斜向右下)
    draw.line(
        [(lens_center_x + 32, lens_center_y + 32), (center + 68, center + 68)],
        fill=(245, 158, 11, 255),
        width=10
    )

    # 4. 绘制 AI 闪烁四角星 (代表 AI 选品智库核心算力)
    star_x = lens_center_x
    star_y = lens_center_y
    star_r = 18
    # 绘制星光十字与菱形
    star_points = [
        (star_x, star_y - star_r),
        (star_x + 5, star_y - 5),
        (star_x + star_r, star_y),
        (star_x + 5, star_y + 5),
        (star_x, star_y + star_r),
        (star_x - 5, star_y + 5),
        (star_x - star_r, star_y),
        (star_x - 5, star_y - 5)
    ]
    draw.polygon(star_points, fill=(255, 255, 255, 255))
    draw.ellipse(
        [star_x - 4, star_y - 4, star_x + 4, star_y + 4],
        fill=(56, 189, 248, 255)
    )

    # 保存高清 PNG
    img.save(png_path, format="PNG")

    # 生成多尺寸标准 Windows .ico (256, 128, 64, 48, 32, 16)
    icon_sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    img.save(ico_path, format="ICO", sizes=icon_sizes)
    print(f"[OK] 选品智库专属图标已生成: {ico_path}")
    return ico_path

if __name__ == "__main__":
    out_dir = Path(__file__).resolve().parent / "assets"
    create_selection_icon(out_dir)
