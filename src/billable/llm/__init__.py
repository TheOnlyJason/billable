"""LLM client and the two-stage pipeline (cluster + narrate).

`client.LLMClient` is a provider-agnostic interface so the v2 product can
swap OpenAI for Anthropic, Azure OpenAI, or Bedrock without touching the
stage code. v1 ships only with `OpenAIClient`.

See DESIGN.md §3 for why we use two stages instead of one big prompt.
"""
