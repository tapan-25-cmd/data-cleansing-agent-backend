from app.agents.provider import InferenceRequest, InferenceResponse, InferenceResult, ProviderMetadata


class MockInferenceProvider:
    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        return InferenceResponse(
            result=InferenceResult(
                status="NOT_IN_DESCRIPTION",
                confidence="LOW",
                reason_code="NOT_IN_DESCRIPTION",
            ),
            metadata=ProviderMetadata(
                provider="mock",
                agent_name="mock_uom_inference",
                agent_version="1",
            ),
        )
