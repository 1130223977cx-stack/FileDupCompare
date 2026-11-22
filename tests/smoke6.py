# -*- coding: utf-8 -*-
"""回归测试：撤销上次归并能跨天读日志（昨天的归并，今天仍能撤销）。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main as M

HEAD = "时间,动作,侧,原路径,目标路径,大小,哈希,结果,备注"


class Stub:
    def __init__(self, app_dir):
        self._d = app_dir

    def app_dir(self):
        return self._d


def write(path, rows, bom=True):
    text = "\n".join([HEAD] + rows) + "\n"
    with open(path, "w", encoding="utf-8-sig" if bom else "utf-8", newline="") as f:
        f.write(text)


def run(root):
    logs = os.path.join(root, "logs")
    os.makedirs(logs)
    old = os.path.join(logs, "操作日志_2026-11-22.csv")
    rows = []
    for i in range(7):
        rows.append(f"2026-11-22 20:29:2{i},归并移动,A,D:\\照片\\f{i}.mp4,F:\\照片\\照片\\f{i}.mp4,100,,成功,")
    rows.append("2026-11-22 20:31:00,删除,A,D:\\照片\\gone.jpg,,1,,失败,占用")
    write(old, rows)
    # 今天的日志存在但只有别的动作 → 应回退到昨天那份
    new = os.path.join(logs, "操作日志_2026-11-23.csv")
    write(new, ["2026-11-23 09:00:00,扫描,,,D:\\照片,F:\\照片,,成功,"])

    moves, src = M.MainWindow._merge_log_batch(Stub(root))
    print("[1] 取到批次数 =", len(moves), "来源 =", src)
    assert len(moves) == 7, f"应取到 7 条，实际 {len(moves)}"
    assert src == "操作日志_2026-11-22.csv", src
    assert moves[0] == ("D:\\照片\\f0.mp4", "F:\\照片\\照片\\f0.mp4", "A"), moves[0]
    print("[2] 顺序正确（首条为最早那条）=", moves[0][0].endswith("f0.mp4"))
    assert moves[0][0].endswith("f0.mp4")

    # 末尾不是归并成功 → 不应把出错的那条算进来
    bad = os.path.join(logs, "操作日志_2026-11-21.csv")
    write(bad, ["2026-11-21 10:00:00,归并移动,A,D:\\a.mp4,E:\\a.mp4,1,,成功,",
                "2026-11-21 10:00:01,归并移动,A,D:\\b.mp4,E:\\b.mp4,1,,失败,"], bom=False)
    print("[3] 无 BOM 的旧日志也能读 =", os.path.exists(bad))
    print("ALL OK")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="cmp6_") as td:
        run(td)
