"""语音播报：Windows 自带 SAPI——零依赖、离线。

多样性设计（解决"单调"）：
- 自动枚举系统已安装语音，问候时随机轮换（女声 Huihui / 男声 Kangkang 等）
- 语速在 -2~+1 间随机微调
- .env 可配置：VOICE_NAME（固定语音）/ VOICE_RANDOM（随机，默认开）
"""
import os
import random
import subprocess
import sys

VOICE_GREETING = os.environ.get("VOICE_GREETING", "true").lower() == "true"
VOICE_REPLY = os.environ.get("VOICE_REPLY", "false").lower() == "true"
VOICE_RANDOM = os.environ.get("VOICE_RANDOM", "true").lower() == "true"
VOICE_NAME = os.environ.get("VOICE_NAME", "").strip()

_voices_cache: list[str] | None = None  # None=未查询；[]=查询过但无结果


def list_installed_voices() -> list[str]:
    """枚举系统已安装语音（中文优先）。"""
    global _voices_cache
    if _voices_cache is not None:
        return _voices_cache
    names: list[str] = []
    if sys.platform == "win32":
        ps = (
            "Add-Type -AssemblyName System.Speech; "
            "(New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices() "
            "| ForEach-Object { $_.VoiceInfo.Name }"
        )
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            all_names = [l.strip() for l in r.stdout.splitlines() if l.strip()]
            # 中文优先，其余垫后
            names = sorted(all_names, key=lambda n: (0 if "Chinese" in n else 1, n))
        except Exception:
            names = []
    _voices_cache = names
    return names


def _pick_voice() -> str:
    """按配置选语音：指定名 > 随机 > 默认。"""
    voices = list_installed_voices()
    if VOICE_NAME:
        # 支持部分名匹配
        for v in voices:
            if VOICE_NAME.lower() in v.lower():
                return v
    if VOICE_RANDOM and voices:
        return random.choice(voices)
    return ""  # 不指定 = 系统默认


def speak(text: str) -> None:
    """异步语音播报（不阻塞 UI）。非 Windows 环境静默跳过。"""
    if not text or sys.platform != "win32":
        return
    clean = text.replace("'", "").replace('"', "")[:200]
    if not clean.strip():
        return
    voice = _pick_voice()
    rate = random.randint(-2, 1)  # 语速微调：-2 ~ +1
    ps = "Add-Type -AssemblyName System.Speech; "
    ps += "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    if voice:
        ps += f"$s.SelectVoice('{voice}'); "
    ps += f"$s.Rate = {rate}; $s.Speak('{clean}')"
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", ps],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        pass  # 语音失败不影响功能


def speak_greeting(greeting_text: str) -> None:
    """播报问候语的主句（"|"分隔的第一段）。"""
    if not VOICE_GREETING:
        return
    main = greeting_text.split("|")[0].strip()
    speak(main)
