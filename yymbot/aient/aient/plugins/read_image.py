import os
import base64
import mimetypes
from .registry import register_tool
import io
from PIL import Image, UnidentifiedImageError

@register_tool()
def read_image(image_path: str):
    """
读取本地图片文件，将其转换为 Base64 编码，并返回包含 MIME 类型和完整数据的字符串。
此工具用于将图片内容加载到上下文中。

参数:
    image_path (str): 本地图片文件的路径。

返回:
    str: 成功时返回包含图片MIME类型和Base64编码数据的格式化字符串。
            失败时返回错误信息字符串。
    """
    original_max_pixels = Image.MAX_IMAGE_PIXELS
    try:
        # 暂时禁用解压炸弹检查，以允许打开非常大的图像进行缩放。
        # 这是安全的，因为我们控制代码，并且会立即对图像进行缩减。
        Image.MAX_IMAGE_PIXELS = None

        # 检查路径是否存在
        if not os.path.exists(image_path):
            return f"<tool_error>图片路径 '{image_path}' 不存在。</tool_error>"
        # 检查是否为文件
        if not os.path.isfile(image_path):
            return f"<tool_error>路径 '{image_path}' 不是一个有效的文件 (可能是一个目录)。</tool_error>"

        # 尝试猜测MIME类型
        mime_type, _ = mimetypes.guess_type(image_path) # encoding 变量通常不需要

        if not mime_type or not mime_type.startswith('image/'):
            # 如果mimetypes无法识别，或者不是图片类型
            return f"<tool_error>文件 '{image_path}' 的MIME类型无法识别为图片 (检测到: {mime_type})。请确保文件是常见的图片格式 (e.g., PNG, JPG, GIF, WEBP)。</tool_error>"

        max_dim = 3072
        with Image.open(image_path) as img:
            width, height = img.size

            target_img = img
            if width > max_dim or height > max_dim:
                if width > height:
                    new_width = max_dim
                    new_height = int(height * (max_dim / width))
                else:
                    new_height = max_dim
                    new_width = int(width * (max_dim / height))

                try:
                    resampling_filter = Image.Resampling.LANCZOS
                except AttributeError:
                    # 兼容旧版 Pillow
                    resampling_filter = Image.LANCZOS

                target_img = img.resize((new_width, new_height), resampling_filter)

            # 将处理后的图片保存到内存中的字节流
            img_byte_arr = io.BytesIO()
            # 保留原始图片格式以获得最佳兼容性，如果无法确定格式，默认为PNG
            img_format = img.format or 'PNG'
            target_img.save(img_byte_arr, format=img_format)
            image_data = img_byte_arr.getvalue()

        base64_encoded_data = base64.b64encode(image_data).decode('utf-8')

        return f"data:{mime_type};base64," + base64_encoded_data

    except UnidentifiedImageError:
        return f"<tool_error>无法识别的图片格式 '{image_path}'，文件可能已损坏或格式不受支持。</tool_error>"
    except FileNotFoundError:
        # 这个异常通常由 open() 抛出，如果 os.path.exists 通过但文件在读取前被删除
        # 或者路径检查逻辑未能完全覆盖所有情况 (理论上不应发生)
        return f"<tool_error>图片路径 '{image_path}' 未找到 (可能在检查后被删除或移动)。</tool_error>"
    except PermissionError:
        return f"<tool_error>没有权限访问图片路径 '{image_path}'。</tool_error>"
    except IOError as e: # 例如文件损坏无法读取，或磁盘问题
        return f"<tool_error>读取图片 '{image_path}' 时发生 I/O 错误: {e}</tool_error>"
    except Exception as e:
        return f"<tool_error>读取图片 '{image_path}' 时发生未知错误: {e}</tool_error>"
    finally:
        # 恢复原始限制以避免副作用。
        Image.MAX_IMAGE_PIXELS = original_max_pixels