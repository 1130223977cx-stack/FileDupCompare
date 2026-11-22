# -*- coding: utf-8 -*-
"""生成 README 用截图：造一批像样的照片库数据（真实 JPEG 缩略图），完整扫描后抓图。"""
import os
import os.path as osp
import random
import shutil
import sys
import time

sys.path.insert(0, osp.dirname(osp.dirname(osp.abspath(__file__))))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
import main as M

HERE = osp.dirname(osp.dirname(osp.abspath(__file__)))
ASSETS = osp.join(osp.dirname(osp.abspath(__file__)), "assets_demo")
OUT = osp.join(HERE, "docs", "screenshots")
DEMO = osp.join(os.environ.get("TEMP", "/tmp"), "cmp_shot_demo")

PHOTOS = [("beach.jpg", "青岛海边全家照"), ("kite.jpg", "潍坊风筝会人群"),
          ("drawing.jpg", "儿童蜡笔画"), ("graduate.jpg", "学士服合影"),
          ("assembly.jpg", "总装线看板"), ("debug.jpg", "控制柜调试记录")]


def make_jpeg(src, dst, pad_kb, salt):
    """复制真实 JPEG 并在尾部追加不同数据：哈希不同，缩略图仍能正常解码。"""
    with open(src, "rb") as f:
        data = f.read()
    tail = (b"\x00PADDIN" + bytes([salt % 251])) * (pad_kb * 4)
    with open(dst, "wb") as f:
        f.write(data + tail)


def build(root):
    a = osp.join(root, "A", "2023-照片备份")
    b = osp.join(root, "B", "2023-照片备份")
    a_cam, a_vid = osp.join(a, "相机胶卷"), osp.join(a, "视频")
    b_cam, b_old = osp.join(b, "相机胶卷"), osp.join(b, "旧手机备份")
    for p in (a_cam, a_vid, b_cam, b_old):
        os.makedirs(p, exist_ok=True)

    random.seed(7)
    combos = [(PHOTOS[i % len(PHOTOS)], i) for i in range(60)]
    random.shuffle(combos)
    # 60 组两边都有的照片（字节完全相同 → 同哈希）
    for i, ((src, tag), _j) in enumerate(combos):
        n = f"IMG_2023{i:04d}_{tag}_{i:03d}.jpg"
        pad = random.choice([180, 320, 640, 980, 1500, 2400])
        make_jpeg(osp.join(ASSETS, src), osp.join(a_cam, n), pad, i + 1)
        make_jpeg(osp.join(ASSETS, src), osp.join(b_cam, n), pad, i + 1)
    # 25 张只在 A 侧（还没备份）
    for i in range(25):
        src, tag = PHOTOS[(i + 2) % len(PHOTOS)]
        n = f"IMG_2024{i:04d}_{tag}_未备份_{i:02d}.jpg"
        make_jpeg(osp.join(ASSETS, src), osp.join(a_cam, n), random.choice([220, 700, 1600]), 500 + i)
    # 10 张只在 B 侧旧机备份里
    for i in range(10):
        src, tag = PHOTOS[(i + 1) % len(PHOTOS)]
        n = f"IMG_2017{1000 + i}_{tag}_旧机_{i:02d}.jpg"
        make_jpeg(osp.join(ASSETS, src), osp.join(b_old, n), random.choice([300, 900]), 900 + i)
    # 两个视频：展示「视频 → 类型块 + 时长」的缩略图退化形态
    for name, mb in (("总装线_看板.mp4", 40), ("质量周会_录屏.mp4", 24)):
        with open(osp.join(a_vid, name), "wb") as f:
            f.write(b"\x00" * (mb * 1024 * 1024))
    return a, b


def pump(app, seconds=0.0):
    t0 = time.time()
    while True:
        app.processEvents()
        if time.time() - t0 >= seconds:
            return
        time.sleep(0.01)


def grab(w, path):
    w.view.viewport().repaint()
    w.band.repaint()
    w.repaint()
    for _ in range(20):
        QApplication.processEvents()
    w.grab().save(path)
    print("saved", path)


def main():
    app = QApplication(sys.argv)
    os.makedirs(OUT, exist_ok=True)
    if osp.isdir(DEMO):
        shutil.rmtree(DEMO)
    os.makedirs(DEMO, exist_ok=True)
    a, b = build(DEMO)

    w = M.MainWindow()
    w.move(30, 30)
    w.resize(1340, 1000)
    w.setAttribute(Qt.WA_DontShowOnScreen, True)
    w.show()
    w.path_a.setText(a)
    w.path_b.setText(b)
    app.processEvents()

    w._start_scan()
    t0 = time.time()
    while time.time() - t0 < 120:
        app.processEvents()
        if w.btn_scan.text() == "重新比对":
            break
        time.sleep(0.05)
    app.processEvents()
    pump(app, 1.2)

    grab(w, osp.join(OUT, "01-overview.png"))

    w.band.set_collapsed(True)
    w.split.setSizes([7000, 520])
    pump(app, 0.5)
    grab(w, osp.join(OUT, "02-collapsed-log.png"))

    w.band.set_collapsed(False)
    w.split.setSizes([9000, 140])
    pump(app, 0.4)
    w.view.verticalScrollBar().setValue(300)
    pump(app, 0.4)
    grab(w, osp.join(OUT, "03-result-rows.png"))

    M.make_icon().pixmap(512, 512).save(osp.join(OUT, "00-icon.png"))
    print("shots:", sorted(os.listdir(OUT)))


if __name__ == "__main__":
    main()
