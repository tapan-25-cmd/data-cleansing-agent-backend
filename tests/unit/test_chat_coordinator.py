from app.agents.chat_coordinator import ChatCoordinator, ChatRequest


def test_uom_chat_request_returns_workbook_upload_action():
    response = ChatCoordinator().respond(ChatRequest(message="Please do the unit measurement"))

    assert response.intent == "START_UOM_CLEANSING"
    assert response.action == "REQUEST_WORKBOOK"
    assert "upload" in response.message.lower()
