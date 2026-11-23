# -*- coding: utf-8 -*-
"""驱动命令行工具（在装有腾讯会议的服务器上校准/手动操作）：

  python -m server.driver.cli inspect
      导出当前 UIA 控件树到 tm_inspect.txt，用于校准 config.json 的 tm 选择器
  python -m server.driver.cli book "主题" "2026-11-23 14:00" "2026-11-23 15:00"
      手动预订
  python -m server.driver.cli cancel "123456789" "主题" "2026-11-23 14:00"
      手动按腾讯会议号取消
  python -m server.driver.cli list
      列出腾讯侧已订会议
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _load_cfg():
    p = os.path.join(ROOT, "config.json")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    cfg = _load_cfg()
    from server.driver import get_driver
    d = get_driver(cfg)

    if cmd == "inspect":
        txt = d.inspect()
        out = os.path.join(ROOT, "tm_inspect.txt")
        with open(out, "w", encoding="utf-8") as f:
            f.write(txt)
        print(f"已导出 {len(txt)} 字符到 {out}")
        return 0
    if cmd == "book" and len(argv) >= 4:
        r = d.book(argv[1], argv[2], argv[3])
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r["ok"] else 2
    if cmd == "cancel" and len(argv) >= 4:
        r = d.cancel(argv[1], argv[2], argv[3])
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r["ok"] else 2
    if cmd == "list":
        items = d.list_scheduled()
        if not items:
            print("(空)")
        for it in items:
            print(f"id={it.get('meeting_id')} | {it.get('raw')}")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
