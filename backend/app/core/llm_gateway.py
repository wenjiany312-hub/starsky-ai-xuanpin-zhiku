"""
统一大模型网关（OpenAI 兼容）：MagPie 本地网关（默认、免 Key）/ DeepSeek / 智谱 GLM / OpenAI / Ollama。

- 服务商 Key 与地址持久化在 config/local/settings.json（重启不丢）。
- 按“角色”选模型：strong=综合研判与报告，fast=意图识别/翻译，agent=带工具执行（Skill 类方法论）。
- 支持 tools / tool_calls（OpenAI function calling 格式），供 Agent 调用 MCP 数据工具。
- MagPie 模型列表实时读取 /v1/models，界面显示人类可读名（如“永不卡住”）。
"""
import time
from typing import Any, Dict, List, Optional

import httpx

from app.core.config import settings
from app.core.logger import logger
from app.core.settings_store import settings_store

ROLES = ("strong", "fast", "agent")
ROLE_LABELS = {"strong": "综合研判与报告", "fast": "意图识别与翻译", "agent": "Skill 方法论执行（带工具）"}

_PROVIDER_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "magpie": {
        "name": "MagPie 本地网关",
        "base_url": settings.MAGPIE_BASE_URL,
        "needs_key": False,
        "models": ["group/never-stuck", "group/group", "claude/claude-opus-5-5", "claude/claude-sonnet-5-5"],
    },
    "deepseek": {
        "name": "DeepSeek",
        "base_url": settings.DEEPSEEK_BASE_URL,
        "api_key": settings.DEEPSEEK_API_KEY,
        "needs_key": True,
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
    "glm": {
        "name": "智谱 GLM",
        "base_url": settings.GLM_BASE_URL,
        "api_key": settings.GLM_API_KEY,
        "needs_key": True,
        "models": ["GLM-5.3", "GLM-5.3-Flash"],
    },
    "openai": {
        "name": "OpenAI 兼容服务",
        "base_url": settings.OPENAI_BASE_URL,
        "api_key": settings.OPENAI_API_KEY,
        "needs_key": True,
        "models": ["gpt-4o", "gpt-4o-mini"],
    },
    "ollama": {
        "name": "本地 Ollama",
        "base_url": settings.OLLAMA_BASE_URL,
        "needs_key": False,
        "models": ["qwen2.5:14b"],
    },
}


class LLMError(RuntimeError):
    pass


class LLMUnavailable(LLMError):
    """连不上 / 超时 / 鉴权失败 / 服务端 5xx：换更大的 token 预算也没用，调用方不该再重试。"""


