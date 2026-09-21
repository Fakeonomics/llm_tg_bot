import os
from .registry import register_tool

# 列出目录文件
@register_tool()
def list_directory(path="."):
    """
列出指定目录中的所有文件和子目录

参数:
    path: 要列出内容的目录路径，默认为当前目录

返回:
    目录内容的列表字符串
    """
    try:
        # 获取目录内容
        items = os.listdir(path)

        # 区分文件和目录
        files = []
        directories = []

        for item in items:
            item_path = os.path.join(path, item)
            if os.path.isfile(item_path):
                files.append(item + " (文件)")
            elif os.path.isdir(item_path):
                directories.append(item + " (目录)")

        # 格式化输出结果
        result = f"路径 '{path}' 中的内容:\n\n"

        if directories:
            result += "目录:\n" + "\n".join([f"- {d}" for d in sorted(directories)]) + "\n\n"

        if files:
            result += "文件:\n" + "\n".join([f"- {f}" for f in sorted(files)])

        if not files and not directories:
            result += "该目录为空"

        return result

    except FileNotFoundError:
        return f"<tool_error>路径 '{path}' 不存在</tool_error>"
    except PermissionError:
        return f"<tool_error>没有权限访问路径 '{path}'</tool_error>"
    except Exception as e:
        return f"<tool_error>列出目录时发生错误: {e}</tool_error>"