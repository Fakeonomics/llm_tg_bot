import subprocess
from .registry import register_tool
from ..utils.scripts import sandbox, unescape_html

import re
import os
import select

# 检查是否在 Unix-like 系统上 (pty 模块主要用于 Unix)
IS_UNIX = hasattr(os, 'fork')

if IS_UNIX:
    import pty

import difflib

last_line = ""
def calculate_similarity(string1: str, string2: str) -> float:
    """Calculates the similarity ratio between two strings.

    Args:
        string1: The first string.
        string2: The second string.

    Returns:
        A float between 0 and 1, where 1 means the strings are identical
        and 0 means they are completely different.
    """
    return difflib.SequenceMatcher(None, string1, string2).ratio()

def compare_line(line: str) -> bool:
    global last_line
    if last_line == "":
        last_line = line
        return False
    similarity = calculate_similarity(line, last_line)
    last_line = line
    # print(f"similarity: {similarity}")
    return similarity > 0.89

def get_python_executable(command: str) -> str:
    """
    获取 Python 可执行文件的路径。

    Returns:
        str: Python 可执行文件的路径。
    """
    cmd_parts = command.split(None, 1)
    if cmd_parts:
        executable = cmd_parts[0]
        args_str = cmd_parts[1] if len(cmd_parts) > 1 else ""

        is_python_exe = False
        if executable == "python" or re.match(r"^python[23]?(\.\d+)?$", executable):
            is_python_exe = True

        if is_python_exe:
            args_list = args_str.split()
            has_u_option = "-u" in args_list
            if not has_u_option:
                if args_str:
                    command = f"{executable} -u {args_str}"
                else:
                    command = f"{executable} -u" # 如果没有其他参数，也添加 -u
    return command

# 执行命令
@register_tool()
def excute_command(command):
    """
执行命令并返回输出结果 (标准输出会实时打印到控制台)。

重要规则:
- **克隆仓库**: 使用 `git clone` 时，必须指定一个新的子目录作为目标，以避免在非空工作目录中出错。
  - **正确示例**: `git clone https://github.com/user/repo_name.git repo_name`
  - **错误用法**: `git clone https://github.com/user/repo_name.git .`
- **禁止**: 禁止用于查看pdf，禁止使用 `pdftotext` 命令。
- **检查子任务状态**: 禁止使用 `ls`, `cat` 等文件系统命令轮询检查子任务的完成状态或其输出。请改用 `get_task_result`, `get_all_tasks_status` 等工具来获取子任务的状态和结果。

参数:
    command: 要执行的命令，例如克隆仓库、安装依赖、运行代码等。

返回:
    命令执行的最终状态和收集到的输出/错误信息。
    """
    try:
        command = unescape_html(command)
        command = get_python_executable(command)

        output_lines = []

        if IS_UNIX:
            # 在 Unix-like 系统上使用 pty 以支持 tqdm 等库的 ANSI 转义序列
            master_fd, slave_fd = pty.openpty()

            process = sandbox.Popen(
                command,
                shell=True,
                stdin=subprocess.PIPE,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
            os.close(slave_fd)

            # print(f"--- 开始执行命令 (PTY): {command} ---")
            while True:
                try:
                    # 使用 select 进行非阻塞读取
                    r, _, _ = select.select([master_fd], [], [], 0.1) # 0.1 秒超时
                    if r:
                        data_bytes = os.read(master_fd, 1024)
                        if not data_bytes: # EOF
                            break
                        # 尝试解码，如果失败则使用 repr 显示原始字节
                        try:
                            data_str = data_bytes.decode(errors='replace')
                        except UnicodeDecodeError:
                            data_str = repr(data_bytes) + " (decode error)\n"

                        print(data_str, end='', flush=True)
                        if "pip install" in command and '━━' in data_str:
                            continue
                        if "git clone" in command and ('Counting objects' in data_str or 'Resolving deltas' in data_str or 'Receiving objects' in data_str or 'Compressing objects' in data_str):
                            continue
                        output_lines.append(data_str)
                    # 检查进程是否已结束，避免在进程已退出后 select 仍然阻塞
                    if process.poll() is not None and not r:
                        break
                except OSError: # 当 PTY 关闭时可能会发生
                    break
            # print(f"\n--- 命令实时输出结束 (PTY) ---")
            os.close(master_fd)
        else:
            # 在非 Unix 系统上，回退到原始的 subprocess.PIPE 行为
            # tqdm 进度条可能不会像在终端中那样动态更新
            process = sandbox.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                encoding='utf-8',
                errors='replace',
                universal_newlines=True
            )
            # print(f"--- 开始执行命令 (PIPE): {command} ---")
            if process.stdout:
                for line in iter(process.stdout.readline, ''):
                    print(line, end='', flush=True)
                    if "pip install" in command and '━━' in line:
                        continue
                    if "git clone" in command and ('Counting objects' in line or 'Resolving deltas' in line or 'Receiving objects' in line or 'Compressing objects' in line):
                        continue
                    output_lines.append(line)
                process.stdout.close()
            # print(f"\n--- 命令实时输出结束 (PIPE) ---")

        process.wait() # 等待命令完成

        # 在非 PTY 模式下，stderr 需要单独读取
        stderr_output = ""
        if not IS_UNIX and process.stderr:
            stderr_output = process.stderr.read()
            process.stderr.close()

        new_output_lines = []
        output_lines = "".join(output_lines).strip().replace("\\r", "\r").replace("\\\\", "").replace("\\n", "\n").replace("\r", "+++").replace("\n", "+++")
        output_lines = re.sub(r'\\u001b\[[0-9;]*[a-zA-Z]', '', output_lines)
        for line in output_lines.split("+++"):
            if line.strip() == "":
                continue
            # aaa = last_line.strip()
            # is_same = compare_line(repr(line.strip()))
            # if not is_same:
            #     # print(f"{repr(aaa)}", flush=True)
            #     # print(f"{repr(line.strip())}", flush=True)
            #     # print(f"is_same: {is_same}", flush=True)
            #     # print(f"\n\n\n", flush=True)
            new_output_lines.append(line)
        # 限制输出行数
        if len(new_output_lines) > 500:
            new_output_lines = new_output_lines[:250] + new_output_lines[-250:]
        final_output_log = "\n".join(new_output_lines)
        # print(f"output_lines: {len(new_output_lines)}")

        if process.returncode == 0:
            if final_output_log.strip() == "":
                return f"执行命令成功"
            else:
                return f"执行命令成功:\n{final_output_log.strip()}"
        else:
            # 如果是 PTY 模式，stderr 已经包含在 final_output_log 中
            if IS_UNIX:
                return f"<tool_error>执行命令失败 (退出码 {process.returncode}):\n输出/错误:\n{final_output_log}</tool_error>"
            else:
                return f"<tool_error>执行命令失败 (退出码 {process.returncode}):\n错误: {stderr_output}\n输出: {final_output_log}</tool_error>"

    except FileNotFoundError:
        return f"<tool_error>执行命令失败: 命令或程序未找到 ({command})</tool_error>"
    except Exception as e:
        return f"<tool_error>执行命令时发生异常: {e}</tool_error>"

