"""Safe arithmetic evaluator for declared computations: + - * / parentheses, numbers, D-ids and names.

The expression is parsed with ast and walked by hand in Decimal, so nothing is ever eval'd and a
ratio like 12636 / 2160 comes out as exactly 5.85, not 5.8499999.
"""
from __future__ import annotations

import ast
import re
from decimal import Decimal, localcontext

_ID = re.compile(r"\b([A-Z]{1,4})-(\d{1,4})\b")
_OPS = {ast.Add: "add", ast.Sub: "sub", ast.Mult: "mul", ast.Div: "div"}


class ComputeError(ValueError):
    """Why an expression could not be evaluated, in plain words."""


def ids_in(expr: str) -> list[str]:
    """Item ids an expression reads, in order: "D-020 / D-021" gives ["D-020", "D-021"]."""
    return [f"{a}-{b}" for a, b in _ID.findall(expr)]


def _py(name: str) -> str:
    return _ID.sub(lambda mm: f"{mm.group(1)}_{mm.group(2)}", name)


def _dec(v) -> Decimal:
    if isinstance(v, Decimal):
        return v
    if isinstance(v, float):
        return Decimal(repr(v))
    if isinstance(v, int) and not isinstance(v, bool):
        return Decimal(v)
    raise ComputeError(f"value {v!r} is not a number")


def _eval(node: ast.AST, env: dict[str, Decimal], src: str, back: dict[str, str]) -> Decimal:
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left, env, src, back), _eval(node.right, env, src, back)
        op = _OPS[type(node.op)]
        if op == "add":
            return left + right
        if op == "sub":
            return left - right
        if op == "mul":
            return left * right
        if right == 0:
            raise ComputeError("division by zero")
        return left / right
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _eval(node.operand, env, src, back)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        seg = ast.get_source_segment(src, node)
        return Decimal(seg) if seg else _dec(node.value)
    if isinstance(node, ast.Name):
        if node.id not in env:
            raise ComputeError(f"unknown name {back.get(node.id, node.id)}")
        return env[node.id]
    raise ComputeError(f"{type(node).__name__} is not allowed (only + - * /, parentheses, numbers and names)")


def safe_eval(expr: str, env: dict[str, Decimal]) -> Decimal:
    """Evaluate expr over env (D-ids and earlier computation names). Raises ComputeError."""
    src = _py(expr)
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError:
        raise ComputeError(f"cannot parse {expr!r}") from None
    names = {_py(k): _dec(v) for k, v in env.items()}
    back = {_py(k): k for k in env} | {_py(r): r for r in ids_in(expr)}
    with localcontext() as ctx:
        ctx.prec = 34
        return _eval(tree.body, names, src, back)
