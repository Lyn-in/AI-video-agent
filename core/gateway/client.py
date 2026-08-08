"""
模型网关：所有大模型调用的唯一出口。

设计取舍：
- 用 urllib（stdlib）而非 SDK，整个平台零强制外部依赖，部署即跑。
- provider 抽象：anthropic / deepseek 都走 messages 风格，差异封在适配层。
- 每个 skill 独立绑定模型与 temperature（作家可高温发散，分镜必须低温求稳）。
- dry_run 模式返回结构化占位产物，让流水线在没有 API key 时也能跑通 ——
  这对碎片时间开发很重要，改引擎逻辑不必每次烧 token。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import keystore


class GatewayError(Exception):
    pass


# 厂商注册表。除 anthropic 外都是 messages 兼容协议（OpenAI 风格 chat/completions），
# 差异只在 url + 认证头，新增一个厂商只需要在这里加一行。
PROVIDERS = {
    "anthropic": {
        "label": "Anthropic（Claude）",
        "url": "https://api.anthropic.com/v1/messages",
        "default_model": "claude-sonnet-4-6",
        "key_env": "ANTHROPIC_API_KEY",
        "style": "anthropic",
        "hint": "console.anthropic.com → API Keys",
    },
    "deepseek": {
        "label": "DeepSeek",
        "url": "https://api.deepseek.com/v1/chat/completions",
        "default_model": "deepseek-chat",
        "key_env": "DEEPSEEK_API_KEY",
        "style": "openai",
        "hint": "platform.deepseek.com → API keys",
    },
    "openai": {
        "label": "OpenAI（GPT）",
        "url": "https://api.openai.com/v1/chat/completions",
        "default_model": "gpt-4o",
        "key_env": "OPENAI_API_KEY",
        "style": "openai",
        "hint": "platform.openai.com → API keys",
    },
    "moonshot": {
        "label": "月之暗面（Kimi）",
        "url": "https://api.moonshot.cn/v1/chat/completions",
        "default_model": "moonshot-v1-8k",
        "key_env": "MOONSHOT_API_KEY",
        "style": "openai",
        "hint": "platform.moonshot.cn → API Key 管理",
    },
    "zhipu": {
        "label": "智谱（GLM）",
        "url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "default_model": "glm-4-flash",
        "key_env": "ZHIPU_API_KEY",
        "style": "openai",
        "hint": "open.bigmodel.cn → API Keys",
    },
    "qwen": {
        "label": "通义千问（Qwen）",
        "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "default_model": "qwen-plus",
        "key_env": "QWEN_API_KEY",
        "style": "openai",
        "hint": "dashscope.console.aliyun.com → API-KEY 管理",
    },
    "doubao": {
        "label": "火山引擎（豆包）",
        "url": "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        "default_model": "doubao-pro-32k",
        "key_env": "DOUBAO_API_KEY",
        "style": "openai",
        "hint": "console.volcengine.com/ark → API Key",
    },
}


@dataclass
class CallResult:
    text: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_sec: float = 0.0
    dry_run: bool = False

    def json_payload(self) -> dict:
        """从回复中抽出 JSON。模型偶尔会加围栏或前言，这里做容错。"""
        t = self.text.strip()
        if "```" in t:
            start = t.find("```")
            fence_end = t.find("\n", start)
            end = t.rfind("```")
            if fence_end != -1 and end > fence_end:
                t = t[fence_end + 1:end].strip()
        if not t.startswith("{"):
            i = t.find("{")
            j = t.rfind("}")
            if i != -1 and j > i:
                t = t[i:j + 1]
        try:
            return json.loads(t)
        except json.JSONDecodeError as e:
            raise GatewayError(
                f"模型未返回合法 JSON：{e}\n原始输出前 300 字：\n{self.text[:300]}"
            )


@dataclass
class ModelGateway:
    log_path: Path | None = None
    max_retries: int = 3
    timeout: int = 300
    dry_run: bool = False
    calls: list[dict] = field(default_factory=list)

    # ---------- 对外唯一入口 ----------
    def call(self, *, system: str, user: str, model_config: dict,
             skill_id: str = "", tag: str = "") -> CallResult:
        provider = model_config.get("provider", "anthropic")
        model = model_config.get("model", "claude-sonnet-4-6")
        temperature = model_config.get("temperature", 1.0)
        max_tokens = model_config.get("max_tokens", 8000)

        if self.dry_run:
            res = CallResult(
                text=_dry_run_stub(skill_id, tag),
                provider=provider, model=model, dry_run=True,
            )
            self._log(skill_id, tag, res, system, user)
            return res

        if provider not in PROVIDERS:
            raise GatewayError(f"未知 provider {provider!r}，支持: {list(PROVIDERS)}")
        cfg = PROVIDERS[provider]
        key = keystore.get_key(provider, cfg["key_env"])
        if not key:
            raise GatewayError(
                f"还没有配置 {cfg['label']} 的密钥。"
                f"去网页上的「密钥设置」页填一下，或用 --dry-run 在无密钥情况下跑通流程。"
            )

        overrides = keystore.get_provider_config(provider)
        url = overrides.get("base_url") or cfg["url"]
        model = overrides.get("model") or model

        t0 = time.time()
        last_err = None
        for attempt in range(1, self.max_retries + 1):
            try:
                if cfg["style"] == "anthropic":
                    res = self._call_anthropic(
                        url, key, model, system, user, temperature, max_tokens)
                else:
                    res = self._call_openai_style(
                        url, key, model,
                        system, user, temperature, max_tokens, provider)
                res.elapsed_sec = round(time.time() - t0, 2)
                self._log(skill_id, tag, res, system, user)
                return res
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "ignore")[:400]
                last_err = f"HTTP {e.code}: {body}"
                if e.code in (400, 401, 403):   # 不可重试
                    break
            except Exception as e:                # noqa: BLE001
                last_err = str(e)
            if attempt < self.max_retries:
                time.sleep(2 ** attempt)

        raise GatewayError(f"调用失败（重试 {self.max_retries} 次）: {last_err}")

    # ---------- provider 适配 ----------
    def _call_anthropic(self, url, key, model, system, user,
                        temperature, max_tokens) -> CallResult:
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        data = self._post(url, body, {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        })
        text = "".join(
            b.get("text", "") for b in data.get("content", [])
            if b.get("type") == "text"
        )
        usage = data.get("usage", {})
        return CallResult(
            text=text, provider="anthropic", model=model,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )

    def _call_openai_style(self, url, key, model, system, user,
                           temperature, max_tokens, provider) -> CallResult:
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        data = self._post(url, body, {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        })
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        return CallResult(
            text=text, provider=provider, model=model,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
        )

    def _post(self, url: str, body: dict, headers: dict) -> dict:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    # ---------- 计费日志 ----------
    def _log(self, skill_id, tag, res: CallResult, system: str, user: str):
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "skill": skill_id,
            "tag": tag,
            "provider": res.provider,
            "model": res.model,
            "in_tokens": res.input_tokens,
            "out_tokens": res.output_tokens,
            "prompt_chars": len(system) + len(user),
            "elapsed": res.elapsed_sec,
            "dry_run": res.dry_run,
        }
        self.calls.append(rec)
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------- dry-run 占位产物 ----------

def _dry_run_stub(skill_id: str, tag: str) -> str:
    """
    返回一份结构完整、字段齐全的假产物，用于在无 API key 时跑通引擎。
    注意：故意保持内容占位感，避免被误当成真产物。
    """
    from ._stubs import STUBS
    key = tag or skill_id
    return json.dumps(STUBS.get(key, {"_dry_run": True, "skill": skill_id}),
                      ensure_ascii=False)
