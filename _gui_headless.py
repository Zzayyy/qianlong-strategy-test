# -*- coding: utf-8 -*-
"""
_gui_headless.py —— GUI 测试用的「无模态」辅助
=============================================
背景（踩过的坑）：
    offscreen 平台下 QMessageBox 是【模态】的 —— 它会一直等用户点按钮，
    而测试里没人点，于是整个测试永久阻塞（表现：命令卡住、无输出）。
    更坑的是它连超时都不报，看起来像死机。

    GUI 里现在有好几处确认弹窗（安全闸、打真平台确认、无消费者警告…），
    以后还会加。所以统一在这里把 QMessageBox 换掉：不弹窗，改成
    【记录 + 自动回答】，测试就能继续跑，还能断言"到底弹了什么"。

用法（必须在 import gui_test 之后、任何发送动作之前调用）：
    import _gui_headless
    _gui_headless.install()                 # 默认一律回答"否"
    _gui_headless.install(answer="yes")     # 需要点"是"的用例
    ...
    print(_gui_headless.dialogs())          # 看弹过哪些窗
"""
import os
import sys

from PySide6.QtWidgets import QMessageBox

_HITS = []
_ANSWER = "no"

_YES = QMessageBox.StandardButton.Yes
_NO = QMessageBox.StandardButton.No
_OK = QMessageBox.StandardButton.Ok


def install(answer="no", verbose=True):
    """把 QMessageBox 的四个静态方法换成不阻塞的版本。

    answer: "yes" / "no" / "ok"  —— 自动返回哪个按钮
    """
    global _ANSWER, _HITS
    _ANSWER = str(answer).lower()
    _HITS = []

    def _ret():
        return _OK if _ANSWER in ("ok", "yes") else _NO

    def make(kind):
        def f(*a, **kw):
            title = a[1] if len(a) > 1 else kw.get("title", "")
            body = a[2] if len(a) > 2 else kw.get("text", "")
            body = str(body or "")
            _HITS.append({
                "kind": kind,
                "title": str(title),
                "text": body,
                "first_line": body.splitlines()[0] if body else "",
                "answered": _ANSWER,
            })
            if verbose:
                sys.stdout.write("  >>> 弹窗[%s] %s | %s  (自动回答=%s)\n"
                                 % (kind, title,
                                    body.splitlines()[0] if body else "",
                                    _ANSWER))
                sys.stdout.flush()
            return _ret()
        return staticmethod(f)

    for kind in ("warning", "question", "information", "critical"):
        setattr(QMessageBox, kind, make(kind))
    return _HITS


def dialogs():
    return list(_HITS)


def titles():
    return [h["title"] for h in _HITS]


def clear():
    del _HITS[:]
