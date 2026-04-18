import argparse
import locale
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = REPO_ROOT / "data" / "Video" / "test.mp4"


def parse_args():
    parser = argparse.ArgumentParser(
        description="使用 OpenAI Whisper 将 MP4 或 MP3 音视频转写为纯文本。"
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"输入文件路径，支持 MP4 或 MP3。默认值：{DEFAULT_INPUT}",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="输出文本文件路径。默认与输入文件同目录同名，仅扩展名改为 .txt。",
    )
    parser.add_argument(
        "--model",
        default="large",
        help="Whisper 模型名称。默认值：large",
    )
    parser.add_argument(
        "--language",
        default="zh",
        help="传给 Whisper 的语言代码。默认值：zh",
    )
    parser.add_argument(
        "--task",
        choices=("transcribe", "translate"),
        default="transcribe",
        help="Whisper 任务类型。默认值：transcribe",
    )
    parser.add_argument(
        "--device",
        default=None,
        help="Torch 运行设备，例如 cpu、cuda 或 mps。默认自动选择。",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="传给 Whisper 的采样温度。默认值：0.0",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="输出 Whisper 的详细处理进度。",
    )
    return parser.parse_args()


def ensure_dependencies():
    if shutil.which("ffmpeg") is None:
        print("缺少依赖：系统环境中未找到 ffmpeg。", file=sys.stderr)
        return None, 1

    try:
        import whisper
    except ImportError:
        print(
            "缺少依赖：未安装 whisper，请先执行 `pip install openai-whisper`。",
            file=sys.stderr,
        )
        return None, 1

    return whisper, 0


def decode_process_output(output):
    if not output:
        return ""

    preferred_encoding = locale.getpreferredencoding(False) or "utf-8"
    for encoding in (preferred_encoding, "utf-8", "gb18030"):
        try:
            return output.decode(encoding)
        except UnicodeDecodeError:
            continue

    return output.decode("utf-8", errors="replace")


def prepare_audio_file(input_path):
    if input_path.suffix.lower() != ".mp4":
        return input_path, 0

    mp3_path = input_path.with_suffix(".mp3")
    if mp3_path.is_file():
        print(f"已找到同名 MP3 文件，直接复用：{mp3_path}", file=sys.stderr)
        return mp3_path, 0

    print(f"未找到同名 MP3 文件，开始从 MP4 提取音频：{input_path}", file=sys.stderr)
    command = [
        "ffmpeg",
        "-i",
        str(input_path),
        "-vn",
        "-codec:a",
        "libmp3lame",
        "-q:a",
        "2",
        str(mp3_path),
    ]

    try:
        completed = subprocess.run(command, check=False, capture_output=True)
    except OSError as exc:
        print(f"执行 ffmpeg 失败：{exc}", file=sys.stderr)
        return None, 1

    if completed.returncode != 0:
        error_message = (
            decode_process_output(completed.stderr).strip()
            or decode_process_output(completed.stdout).strip()
            or "未知错误"
        )
        print(f"MP3 提取失败：{error_message}", file=sys.stderr)
        return None, 1

    print(f"MP3 音频已提取完成：{mp3_path}", file=sys.stderr)
    return mp3_path, 0


def transcribe_file(args):
    whisper, status = ensure_dependencies()
    if status != 0:
        return status

    input_path = args.input.expanduser().resolve()
    if not input_path.is_file():
        print(f"输入文件不存在：{input_path}", file=sys.stderr)
        return 1

    audio_path, status = prepare_audio_file(input_path)
    if status != 0:
        return status

    output_path = (
        args.output.expanduser().resolve()
        if args.output
        else audio_path.with_suffix(".txt")
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    load_kwargs = {}
    if args.device:
        load_kwargs["device"] = args.device

    print(f"正在加载 Whisper 模型：{args.model}", file=sys.stderr)
    model = whisper.load_model(args.model, **load_kwargs)

    transcribe_kwargs = {
        "task": args.task,
        "temperature": args.temperature,
        "verbose": args.verbose,
        "fp16": False,
    }
    if args.language:
        transcribe_kwargs["language"] = args.language

    print(f"正在根据音频文件进行转写：{audio_path}", file=sys.stderr)
    result = model.transcribe(str(audio_path), **transcribe_kwargs)
    text = result.get("text", "").strip()

    with output_path.open("w", encoding="utf-8") as file:
        file.write(text)
        if text:
            file.write("\n")

    print(f"转写结果已保存到：{output_path}", file=sys.stderr)
    return 0


def main():
    args = parse_args()
    raise SystemExit(transcribe_file(args))


if __name__ == "__main__":
    main()
