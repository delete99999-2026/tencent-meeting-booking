# -*- coding: utf-8 -*-
"""腾讯会议驱动层。

对外只暴露 get_driver(cfg)，按 cfg['driver_mode'] 返回对应实现：
  - 'dryrun'    模拟成功（开发/自测/无客户端环境）
  - 'pywinauto' 真实操作 Windows 腾讯会议客户端
"""
import logging

log = logging.getLogger("tm.driver")


def get_driver(cfg):
    mode = (cfg.get("driver_mode") or "dryrun").lower()
    tm_cfg = cfg.get("tm") or {}
    if mode == "pywinauto":
        from server.driver.pywinauto_tm import PywinautoDriver
        return PywinautoDriver(tm_cfg)
    from server.driver.dryrun import DryRunDriver
    return DryRunDriver()
