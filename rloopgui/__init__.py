#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rloopGUI —— rloop 的显示层。

独立程序，**零 `import rloop`**。它通过 rloop 的 CLI（`rloop api <verb>`）
拿数据，用 JSON 通信。所以：
  · 核心可以随便重构，只要 api 契约不变，面板一行不用改；
  · 面板可以换任何语言重写，核心一行不用改；
  · 面板是**观察者** —— 契约里没有写产物的 verb，长不出「处理 findings」的按钮。

入口是 `rloop web`（核心以进程方式拉起这里），或者直接 `python3 -m rloopgui web`。
"""

VERSION = "0.1.0"
