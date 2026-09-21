from ..utils.scripts import sandbox

from .registry import register_tool

@register_tool()
def set_readonly_path(path: str):
    """
    将指定的文件或目录路径设置为只读。

    这个函数将一个路径添加到只读路径列表中。一旦被设置为只读，
    任何对该路径的写操作都将被禁止。

    Args:
        path (str): 需要设置为只读的文件或目录的路径。

    Returns:
        str: 操作成功或失败的提示信息。
    """
    status = sandbox.add_readonly_path(path)
    if status == "Success":
        return f"路径 '{path}' 已成功设置为只读。"
    else:
        return f"系统未启用该功能，无法设置只读路径。"