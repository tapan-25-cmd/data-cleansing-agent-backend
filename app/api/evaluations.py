from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status

from app.services.evaluation_service import load_evaluation_catalog


router = APIRouter(prefix="/evaluations", tags=["evaluations"])


@router.get("/latest")
def latest_evaluation() -> dict:
    return load_evaluation_catalog()


@router.get("/agent")
def agent_evaluations(request: Request) -> dict:
    """Hard-case evaluation runs, newest first, plus the size of the case set."""
    from app.services.agent_evaluation_service import load_cases
    version, cases = load_cases()
    state = request.app.state
    return {
        "cases_version": version, "cases": len(cases),
        "running": getattr(state, "agent_evaluation_running", False),
        "runs": state.repositories.agent_evaluations(),
    }


@router.post("/agent/run", status_code=status.HTTP_202_ACCEPTED)
def run_agent_evaluation(request: Request, tasks: BackgroundTasks) -> dict:
    """Runs every hard case through the live AI: one paid call per case."""
    state = request.app.state
    if not state.ai_reading_test.provider_is_real:
        raise HTTPException(409, "The AI is switched off on this server (AI_PROVIDER=mock)")
    if getattr(state, "agent_evaluation_running", False):
        raise HTTPException(409, "An evaluation is already running")
    state.agent_evaluation_running = True

    def run() -> None:
        try:
            state.repositories.save_agent_evaluation(state.agent_evaluation.run())
        finally:
            state.agent_evaluation_running = False

    tasks.add_task(run)
    return {"status": "RUNNING"}
