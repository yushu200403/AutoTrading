"""AI 提供商故障转移与响应协议回归测试。"""

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.bot import ai_agent as ai_module
from app.bot.ai_agent import AIAgent, AIAgentError, AIProvider


def _tool_block(name="update_memory", args=None):
    payload = {
        "name": name,
        "info": "测试工具",
        "args": args or {"content": "继续等待"},
    }
    return f"<tooluse>{json.dumps(payload)}</tooluse>"


def _response(content, finish_reason="stop", usage=True):
    usage_value = None
    if usage:
        usage_value = SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        )
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content),
            )
        ],
        usage=usage_value,
        model="test-model",
    )


class FakeCompletions:
    def __init__(self, api_key):
        self.api_key = api_key
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.api_key == "primary-key":
            raise RuntimeError("主提供商不可用")
        return _response("谨慎观察\n" + _tool_block())


class FakeOpenAI:
    def __init__(self, api_key, base_url, timeout, max_retries):
        self.chat = SimpleNamespace(completions=FakeCompletions(api_key))


class AgentConfig:
    AI_1_API_KEY = "primary-key"
    AI_1_BASE_URL = "https://primary.invalid/v1"
    AI_1_MODEL = "primary-model"
    AI_2_API_KEY = "backup-key"
    AI_2_BASE_URL = "https://backup.invalid/v1"
    AI_2_MODEL = "backup-model"
    AI_TIMEOUT_SECONDS = 3
    AI_TEMPERATURE = 0.2
    AI_MAX_RESPONSE_TOKENS = 8000
    AI_MAX_PROMPT_CHARS = 120000
    TRADING_INTERVAL_MINUTES = 3
    RISK_MIN_PROTECTIVE_DISTANCE_PERCENT = Decimal("0.3")
    RISK_MAX_LEVERAGE = 20
    RISK_MAX_SINGLE_TRADE_USDT = 1000
    RISK_MAX_POSITION_NOTIONAL_USDT = 3000
    RISK_MAX_TOTAL_NOTIONAL_USDT = 5000
    RISK_REQUIRE_PROTECTIVE_ORDER = True


def test_agent_falls_back_and_parses_complete_response(monkeypatch):
    monkeypatch.setattr(ai_module, "get_config", lambda: AgentConfig)
    monkeypatch.setattr(ai_module, "OpenAI", FakeOpenAI)
    agent = AIAgent()

    result = agent.analyze("真实行情上下文", "保持谨慎")

    assert result.provider == "provider2"
    assert result.model == "test-model"
    assert result.reasoning == "谨慎观察"
    assert result.usage["total_tokens"] == 15
    assert result.has_memory_update
    assert result.tool_calls[0].name == "update_memory"


def test_agent_rejects_invalid_parameters_and_responses():
    agent = AIAgent.__new__(AIAgent)
    agent.config = SimpleNamespace(AI_TEMPERATURE=0.2)
    agent.provider1 = AIProvider("未配置", "", "", "", 1)
    agent.provider2 = None

    with pytest.raises(AIAgentError, match="未配置"):
        agent.analyze_with_messages([])

    agent.provider1 = SimpleNamespace(is_configured=True)
    with pytest.raises(AIAgentError, match="temperature"):
        agent.analyze_with_messages([], temperature=3)
    with pytest.raises(AIAgentError, match="max_tokens"):
        agent.analyze_with_messages([], max_tokens=0)

    provider = SimpleNamespace(name="测试提供商")
    with pytest.raises(AIAgentError, match="choices"):
        agent._parse_response(SimpleNamespace(choices=[]), provider)
    with pytest.raises(AIAgentError, match="未完整结束"):
        agent._parse_response(_response(_tool_block(), "length"), provider)
    with pytest.raises(AIAgentError, match="响应为空"):
        agent._parse_response(_response("  "), provider)
    with pytest.raises(AIAgentError, match="content 必须是字符串"):
        agent._parse_response(_response(["非法内容"]), provider)
    missing_message = _response(_tool_block())
    missing_message.choices[0] = SimpleNamespace(finish_reason="stop")
    with pytest.raises(AIAgentError, match="content 必须是字符串"):
        agent._parse_response(missing_message, provider)
    with pytest.raises(AIAgentError, match="工具协议无效"):
        agent._parse_response(_response("<tooluse>{非法}</tooluse>"), provider)
    with pytest.raises(AIAgentError, match="未返回工具"):
        agent._parse_response(_response("只有分析，没有工具"), provider)
    with pytest.raises(AIAgentError, match="缺少 update_memory"):
        agent._parse_response(
            _response(
                _tool_block(
                    "cancel_orders",
                    {"target": "BTC/USDT", "order_type": "all"},
                )
            ),
            provider,
        )

    parsed = agent._parse_response(_response(_tool_block(), usage=False), provider)
    assert parsed.usage == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }

    response = _response(_tool_block())
    response.usage = SimpleNamespace(
        prompt_tokens=None,
        completion_tokens=5,
        total_tokens=None,
    )
    parsed = agent._parse_response(response, provider)
    assert parsed.usage == {
        "prompt_tokens": 0,
        "completion_tokens": 5,
        "total_tokens": 0,
    }
