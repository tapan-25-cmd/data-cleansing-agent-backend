from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2_000)


class ChatResponse(BaseModel):
    intent: str
    action: str | None = None
    message: str


class ChatCoordinator:
    """Small workflow agent that keeps chat actions explicit and predictable."""

    def respond(self, request: ChatRequest) -> ChatResponse:
        normalized = request.message.casefold()
        uom_terms = ("unit measurement", "unit of measure", "uom", "clean", "standardize", "standardise")
        if any(term in normalized for term in uom_terms):
            return ChatResponse(
                intent="START_UOM_CLEANSING",
                action="REQUEST_WORKBOOK",
                message="Please upload the Excel workbook you want me to cleanse. I’ll preserve the workbook and populate the standardized size, UOM, and pack-size fields.",
            )
        return ChatResponse(
            intent="HELP",
            message="I can cleanse unit-of-measure data in an Excel workbook. Try: “Please do the unit measurement.”",
        )
