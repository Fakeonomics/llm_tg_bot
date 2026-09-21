import os
import ast
import asyncio
import logging
import tempfile
from .registry import register_tool

def get_dangerous_attributes(node):
    # 简单的代码审查，检查是否包含某些危险关键词
    dangerous_keywords = ['os', 'subprocess', 'sys', 'import', 'eval', 'exec', 'open']
    if isinstance(node, ast.Name):
        return node.id in dangerous_keywords
    elif isinstance(node, ast.Attribute):
        return node.attr in dangerous_keywords
    return False

def check_code_safety(code):
    try:
        # 解析代码为 AST
        tree = ast.parse(code)

        # 检查所有节点
        for node in ast.walk(tree):
            # 检查危险属性访问
            if get_dangerous_attributes(node):
                return False

            # 检查危险的调用
            if isinstance(node, ast.Call):
                if isinstance(node.func, (ast.Name, ast.Attribute)):
                    if get_dangerous_attributes(node.func):
                        return False

            # 检查字符串编码/解码操作
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ('encode', 'decode'):
                    return False

        return True
    except SyntaxError:
        return False

@register_tool()
async def run_python_script(code):
    """
    执行 Python 代码

    参数:
        code: 要执行的 Python 代码字符串

    返回:
        执行结果字符串
    """

    timeout = 10
    # 检查代码安全性
    if not check_code_safety(code):
        return "Code contains potentially dangerous operations.\n\n"

    # 添加一段捕获代码，确保最后表达式的值会被输出
    # 这种方式比 ast 解析更可靠
    wrapper_code = """
import sys
_result = None

def _capture_last_result(code_to_run):
    global _result
    namespace = {{}}
    exec(code_to_run, namespace)
    if "_last_expr" in namespace:
        _result = namespace["_last_expr"]

# 用户代码
_user_code = '''
{}
'''

# 处理用户代码，尝试提取最后一个表达式
lines = _user_code.strip().split('\\n')
if lines:
    # 检查最后一行是否是表达式
    last_line = lines[-1].strip()
    if last_line and not last_line.startswith(('def ', 'class ', 'if ', 'for ', 'while ', 'try:', 'with ')):
        if not any(last_line.startswith(kw) for kw in ['return', 'print', 'raise', 'assert', 'import', 'from ']):
            if not last_line.endswith(':') and not last_line.endswith('='):
                # 可能是表达式，修改它
                lines[-1] = "_last_expr = " + last_line
                _user_code = '\\n'.join(lines)

_capture_last_result(_user_code)

# 输出结果
if _result is not None:
    print("\\nResult:", repr(_result))
""".format(code)

    import asyncio as _aio
    try:
        from src.services.agent_tools import run_code as _container_run
    except Exception:
        _container_run = None
    if _container_run is None:
        logging.warning("container runner missing; degraded exec")
        return "<tool_error>Container unavailable</tool_error>"
    try:
        res = await _aio.to_thread(
            _container_run, ["python3", "-c", wrapper_code],
            30, 500, 30, "yym")
        mess = (
            f"Execution result:\n{res.get('stdout', '')}\n",
            f"Stderr:\n{res.get('stderr', '')}\n" if res.get('stderr') else "",
            f"Return Code: {res.get('code')}\n" if res.get('code') else "",
        )
        mess = "".join(mess)
        return mess
    except Exception as e:
        logging.error(f"Error executing code: {str(e)}")
        return f"<tool_error>Error: {str(e)}</tool_error>"

# 使用示例
async def main():
    code = """
print("Hello, World!")
"""
    code = """
def add(a, b):
    return a + b

result = add(5, 3)
print(result)
    """
    result = await run_python_script(code)
    print(result)

if __name__ == "__main__":
    asyncio.run(main())