"""页面画像的数据形状。

刻意与生成逻辑（`profile.py`）分开放：存储层要实现画像的读写，但没有任何理由
为此把模型接入、链路推导整条导入链拉进来。这个模块只有 pydantic 形状定义。

## 为什么画像不参与引文校验

`summary` 是模型生成的文本，不是源码原文。它只能帮助在线定位「理解这个页面」，
绝不能进入任何语料集合（`chains.corpus` / 页面 evidence）。否则模型可以先生成
一句「本页支持导出」，下一轮再引用它自称通过校验——自证循环，校验就失效了。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class ProfileModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class PageProfile(ProfileModel):
    """一个页面的画像。"""

    page_id: str = Field(alias="nodeId")
    name: str
    route_paths: list[str] = Field(default_factory=list)
    summary: str


class ProfilePayload(ProfileModel):
    """一份完整的页面画像产物：页面清单 + 生成信息 + 缺口说明。

    `warnings` 不是错误：它记录「哪些页面没写成」。定位时照常工作，只是这些页面
    在索引里少一段描述。
    """

    analysis_id: str
    generated_at: datetime
    model: str | None = None
    profiles: list[PageProfile] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def summary_of(self, page_id: str) -> str | None:
        for profile in self.profiles:
            if profile.page_id == page_id:
                return profile.summary
        return None
