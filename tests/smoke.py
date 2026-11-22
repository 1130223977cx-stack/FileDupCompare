# -*- coding: utf-8 -*-
"""无界面自检：窗口构建 / 渲染 / 扫描引擎 / 归并选路。"""
import os
import shutil
import sys
import tempfile

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402
import main as m  # noqa: E402

app = QApplication([])
w = m.MainWindow()
w.show()
app.processEvents()
print("[1] window ok", w.size().width(), w.size().height())

# ---------- 假数据渲染 ----------
g1 = m.Group("a" * 64, 1000, [m.FileRec(r"D:\x\a.txt", "A", 1000, 0), m.FileRec(r"E:\y\a.txt", "B", 1000, 0)])
g2 = m.Group("b" * 64, 2000, [m.FileRec(r"D:\x\b.txt", "A", 2000, 0), m.FileRec(r"D:\x\b (1).txt", "A", 2000, 0)])
g3 = m.Group("c" * 64, 3000, [m.FileRec(r"D:\x\c.txt", "A", 3000, 0)])
w.result = m.ScanResult([g1, g2, g3], [("D:\\x\\z.txt", "文件被占用")], 5, 2, 1.0)
w._rebuild_index()
w._render()
app.processEvents()
print("[2] render ok rows=", w.view.lay.count(), "paths=", len(w.by_path))

w._set_filter("same")
app.processEvents()
w._set_filter("diff")
app.processEvents()
print("[3] filter ok")

w._apply_strategy("A")
print("[4] strategy selected=", len(w.selected))

# ---------- 真实扫描 ----------
tmp = tempfile.mkdtemp(prefix="cmp_")
a = os.path.join(tmp, "A")
b = os.path.join(tmp, "B")
os.makedirs(os.path.join(a, "sub"))
os.makedirs(os.path.join(b, "sub"))
with open(os.path.join(a, "same.txt"), "w", encoding="utf-8") as f:
    f.write("hello" * 100)
with open(os.path.join(b, "same - copy.txt"), "w", encoding="utf-8") as f:
    f.write("hello" * 100)
with open(os.path.join(a, "sub", "only_a.txt"), "w", encoding="utf-8") as f:
    f.write("only a")
with open(os.path.join(b, "sub", "only_b.txt"), "w", encoding="utf-8") as f:
    f.write("only b")
open(os.path.join(a, "empty.txt"), "w").close()

res_holder = []
worker = m.ScanWorker(a, b, {"recursive": True, "skip_hidden": True, "skip_zero": True, "min_kb": 0, "exclude": ""})
worker.finished_result.connect(lambda r: res_holder.append(r))
worker.run()
res = res_holder[0]
dups = [g for g in res.groups if len(g.files) > 1]
uniq_a = [g for g in res.groups if len(g.files) == 1 and g.files[0].side == "A"]
uniq_b = [g for g in res.groups if len(g.files) == 1 and g.files[0].side == "B"]
print("[5] scan ok  dup=", len(dups), " uniqA=", len(uniq_a), " uniqB=", len(uniq_b),
      " skipped=", len(res.skipped), " aborted=", res.aborted)
assert len(dups) == 1 and len(uniq_a) == 1 and len(uniq_b) == 1, "分组结果不符"

# ---------- 归并候选 ----------
w.result = res
w.root_a, w.root_b = a, b
w._rebuild_index()
cands = w._merge_candidates("A")
print("[6] merge candidates(A) =", [(r.path, r.size) for _, r in cands])
assert len(cands) == 1 and cands[0][1].path.endswith("only_a.txt")

# ---------- 真实归并 ----------
rec, target = cands[0][1], os.path.join(b, "sub", "only_a.txt")
shutil.move(m.win_path(rec.path), m.win_path(target))
print("[7] moved ->", os.path.exists(target), " origin gone:", not os.path.exists(rec.path))

shutil.rmtree(tmp, ignore_errors=True)
print("ALL OK")
