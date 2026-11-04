"""Optional SDK adapter. The core accepts any injected JSON callable."""

from __future__ import annotations

import json


class JSONChatError(RuntimeError):
    def __init__(
        self, message, *, error_type=None, status_code=None, provider_code=None, finish_reason=None
    ):
        super().__init__(message)
        self.diagnostic = {
            "error_type": error_type,
            "status_code": status_code,
            "provider_code": provider_code,
            "finish_reason": finish_reason,
        }


class OpenAICompatibleJSONChat:
    def __init__(self, client, model, *, mode="json_object", max_tokens=4096, extra_body=None):
        if mode not in ("json_object", "json_schema", "prompt"):
            raise ValueError("Unsupported structured-output mode")
        self.client, self.model, self.mode = client, model, mode
        self.max_tokens = max_tokens
        self.extra_body = extra_body or {}
        self.calls = 0

    def __call__(self, system, inp, schema):
        options = {}
        if self.mode == "json_object":
            options["response_format"] = {"type": "json_object"}
        elif self.mode == "json_schema":
            options["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "memory_output", "strict": True, "schema": schema},
            }
        if self.extra_body:
            options["extra_body"] = self.extra_body
        self.calls += 1
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": system
                        + "\n\n输出 JSON 必须符合以下 Schema：\n"
                        + json.dumps(schema, ensure_ascii=False),
                    },
                    {"role": "user", "content": json.dumps(inp, ensure_ascii=False)},
                ],
                max_tokens=self.max_tokens,
                **options,
            )
        except Exception as exc:
            raise JSONChatError(
                "Memory model request failed; check provider configuration",
                error_type=type(exc).__name__,
                status_code=getattr(exc, "status_code", None),
                provider_code=getattr(exc, "code", None),
            ) from None
        if not response.choices or response.choices[0].finish_reason != "stop":
            raise JSONChatError(
                "Memory model did not complete its JSON output",
                finish_reason=response.choices[0].finish_reason if response.choices else None,
            )
        content = response.choices[0].message.content
        if not content:
            raise JSONChatError("Memory model returned empty content")
        return content
