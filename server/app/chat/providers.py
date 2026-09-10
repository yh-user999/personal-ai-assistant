"""确定性事实 provider。"""
from __future__ import annotations

import ast
import operator
from typing import Any

from app.chat.response_plan import plan_datetime

_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def current_datetime(message: str) -> dict[str, Any] | None:
    value = plan_datetime(message)
    if value is None:
        return None
    return {"kind": "current_datetime", "source": "system_clock", "value": value}


def _calculate(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_calculate(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left, right = _calculate(node.left), _calculate(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 10:
            raise ValueError("指数过大")
        return _BINOPS[type(node.op)](left, right)
    raise ValueError("不支持的表达式")


def calculator(expression: str) -> dict[str, Any] | None:
    text = (expression or "").strip()
    if not text or len(text) > 100 or any(ch in text for ch in "=_[]{};:"):
        return None
    try:
        tree = ast.parse(text, mode="eval")
        result = _calculate(tree.body)
        if abs(result) > 1e15:
            return None
        return {"kind": "calculation", "source": "safe_arithmetic", "value": result}
    except (SyntaxError, ValueError, TypeError, ZeroDivisionError, OverflowError):
        return None
