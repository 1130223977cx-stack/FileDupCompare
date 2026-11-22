# -*- coding: utf-8 -*-
"""把原型里的示意图压到适合入仓库的尺寸（长边 1600、质量 82）。"""
import os
import os.path as osp
from PIL import Image

base = osp.join(osp.dirname(osp.dirname(osp.abspath(__file__))), "docs", "prototype", "assets")
for name in sorted(os.listdir(base)):
    if not name.lower().endswith((".jpg", ".jpeg")):
        continue
    p = osp.join(base, name)
    before = osp.getsize(p) / 1048576
    im = Image.open(p).convert("RGB")
    w, h = im.size
    if max(w, h) > 1600:
        r = 1600 / max(w, h)
        im = im.resize((int(w * r), int(h * r)), Image.LANCZOS)
    im.save(p, "JPEG", quality=82, optimize=True)
    print(f"{name}: {before:.1f}MB -> {osp.getsize(p) / 1048576:.2f}MB")