if __name__ == "__main__":
    # print(excute_command("ls -l && echo 'Hello, World!'"))
    # print(excute_command("ls -l &amp;&amp; echo 'Hello, World!'"))

#     tqdm_script = """
# import time
# from tqdm import tqdm

# for i in range(10):
#     print(f"TQDM 进度条测试: {i}")
#     time.sleep(1)
# print('\\n-------TQDM 任务完成.')
# """

#     tqdm_script = """
# import time
# print("Hello, World!1")
# print("Hello, World!2")
# for i in range(10):
#     print(f"TQDM 进度条测试: {i}")
#     time.sleep(1)
# """
#     processed_tqdm_script = tqdm_script.replace('"', '\\"')
#     tqdm_command = f"python -c \"{processed_tqdm_script}\""
#     # print(f"执行: {tqdm_command}")
#     print(excute_command(tqdm_command))

    tqdm_script = """
import time
with open("/Users/yanyuming/Downloads/GitHub/beswarm/1.txt", "r") as f:
    content = f.read()
for i in content.split("\\n"):
    print(i)
"""
    processed_tqdm_script = tqdm_script.replace('"', '\\"')
    tqdm_command = f"python -c \"{processed_tqdm_script}\""
    # print(f"执行: {tqdm_command}")
    print(excute_command(tqdm_command))

#     tqdm_script = """
# import time
# from tqdm import tqdm

# for i in tqdm(range(10)):
#     time.sleep(1)
# """
#     processed_tqdm_script = tqdm_script.replace('"', '\\"')
#     tqdm_command = f"python -c \"{processed_tqdm_script}\""
#     # print(f"执行: {tqdm_command}")
#     print(excute_command(tqdm_command))


    # tqdm_command = f"pip install requests"
    # # print(f"执行: {tqdm_command}")
    # print(excute_command(tqdm_command))


    # long_running_command_unix = "echo '开始长时间任务...' && for i in 1 2 3; do echo \"正在处理步骤 $i/3...\"; sleep 1; done && echo '长时间任务完成!'"
    # print(f"执行: {long_running_command_unix}")
    # print(excute_command(long_running_command_unix))


    # long_running_command_unix = "pip install torch"
    # print(f"执行: {long_running_command_unix}")
    # print(excute_command(long_running_command_unix))


#     python_long_task_command = """
# python -c "import time; print('Python 长时间任务启动...'); [print(f'Python 任务进度: {i+1}/3', flush=True) or time.sleep(1) for i in range(3)]; print('Python 长时间任务完成.')"
# """
#     python_long_task_command = python_long_task_command.strip() # 移除可能的前后空白
#     print(f"执行: {python_long_task_command}")
#     print(excute_command(python_long_task_command))

    # print(get_python_executable("python -c 'print(123)'"))
# python -m beswarm.aient.aient.plugins.excute_command
