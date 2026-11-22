# -*- coding: utf-8 -*-
"""把主窗口离屏渲染成 PNG，用于检查视觉。"""
import os
import shutil
import sys
import tempfile
import time

os.environ.pop("QT_QPA_PLATFORM", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402
import main as m  # noqa: E402

ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "文件比对工具原型", "assets")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shot.png")

app = QApplication([])
w = m.MainWindow()
w.resize(1300, 900)
w.setAttribute(m.Qt.WA_DontShowOnScreen, True)
w.show()
app.processEvents()
app.processEvents()
from PySide6.QtWidgets import QLabel  # noqa: E402

info = []
for lb in w.findChildren(QLabel):
    if lb.text() in ("同步滚动", "对比目录"):
        p = lb.parentWidget()
        info.append(f"{lb.text()} label={lb.geometry().getRect()} parent={p.geometry().getRect()}")
info.append(f"pathA width={w.path_a.width()} pathB width={w.path_b.width()}")
info.append(f"dirbar width={w.path_a.parentWidget().parentWidget().width()}")
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "geom.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(info))
w.grab().save(OUT)
tmp = tempfile.mkdtemp(prefix="shot_")
A = os.path.join(tmp, "归档")
B = os.path.join(tmp, "旧硬盘")
for d in ("照片/2026", "项目资料/SOP", "用车", "图纸"):
    os.makedirs(os.path.join(A, d), exist_ok=True)
for d in ("手机导出", "MES", "SOP", "用车", "图纸"):
    os.makedirs(os.path.join(B, d), exist_ok=True)


def put(root, rel, src=None, text=None, size=None):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if text is not None:
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    elif src and os.path.exists(src):
        shutil.copy2(src, p)
    return p


def asset(name):
    p = os.path.join(ASSETS, name)
    return p if os.path.exists(p) else None


groups = []


def g(h, size, files):
    recs = [m.FileRec(p, side, size, os.path.getmtime(p) if os.path.exists(p) else time.time(), h) for side, p in files]
    groups.append(m.Group(h, size, recs))


p1 = put(A, "照片/2026/青岛海边_全家照_001.jpg", asset("beach.jpg"))
p2 = put(B, "手机导出/青岛海边_全家照_001.jpg", asset("beach.jpg"))
g("a" * 8 + "0" * 56, os.path.getsize(p1), [("A", p1), ("B", p2)])

p1 = put(A, "照片/2024/IMG_20240713_142233.jpg", asset("drawing.jpg"))
p2 = put(A, "照片/2024/副本/IMG_20240713_142233 (1).jpg", asset("drawing.jpg"))
p3 = put(B, "手机导出/IMG_20240713_142233.jpg", asset("drawing.jpg"))
g("b" * 8 + "1" * 56, os.path.getsize(p1), [("A", p1), ("A", p2), ("B", p3)])

p1 = put(A, "项目资料/SOP/冲压车间SOP_V1.pdf", text="pdf" * 900)
p2 = put(B, "SOP/冲压车间SOP_V1.pdf", text="pdf" * 900)
g("c" * 8 + "2" * 56, os.path.getsize(p1), [("A", p1), ("B", p2)])

p1 = put(A, "用车/阿维塔06_保养记录.pdf", text="pdf" * 400)
p2 = put(B, "用车/阿维塔06_保养记录 - 副本.pdf", text="pdf" * 400)
g("d" * 8 + "3" * 56, os.path.getsize(p1), [("A", p1), ("B", p2)])

p1 = put(A, "图纸/歌尔_SMT产线布局图.dwg", text="dwg" * 1200)
p2 = put(B, "图纸/歌尔_SMT产线布局图.dwg", text="dwg" * 1200)
g("e" * 8 + "4" * 56, os.path.getsize(p1), [("A", p1), ("B", p2)])

u1 = put(A, "照片/2025/潍坊_风筝会_001.jpg", asset("kite.jpg"))
u2 = put(A, "项目资料/天津调研笔记.docx", text="docx" * 200)
u3 = put(B, "照片/2016/毕业照.jpg", asset("graduate.jpg"))
u4 = put(B, "安装包/node-v20.zip", text="zip" * 5000)
ug = []
for p, side in ((u1, "A"), (u2, "A"), (u3, "B"), (u4, "B")):
    ug.append(m.FileRec(p, side, os.path.getsize(p), os.path.getmtime(p), "u" + str(ord(p[-1])) * 10))

res = m.ScanResult(groups + [m.Group(f.hash, f.size, [f]) for f in ug],
                   [(A + r"\项目资料\排产表.xlsx", "文件被占用（Excel 正在打开）"),
                    (B + r"\加密\2024流水.xlsx", "无读取权限")],
                   sum(1 for _ in groups) + 21000, 17000, 18.4)
w.result = res
w.root_a, w.root_b = A, B
w.path_a.setText(A)
w.path_b.setText(B)
w._rebuild_index()
w._render()
w._update_buttons()
w.st_scan.setText("完成 · 5 组重复 / 8 个副本 / 可释放 12.4 MB · 用时 4.2s")
w.st_stats.setText("5 组 · 8 副本 · 可释放 12.4 MB")
w.st_count.setText("已扫描 38,014（A 21,005 / B 17,009）· 跳过 2")

w.show()
app.processEvents()
app.processEvents()
w.grab().save(OUT)
print("saved", OUT, os.path.getsize(OUT))
shutil.rmtree(tmp, ignore_errors=True)
