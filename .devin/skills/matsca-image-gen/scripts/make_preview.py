#!/usr/bin/env python3
"""把生成的大图压成回传预览：等比缩到最长边 ≤max-px，存 JPEG（默认 q82）。

为什么要它：原图 1024² PNG 约 1.7MB/张，走隧道分块回传偏大、嵌网页也重；
压成 ≤900px、q≈82 的 JPEG 后约 50–160KB（缩 ~10–30 倍），肉眼几乎无差，
回传/预览快得多。**原图照旧保留**，预览只是回传/嵌页用的轻量副本。

后端优先 Pillow，无则回退 ImageMagick `convert`；两者都没有时给出清晰报错。

用法：
  python make_preview.py <图或目录> [...] [--outdir preview] [--max-px 900] [--quality 82]
不传 --outdir 时默认落在每张图所在目录的 ./preview/ 下。
"""
import argparse
import os
import shutil
import subprocess
import sys

IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")


def target_size(w, h, max_px):
    """等比缩放使最长边 ≤max_px；本就不超就原样返回。纯函数，便于离线自测。"""
    m = max(w, h)
    if m <= max_px:
        return w, h
    s = max_px / float(m)
    return max(1, round(w * s)), max(1, round(h * s))


def _with_pillow(src, dst, max_px, quality):
    try:
        from PIL import Image
    except ImportError:
        return False
    im = Image.open(src).convert("RGB")
    w, h = target_size(im.size[0], im.size[1], max_px)
    if (w, h) != im.size:
        im = im.resize((w, h), Image.LANCZOS)
    im.save(dst, "JPEG", quality=quality, optimize=True)
    return True


def _with_convert(src, dst, max_px, quality):
    exe = shutil.which("convert") or shutil.which("magick")
    if not exe:
        return False
    cmd = [exe, src, "-resize", "%dx%d>" % (max_px, max_px),
           "-quality", str(quality), dst]
    return subprocess.run(cmd).returncode == 0


def make_preview(src, dst, max_px=900, quality=82):
    """压一张图到 dst（JPEG）。返回 dst；无可用后端则抛 RuntimeError。"""
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    if _with_pillow(src, dst, max_px, quality):
        return dst
    if _with_convert(src, dst, max_px, quality):
        return dst
    raise RuntimeError("需要 Pillow 或 ImageMagick(convert) 才能压缩预览；两者都没装")


def iter_images(paths):
    for p in paths:
        if os.path.isdir(p):
            for n in sorted(os.listdir(p)):
                if n.lower().endswith(IMG_EXT):
                    yield os.path.join(p, n)
        elif p.lower().endswith(IMG_EXT):
            yield p


def main():
    ap = argparse.ArgumentParser(description="生成回传用的压缩 JPEG 预览（原图保留）")
    ap.add_argument("paths", nargs="+", help="图片文件或目录（目录则取其中所有图）")
    ap.add_argument("--outdir", default=None, help="预览输出目录（默认各图同级 ./preview/）")
    ap.add_argument("--max-px", dest="max_px", type=int, default=900, help="最长边像素上限")
    ap.add_argument("--quality", type=int, default=82, help="JPEG 质量 1-95")
    a = ap.parse_args()
    n = 0
    for src in iter_images(a.paths):
        outdir = a.outdir or os.path.join(os.path.dirname(src) or ".", "preview")
        base = os.path.splitext(os.path.basename(src))[0] + ".jpg"
        dst = os.path.join(outdir, base)
        make_preview(src, dst, a.max_px, a.quality)
        print("%s -> %s (%dKB)" % (src, dst, os.path.getsize(dst) // 1024))
        n += 1
    if not n:
        sys.exit("没有可处理的图片")


if __name__ == "__main__":
    main()
