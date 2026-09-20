"""赛务判定后端应用包。

模块划分：

- ``timeutil``：业务时间解析，原始时区与 UTC 并存
- ``events``：只追加事件日志、幂等去重、确定性排序
- ``state``：事件归约出的赛务状态投影
- ``scoring``：成绩、名次、违规与奖励判定
- ``medical``：脱敏救治调度与按岗可见
- ``api``：HTTP 路由与权限
- ``scenarios``：冒名、并列、跨枪、急救等重演脚本
"""

SERVICE_ID = "marathon-adjudication"
