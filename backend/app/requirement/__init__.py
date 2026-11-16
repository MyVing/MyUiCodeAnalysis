"""需求驱动的页面定位。

分层原则（决定了本包的结构）：
  - 召回（recall / lexicon / text）是纯代码、确定性、可单测的，产出「候选」
  - 判定（judge / validate）才用模型，且模型只能从候选里选，不得发明 nodeId
  - 结果落 RequirementMap（独立契约），不与 IR 事实层混在一起
"""
