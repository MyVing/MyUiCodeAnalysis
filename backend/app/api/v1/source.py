"""源码片段读取：前端点开「文件:行」时用它跳回真实代码。

安全边界：这是唯一接受客户端文件路径的接口，因此路径必须经过越狱校验——
解析后的真实路径必须落在项目根目录内。符号链接与 `../` 都能被 resolve() 揭穿，
所以校验放在 resolve 之后，而不是拼接字符串时。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import ProjectServiceDep
from app.api.v1.projects import require_project
from app.schemas import Project, SourceExcerpt

router = APIRouter(prefix="/projects", tags=["source"])

DEFAULT_RADIUS = 30
MAX_RADIUS = 400
# 单次返回的字符上限：防止有人用 radius 拉走整个 bundle
MAX_CHARS = 200_000


class UnsafePathError(Exception):
    """请求的文件路径落在项目根目录之外。"""


def read_excerpt(project: Project, file: str, line: int | None, radius: int) -> SourceExcerpt:
    """读项目内的源码片段。所有错误都用可读的中文消息，前端直接展示。"""
    relative = Path(file)
    if relative.is_absolute():
        raise UnsafePathError(f"文件路径必须是相对项目根目录的相对路径：{file}")

    root = Path(project.root_path).resolve()
    target = (root / relative).resolve()
    # resolve() 之后再比较：这样 `..` 与指向外部目录的符号链接都会被拦下
    if target != root and root not in target.parents:
        raise UnsafePathError(f"文件路径越出了项目根目录：{file}")
    if not target.is_file():
        raise FileNotFoundError(file)

    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ValueError("该文件不是 UTF-8 文本，无法展示源码") from None

    lines = text.splitlines()
    if not lines:
        return SourceExcerpt(file=file, start_line=1, end_line=1, content="")

    radius = max(0, min(radius, MAX_RADIUS))
    if line is None:
        start_index = 0
        end_index = min(len(lines), max(radius * 2, 1))
    else:
        # line 是 1 起始的源码行号；越界时钳到文件范围内，而不是报错
        center = min(max(line, 1), len(lines)) - 1
        start_index = max(0, center - radius)
        end_index = min(len(lines), center + radius + 1)

    excerpt = "\n".join(lines[start_index:end_index])
    truncated = False
    if len(excerpt) > MAX_CHARS:
        excerpt = excerpt[:MAX_CHARS]
        truncated = True

    return SourceExcerpt(
        file=file,
        start_line=start_index + 1,
        end_line=end_index,
        content=excerpt,
        truncated=truncated,
    )


@router.get(
    "/{project_id}/source",
    response_model=SourceExcerpt,
    responses={
        403: {"description": "路径越狱"},
        404: {"description": "文件不存在"},
    },
)
def get_source_excerpt(
    project_id: str,
    service: ProjectServiceDep,
    file: str = Query(min_length=1),
    line: int | None = Query(default=None, ge=1),
    radius: int = Query(default=DEFAULT_RADIUS, ge=0, le=MAX_RADIUS),
) -> SourceExcerpt:
    project = require_project(project_id, service)
    try:
        return read_excerpt(project, file, line, radius)
    except UnsafePathError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from None
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"文件不存在：{file}"
        ) from None
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(error)
        ) from None
