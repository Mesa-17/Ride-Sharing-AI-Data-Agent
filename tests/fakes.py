"""A scripted stand-in for the chat models, so the graphs can be tested without an API key."""
from types import SimpleNamespace


class FakeLLM:
    """llm(level) -> model. Text replies and structured replies are popped from queues in order."""

    def __init__(self, texts=None, structured=None):
        self.texts = list(texts or [])
        self.structured = list(structured or [])
        self.prompts = []

    def __call__(self, level):
        return self

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return SimpleNamespace(content=self.texts.pop(0))

    def with_structured_output(self, schema):
        parent = self

        class _Structured:
            def invoke(self, prompt):
                parent.prompts.append(prompt)
                return parent.structured.pop(0)
        return _Structured()
