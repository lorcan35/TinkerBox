"""Calculator tool: safe mathematical expression evaluator."""

import ast
import logging
import math
import operator
import re

from dragon_voice.tools.base import Tool

logger = logging.getLogger(__name__)

# Whitelisted binary/unary operators
SAFE_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

# Whitelisted functions
SAFE_FUNCS = {
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log2": math.log2,
    "log10": math.log10,
    "abs": abs,
    "round": round,
    "ceil": math.ceil,
    "floor": math.floor,
}

# Whitelisted constants
SAFE_CONSTS = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
}


def _preprocess(expression: str) -> str:
    """Normalize human-friendly expressions to Python syntax."""
    expr = expression.strip()

    # "15% of 230" -> "(15/100)*230"
    m = re.match(r'^([\d.]+)\s*%\s*of\s+([\d.]+)$', expr, re.IGNORECASE)
    if m:
        return f"({m.group(1)}/100)*{m.group(2)}"

    # "X^Y" -> "X**Y"
    expr = expr.replace('^', '**')

    # Standalone "%" as modulo is already handled by Python

    return expr


def _safe_eval(node: ast.AST) -> float:
    """Recursively evaluate an AST node using only whitelisted operations."""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)

    # Numbers
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)

    # Unary operators: -x, +x
    if isinstance(node, ast.UnaryOp):
        op_func = SAFE_OPS.get(type(node.op))
        if op_func is None:
            raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")
        return op_func(_safe_eval(node.operand))

    # Binary operators: x + y, x * y, etc.
    if isinstance(node, ast.BinOp):
        op_func = SAFE_OPS.get(type(node.op))
        if op_func is None:
            raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
        left = _safe_eval(node.left)
        right = _safe_eval(node.right)
        # Safety: prevent huge exponents
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ValueError("Exponent too large (max 1000)")
        return op_func(left, right)

    # Function calls: sqrt(x), sin(x), etc.
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("Only named functions are allowed")
        func_name = node.func.id
        func = SAFE_FUNCS.get(func_name)
        if func is None:
            raise ValueError(f"Unknown function: {func_name}")
        args = [_safe_eval(a) for a in node.args]
        return float(func(*args))

    # Named constants: pi, e
    if isinstance(node, ast.Name):
        val = SAFE_CONSTS.get(node.id)
        if val is None:
            raise ValueError(f"Unknown variable: {node.id}")
        return float(val)

    raise ValueError(f"Unsupported expression element: {type(node).__name__}")


class CalculatorTool(Tool):
    """Calculate mathematical expressions safely (no eval)."""

    @property
    def name(self) -> str:
        return "calculator"

    @property
    def description(self) -> str:
        return "Calculate mathematical expressions accurately"

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "Math expression (e.g., '15% of 230', 'sqrt(144) + 3^2', 'sin(pi/4)')",
                },
            },
            "required": ["expression"],
        }

    async def execute(self, args: dict) -> dict:
        expression = args.get("expression", "").strip()
        if not expression:
            return {"error": "expression is required"}

        try:
            processed = _preprocess(expression)
            tree = ast.parse(processed, mode="eval")
            result = _safe_eval(tree)

            # Clean up display: 34.0 -> 34, but keep 34.5
            if result == int(result) and abs(result) < 1e15:
                display = str(int(result))
            else:
                display = f"{result:.10g}"

            logger.info("Calculator: %s = %s", expression, display)
            return {
                "expression": expression,
                "result": result,
                "display": display,
            }

        except (ValueError, TypeError, SyntaxError, ZeroDivisionError) as e:
            return {"expression": expression, "error": str(e)}
        except Exception as e:
            logger.exception("Calculator error")
            return {"expression": expression, "error": f"Calculation failed: {e}"}
