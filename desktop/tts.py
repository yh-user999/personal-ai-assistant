"""语音播报：Windows 自带 SAPI（System.Speech）——零依赖、离线、中文女声。

设计：只播"问候"这类短句（默认开启）；AI 回复播报默认关闭（防烦人，.env 可开）。
"""
import os
import subprocess
import sys

VOICE_GREETING = os.environ.get("VOICE_GREETING", "true").lower() == "true"
VOICE_REPLY = os.environ.get("VOICE_REPLY", "false").lower() == "true"


def speak(text: str, rate: int = 0) -> None:
    """异步语音播报（不阻塞 UI）。非 Windows 环境静默跳过。"""
    if not text or sys.platform != "win32":
        return
    # 清理单引号防注入；截断过长文本
    clean = text.replace("'", "").replace('"', "")[:200]
    if not clean.strip():
        return
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.Rate = {rate}; $s.Speak('{clean}')"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", ps],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        pass  # 语音失败不影响功能


def speak_greeting(greeting_text: str) -> None:
    """播报问候语的主句（"|"分隔的第一段，如"早上好 ☀️"）。"""
    if not VOICE_GREETING:
        return
    main = greeting_text.split("|")[0].strip()
    speak(main)
