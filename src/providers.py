# -*- coding: utf-8 -*-
"""AI 服务商预设：让"填个 Key 就能用"成为默认体验。

之前 API 配置要求用户手填 Base URL 和模型名，对不熟悉的人门槛太高。
这里内置主流服务商的端点与常用模型，界面只需选服务商 + 填 Key。

**安全约定**：本模块只有公开的端点与模型名，**绝不含任何密钥**。
密钥只存在 `data/settings.json`（已 gitignore）。
"""

from __future__ import annotations

from typing import Dict, List, Optional

__all__ = [
    "PROVIDERS",
    "provider_list",
    "find_provider",
    "models_for",
    "guess_provider",
]

#: 服务商预设。
#:
#: 字段:
#:   id        内部标识
#:   name      显示名
#:   base      OpenAI 兼容端点（不含 /chat/completions）
#:   models    常用模型（第一个作为默认）
#:   key_url   申请 Key 的地址（界面上给"去获取"链接）
#:   note      补充说明
PROVIDERS: List[Dict[str, object]] = [
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "base": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "key_url": "https://platform.deepseek.com/api_keys",
        "note": "性价比高，中文好",
    },
    {
        "id": "zhipu",
        "name": "智谱 GLM",
        "base": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4.7-flash", "glm-4-flash", "glm-4-plus", "glm-4-air"],
        "key_url": "https://open.bigmodel.cn/usercenter/apikeys",
        "note": "glm-4-flash 系列有免费额度",
    },
    {
        "id": "moonshot",
        "name": "月之暗面 Kimi",
        "base": "https://api.moonshot.cn/v1",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "kimi-k2-0905-preview"],
        "key_url": "https://platform.moonshot.cn/console/api-keys",
        "note": "长文本友好",
    },
    {
        "id": "dashscope",
        "name": "阿里通义千问",
        "base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-plus", "qwen-turbo", "qwen-max", "qwen-long"],
        "key_url": "https://bailian.console.aliyun.com/?apiKey=1",
        "note": "DashScope 兼容模式",
    },
    {
        "id": "siliconflow",
        "name": "硅基流动",
        "base": "https://api.siliconflow.cn/v1",
        "models": ["Qwen/Qwen2.5-7B-Instruct", "deepseek-ai/DeepSeek-V3", "THUDM/glm-4-9b-chat"],
        "key_url": "https://cloud.siliconflow.cn/account/ak",
        "note": "聚合多家开源模型，部分免费",
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "base": "https://api.openai.com/v1",
        "models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"],
        "key_url": "https://platform.openai.com/api-keys",
        "note": "需要海外网络",
    },
    {
        "id": "custom",
        "name": "自定义",
        "base": "",
        "models": [],
        "key_url": "",
        "note": "任何 OpenAI 兼容端点",
    },
]


def provider_list() -> List[Dict[str, object]]:
    """给前端的服务商清单（深拷贝，避免调用方改到常量）。"""
    return [
        {
            "id": item["id"],
            "name": item["name"],
            "base": item["base"],
            "models": list(item["models"]),  # type: ignore[arg-type]
            "key_url": item["key_url"],
            "note": item["note"],
        }
        for item in PROVIDERS
    ]


def find_provider(provider_id: str) -> Optional[Dict[str, object]]:
    """按 id 找服务商。"""
    for item in PROVIDERS:
        if item["id"] == provider_id:
            return item
    return None


def models_for(provider_id: str) -> List[str]:
    """某服务商的模型列表。"""
    item = find_provider(provider_id)
    return list(item["models"]) if item else []  # type: ignore[arg-type]


def guess_provider(base_url: str) -> str:
    """根据 Base URL 反推服务商 id（用于回填界面）。"""
    url = (base_url or "").strip().rstrip("/").lower()
    if not url:
        return ""
    for item in PROVIDERS:
        base = str(item["base"] or "").rstrip("/").lower()
        if base and (url == base or url.startswith(base)):
            return str(item["id"])
    return "custom"
