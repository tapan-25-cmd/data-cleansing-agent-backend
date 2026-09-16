from typing import Annotated

from fastapi import APIRouter, Depends

from app.agents.chat_coordinator import ChatCoordinator, ChatRequest, ChatResponse
from app.api.dependencies import chat_coordinator

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/messages")
def send_message(
    request: ChatRequest,
    coordinator: Annotated[ChatCoordinator, Depends(chat_coordinator)],
) -> ChatResponse:
    return coordinator.respond(request)
