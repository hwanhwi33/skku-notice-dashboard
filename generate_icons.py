"""
PWA 아이콘 생성 스크립트
실행: python generate_icons.py
static/ 폴더에 아이콘 파일들이 생성됩니다.
"""
import os

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("Pillow가 필요합니다: pip install Pillow")
    exit(1)

SIZES = [72, 96, 128, 144, 152, 192, 384, 512]
OUTPUT_DIR = "static"
os.makedirs(OUTPUT_DIR, exist_ok=True)

for size in SIZES:
    img = Image.new('RGBA', (size, size), (0, 62, 33, 255))  # #003e21
    draw = ImageDraw.Draw(img)

    # 중앙에 "성" 글자 (또는 원형 배경)
    circle_margin = size // 8
    draw.ellipse(
        [circle_margin, circle_margin, size - circle_margin, size - circle_margin],
        fill=(255, 255, 255, 255)
    )

    # 글자 크기 조절
    font_size = size // 3
    try:
        # 시스템에 한글 폰트가 있으면 사용
        font = ImageFont.truetype("/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc", font_size)
    except (OSError, IOError):
        try:
            font = ImageFont.truetype("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", font_size)
        except (OSError, IOError):
            font = ImageFont.load_default()

    text = "성대"
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    text_x = (size - text_w) // 2
    text_y = (size - text_h) // 2 - bbox[1]
    draw.text((text_x, text_y), text, fill=(0, 62, 33, 255), font=font)

    filepath = os.path.join(OUTPUT_DIR, f"icon-{size}.png")
    img.save(filepath, "PNG")
    print(f"✅ 생성됨: {filepath}")

print(f"\n총 {len(SIZES)}개 아이콘이 {OUTPUT_DIR}/ 폴더에 생성되었습니다.")
