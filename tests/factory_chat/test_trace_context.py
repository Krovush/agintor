from __future__ import annotations


def test_provider_mutator_requests_carry_factory_trace_context() -> None:
    from agintor.contracts import ModelResponse, OpenAITraceContext
    from agintor.search.mutators import ProviderPatchMutator

    captured = {}

    class CapturingProvider:
        provider_name = "openai"

        def generate(self, request):
            captured["metadata"] = request.metadata
            return ModelResponse(text="<<<SEARCH\nold\n===\nnew\n>>>REPLACE")

    trace_context = OpenAITraceContext(
        factory_chat_id="chat.alpha",
        factory_message_id="fmsg.1",
        factory_message_index=1,
    )
    mutator = ProviderPatchMutator(CapturingProvider())

    mutator._request_patch(
        instructions="Return a patch.",
        prompt="Patch something.",
        model_class="large",
        seed=0,
        mode="patch",
        trace_context=trace_context,
    )

    metadata = captured["metadata"]
    assert metadata["trace_context"]["factory_chat_id"] == "chat.alpha"
    assert metadata["trace_context"]["factory_message_id"] == "fmsg.1"
