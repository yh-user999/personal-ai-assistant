"""简历命令解析测试。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("LLM_API_KEY", "sk-test")
os.environ.setdefault("EMBEDDING_API_KEY", "sk-test")

from app.services.resume import parse_resume_command


def test_parse_with_target_job():
    assert parse_resume_command("优化简历：目标岗位=阿里云运维工程师") == "阿里云运维工程师"


def test_parse_without_target():
    assert parse_resume_command("优化简历") == ""


def test_parse_not_resume_command():
    assert parse_resume_command("帮我看看简历里的技能") is None
    assert parse_resume_command("写文档 简历") is None
