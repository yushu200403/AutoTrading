"""OpenAI 兼容模型的交易决策适配器。"""

import logging
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from openai import OpenAI

from config import get_config
from app.bot.exceptions import OpenNOF1Error
from app.bot.prompts import build_system_prompt, build_user_prompt
from app.bot.xml_parser import XMLParseError, ToolCall, has_memory_update, parse_tool_calls


logger = logging.getLogger(__name__)


class AIAgentError(OpenNOF1Error):
    """模型请求或响应协议无效。"""


@dataclass
class AIResponse:
    """完成严格校验的模型响应。"""

    raw_response: str
    tool_calls: List[ToolCall]
    has_memory_update: bool
    model: str
    provider: str
    usage: dict

    @property
    def reasoning(self) -> str:
        if not self.raw_response:
            return ""
        match = re.search(r'<tooluse>', self.raw_response, re.IGNORECASE)
        return self.raw_response[:match.start()].strip() if match else self.raw_response


@dataclass
class AIProvider:
    """单个模型提供商配置。"""

    name: str
    api_key: str
    base_url: str
    model: str
    timeout_seconds: int
    client: Optional[OpenAI] = None

    def __post_init__(self):
        if self.is_configured:
            self.client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                max_retries=1,
            )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)


class AIAgent:
    """支持主备故障转移的模型代理。"""

    def __init__(self, api_key: str = None, base_url: str = None):
        config = get_config()
        self.config = config
        self.provider1 = AIProvider(
            "provider1",
            api_key or config.AI_1_API_KEY,
            base_url or config.AI_1_BASE_URL,
            config.AI_1_MODEL,
            config.AI_TIMEOUT_SECONDS,
        )
        self.provider2 = None
        if config.AI_2_API_KEY:
            self.provider2 = AIProvider(
                "provider2",
                config.AI_2_API_KEY,
                config.AI_2_BASE_URL,
                config.AI_2_MODEL,
                config.AI_TIMEOUT_SECONDS,
            )

        self.api_key = self.provider1.api_key
        self.base_url = self.provider1.base_url
        self.client = self.provider1.client
        self.model = self.provider1.model
        if not self.provider1.is_configured:
            logger.warning("未配置主模型提供商")

    def analyze(
        self,
        market_context: str,
        custom_instructions: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AIResponse:
        system_prompt = build_system_prompt(self.config)
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": build_user_prompt(market_context, custom_instructions),
            },
        ]
        return self.analyze_with_messages(messages, temperature, max_tokens)

    def analyze_with_messages(
        self,
        messages: list,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AIResponse:
        if not self.provider1.is_configured:
            raise AIAgentError("未配置主模型提供商")
        selected_temperature = (
            self.config.AI_TEMPERATURE if temperature is None else temperature
        )
        if not 0 <= selected_temperature <= 2:
            raise AIAgentError("temperature 必须在 0 到 2 之间")
        selected_max_tokens = (
            self.config.AI_MAX_RESPONSE_TOKENS if max_tokens is None else max_tokens
        )
        if selected_max_tokens <= 0:
            raise AIAgentError("max_tokens 必须大于 0")
        # 重试会在同一组消息上追加纠错内容，这里在发起付费请求前拦截超长提示词
        prompt_chars = sum(len(str(item.get("content", ""))) for item in messages)
        if prompt_chars > self.config.AI_MAX_PROMPT_CHARS:
            raise AIAgentError(
                f"提示词长度 {prompt_chars} 字符超过 AI_MAX_PROMPT_CHARS="
                f"{self.config.AI_MAX_PROMPT_CHARS}，已放弃请求以避免超出模型上下文"
            )

        response, provider = self._complete(
            messages, selected_temperature, selected_max_tokens
        )
        return self._parse_response(response, provider)

    def _complete(
        self, messages: list, temperature: float, max_tokens: int
    ) -> Tuple[object, AIProvider]:
        errors = []
        providers = [self.provider1]
        if self.provider2 and self.provider2.is_configured:
            providers.append(self.provider2)
        for provider in providers:
            try:
                logger.info("正在请求模型提供商 %s", provider.name)
                response = provider.client.chat.completions.create(
                    model=provider.model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                return response, provider
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")
                logger.warning("模型提供商 %s 请求失败: %s", provider.name, exc)
        raise AIAgentError("所有模型提供商均请求失败: " + "；".join(errors))

    def _parse_response(self, response, provider: AIProvider) -> AIResponse:
        if not response.choices:
            raise AIAgentError("模型响应 choices 为空")
        choice = response.choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason not in {None, "stop"}:
            raise AIAgentError(f"模型响应未完整结束: {finish_reason}")
        message = getattr(choice, "message", None)
        raw_response = getattr(message, "content", None)
        if not isinstance(raw_response, str):
            raise AIAgentError("模型响应 content 必须是字符串")
        if not raw_response.strip():
            raise AIAgentError("模型响应为空")
        try:
            tool_calls = parse_tool_calls(raw_response, strict=True)
        except XMLParseError as exc:
            raise AIAgentError(f"模型工具协议无效: {exc}") from exc
        if not tool_calls:
            raise AIAgentError("模型未返回工具调用")
        if not has_memory_update(tool_calls):
            raise AIAgentError("模型响应缺少 update_memory")

        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if response.usage:
            usage = {
                "prompt_tokens": getattr(response.usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(response.usage, "completion_tokens", 0) or 0,
                "total_tokens": getattr(response.usage, "total_tokens", 0) or 0,
            }
        model = getattr(response, "model", None) or getattr(
            provider, "model", provider.name
        )
        return AIResponse(
            raw_response=raw_response,
            tool_calls=tool_calls,
            has_memory_update=True,
            model=str(model),
            provider=provider.name,
            usage=usage,
        )
