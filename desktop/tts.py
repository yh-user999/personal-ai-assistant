"""语音播报：Windows 自带 SAPI——零依赖、离线。

角色切换设计（用户可控，非随机）：
- 面板「🎙 语音」按钮列出系统已装语音角色，选中即试听并持久化
- 优先级：运行时选择（voice_config.json）> .env VOICE_NAME > 系统默认
"""
import json
import os
import subprocess
import sys
from pathlib import Path

VOICE_GREETING = os.environ.get("VOICE_GREETING", "true").lower() == "true"
VOICE_REPLY = os.environ.get("VOICE_REPLY", "false").lower() == "true"
VOICE_NAME = os.environ.get("VOICE_NAME", "").strip()

_CONFIG_FILE = Path(__file__).resolve().parent / "voice_config.json"

_voices_cache: list[str] | None = None  # None=未查询


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
            names = sorted(all_names, key=lambda n: (0 if "Chinese" in n else 1, n))
        except Exception:
            names = []
    _voices_cache = names
    return names


def get_current_voice() -> str:
    """当前选定语音：运行时选择 > .env 指定 > ''（系统默认）。"""
    if VOICE_NAME:
        return VOICE_NAME
    try:
        cfg = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        name = cfg.get("voice", "")
        # 名字需存在于系统语音列表，否则回退默认
        if name and name in list_installed_voices():
            return name
        if name:  # 存了但系统里没了（部分名匹配一次）
            for v in list_installed_voices():
                if name.lower() in v.lower():
                    return v
    except Exception:
        pass
    return ""


def set_voice(name: str) -> None:
    """持久化用户选择的语音角色。"""
    _CONFIG_FILE.write_text(
        json.dumps({"voice": name}, ensure_ascii=False), encoding="utf-8"
    )


def speak(text: str) -> None:
    """异步语音播报（不阻塞 UI）。非 Windows 环境静默跳过。"""
    if not text or sys.platform != "win32":
        return
    clean = text.replace("'", "").replace('"', "")[:200]
    if not clean.strip():
        return
    voice = get_current_voice()
    ps = "Add-Type -AssemblyName System.Speech; "
    ps += "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    if voice:
        ps += f"$s.SelectVoice('{voice}'); "
    ps += f"$s.Speak('{clean}')"
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