class LLMGateway:
    def __init__(self):
        self._model_cache: Dict[str, Dict[str, Any]] = {}  # provider -> {"at": ts, "models": [...]}

    # ---------------- 服务商配置 ----------------

    def provider_config(self, provider_id: str) -> Dict[str, Any]:
        if provider_id not in _PROVIDER_DEFAULTS:
            raise LLMError(f"未知的模型服务商: {provider_id}")
        cfg = dict(_PROVIDER_DEFAULTS[provider_id])
        saved = settings_store.get("llm", "providers", provider_id, default={}) or {}
        for key in ("api_key", "base_url"):
            if saved.get(key):
                cfg[key] = saved[key]
        return cfg

    def is_configured(self, provider_id: str) -> bool:
        cfg = self.provider_config(provider_id)
        return (not cfg.get("needs_key")) or bool(cfg.get("api_key"))

    def update_provider(self, provider_id: str, api_key: Optional[str] = None,
                        base_url: Optional[str] = None) -> None:
        if provider_id not in _PROVIDER_DEFAULTS:
            raise LLMError(f"未知的模型服务商: {provider_id}")
        saved = settings_store.get("llm", "providers", provider_id, default={}) or {}
        if api_key is not None:
            saved["api_key"] = api_key.strip()
        if base_url:
            saved["base_url"] = base_url.strip()
        settings_store.set("llm", "providers", provider_id, saved)
        self._model_cache.pop(provider_id, None)
        logger.info("已保存模型服务商配置: %s", provider_id)

    # ---------------- 角色 → 模型 ----------------

    def role_model(self, role: str = "strong") -> Dict[str, str]:
        roles = settings_store.get("llm", "roles", default={}) or {}
        sel = roles.get(role) or roles.get("strong") or {"provider": "magpie", "model": "group/never-stuck"}
        return {"provider": sel.get("provider") or "magpie", "model": sel.get("model") or "group/never-stuck"}

    def set_role_model(self, role: str, provider_id: str, model: str) -> None:
        if role not in ROLES:
            raise LLMError(f"未知角色: {role}")
        self.provider_config(provider_id)  # 校验服务商存在
        settings_store.set("llm", "roles", role, {"provider": provider_id, "model": model})

    # 兼容旧接口：当前默认 = strong 角色
    @property
    def current_default_provider(self) -> str:
        return self.role_model("strong")["provider"]

    @property
    def current_default_model(self) -> str:
        return self.role_model("strong")["model"]

    # ---------------- 模型列表 ----------------

    async def list_models(self, provider_id: str, refresh: bool = False) -> List[Dict[str, Any]]:
        """返回 [{id, name, tools, vision, context}]；MagPie / Ollama 实时读取，失败回退静态表。"""
        cached = self._model_cache.get(provider_id)
        if cached and not refresh and time.time() - cached["at"] < 300:
            return cached["models"]
        cfg = self.provider_config(provider_id)
        models: List[Dict[str, Any]] = []
        if provider_id in ("magpie", "ollama", "openai", "deepseek", "glm") and self.is_configured(provider_id):
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(f"{cfg['base_url'].rstrip('/')}/models", headers=self._headers(cfg))
                    resp.raise_for_status()
                    for m in resp.json().get("data") or []:
                        mid = m.get("id")
                        if not mid:
                            continue
                        inputs = (m.get("modalities") or {}).get("input") or []
                        models.append({
                            "id": mid,
                            "name": m.get("display_name") or mid,
                            "tools": bool(m.get("tool_call")) or "tools" in (m.get("supported_parameters") or []),
                            "vision": "image" in inputs,
                            "context": m.get("context_length") or m.get("context_window"),
                            "group": m.get("owned_by"),
                        })
            except Exception as exc:
                logger.info("读取 %s 模型列表失败，使用静态表: %s", provider_id, exc)
        if not models:
            models = [{"id": mid, "name": mid, "tools": True, "vision": False, "context": None, "group": None}
                      for mid in cfg.get("models", [])]
        self._model_cache[provider_id] = {"at": time.time(), "models": models}
        return models

    async def model_display_name(self, provider_id: str, model_id: str) -> str:
        try:
            for m in await self.list_models(provider_id):
                if m["id"] == model_id:
                    return m["name"]
        except Exception:
            pass
        return model_id

    # ---------------- 调用 ----------------

    @staticmethod
    def _headers(cfg: Dict[str, Any]) -> Dict[str, str]:
        return {"Content-Type": "application/json", "Authorization": f"Bearer {cfg.get('api_key') or 'local'}"}

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        role: str = "strong",
        provider_id: Optional[str] = None,
        model: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.3,
        max_tokens: int = 8000,
        timeout: float = 300.0,
    ) -> Dict[str, Any]:
        """
        单轮调用。返回 {"content": str, "tool_calls": [...], "model": str, "provider": str, "usage": {...}}。
        失败抛 LLMError（调用方据此把结论标为“未研判”，绝不回落到模板）。
        """
        sel = self.role_model(role)
        provider_id = provider_id or sel["provider"]
        model = model or sel["model"]
        cfg = self.provider_config(provider_id)
        if cfg.get("needs_key") and not cfg.get("api_key"):
            raise LLMError(f"{cfg['name']} 未配置 API Key（设置 → 模型）")

        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        url = f"{cfg['base_url'].rstrip('/')}/chat/completions"
        started = time.perf_counter()
        # 推理型模型（如 group/never-stuck）先在内部想 2000~3000 token 再写正文，
        # 调用方按「正文长度」给的预算会被推理吃光，返回空正文 + finish_reason=length。
        # 这种情况在网关这一层抬高预算重试一次，所有调用方（翻译/研判/方法论提炼）一起受益。
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(url, headers=self._headers(cfg), json=payload)
            except httpx.TimeoutException as exc:
                raise LLMUnavailable(f"模型响应超时（{cfg['name']}，{timeout:.0f} 秒内没有返回）") from exc
            except httpx.HTTPError as exc:
                raise LLMUnavailable(f"模型服务不可达（{cfg['name']}）：{type(exc).__name__} {exc}") from exc
            if resp.status_code in (401, 403) or resp.status_code >= 500:
                raise LLMUnavailable(f"模型调用失败 HTTP {resp.status_code}：{resp.text[:300]}")
            if resp.status_code != 200:
                raise LLMError(f"模型调用失败 HTTP {resp.status_code}：{resp.text[:300]}")
            try:
                data = resp.json()
                choice = (data.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
            except Exception as exc:
                raise LLMError(f"模型返回无法解析：{resp.text[:200]}") from exc
            content = msg.get("content") or ""
            if isinstance(content, list):  # 部分网关返回分段内容
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            tool_calls = msg.get("tool_calls") or []
            cut = choice.get("finish_reason") == "length"
            details = (data.get("usage") or {}).get("completion_tokens_details") or {}
            reasoning = details.get("reasoning_tokens") or 0
            # 正文为空，或正文被截断且预算大半花在推理上（JSON 写到一半）——都是预算给少了
            starved = cut and (not content.strip() or reasoning >= payload["max_tokens"] * 0.5)
            if starved and attempt == 0 and not tool_calls:
                payload["max_tokens"] = min(max(payload["max_tokens"] * 3, payload["max_tokens"] + 6000), 32000)
                logger.info("LLM %s/%s 推理用掉 %s token、正文%s，预算抬到 %d 重试", provider_id, model,
                            reasoning or "?", "为空" if not content.strip() else "被截断", payload["max_tokens"])
                continue
            if content.strip() or tool_calls:
                break
            if cut:
                raise LLMError("模型把 token 预算都用在内部推理上，没写出正文")
            raise LLMError("模型返回空内容")
        logger.info("LLM %s/%s 用时 %.1fs", provider_id, model, time.perf_counter() - started)
        return {
            "content": content,
            "tool_calls": tool_calls,
            "model": data.get("model") or model,
            "provider": provider_id,
            "usage": data.get("usage") or {},
            "finish_reason": choice.get("finish_reason"),
        }

    async def complete(self, prompt: str, role: str = "strong", system: Optional[str] = None,
                       temperature: float = 0.3, max_tokens: int = 8000,
                       provider_id: Optional[str] = None, model: Optional[str] = None,
                       timeout: float = 300.0) -> str:
        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        res = await self.chat(messages, role=role, provider_id=provider_id, model=model,
                              temperature=temperature, max_tokens=max_tokens, timeout=timeout)
        return res["content"]

    async def test_connection(self, provider_id: str, model: Optional[str] = None) -> Dict[str, Any]:
        started = time.perf_counter()
        try:
            cfg = self.provider_config(provider_id)
            target = model or (cfg.get("models") or [""])[0]
            res = await self.chat([{"role": "user", "content": "只回复：OK"}], provider_id=provider_id,
                                  model=target, max_tokens=16, timeout=30.0)
            return {"status": "success", "provider": provider_id, "model": res["model"],
                    "latency_ms": round((time.perf_counter() - started) * 1000), "message": "连接正常"}
        except Exception as exc:
            return {"status": "fail", "provider": provider_id, "model": model,
                    "latency_ms": round((time.perf_counter() - started) * 1000), "message": str(exc)[:300]}

    def providers_summary(self) -> List[Dict[str, Any]]:
        out = []
        for pid in _PROVIDER_DEFAULTS:
            cfg = self.provider_config(pid)
            key = cfg.get("api_key") or ""
            out.append({
                "id": pid,
                "name": cfg["name"],
                "base_url": cfg["base_url"],
                "needs_key": bool(cfg.get("needs_key")),
                "is_configured": self.is_configured(pid),
                "key_hint": (key[:3] + "…" + key[-4:]) if len(key) > 10 else ("已设置" if key else ""),
            })
        return out


llm_gateway = LLMGateway()
