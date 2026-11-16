"""任务查询：分析任务的状态与统计。"""

from fastapi import APIRouter, HTTPException, status

from app.api.deps import AnalysisRepositoryDep
from app.schemas import AnalysisTask

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get(
    "/{task_id}",
    response_model=AnalysisTask,
    responses={404: {"description": "任务不存在"}},
)
def get_task(task_id: str, repository: AnalysisRepositoryDep) -> AnalysisTask:
    task = repository.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"任务 {task_id} 不存在")
    return task
