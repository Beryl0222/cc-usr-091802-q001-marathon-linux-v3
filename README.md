# 马拉松赛务判定中枢

面向四万人规模赛事的赛务判定后端：把报名身份、号码布领取、分区检录、计时点和完赛事件串成可追溯流程，支撑名次奖、中国籍特别奖、破赛会纪录奖励的判定与公示，以及现场救治资源的脱敏调度。

## 架构

```
adjudication/
  timeutil.py   业务时间保留原始时区，内部统一换算 UTC
  rules.py      赛事发布规则版本：国籍资格、奖励范围、纪录、申诉期限
  journal.py    追加式事件日志：event_id 幂等去重，可选 JSONL 持久化
  core.py       投影与判定：名次、并列、奖励、裁决冲销、公示快照
  medical.py    医疗站/救护车/定点医院台账，最近资源锁定，病例脱敏视图
  api.py        HTTP 接口与角色门禁
service.py      运行入口（/health、--check、--data 持久化）
```

核心保证：

- **断网补传 / 重放 / 乱序不改变名次**——所有事实以事件进入日志，按 `event_id` 幂等去重；名次只由事件发生时间决定，与到达顺序无关；同一计时点重复过线取最早有效成绩。
- **成绩更正以新裁决冲销旧结果**——`adjust_time` / `disqualify` / `reinstate` / `wave_change` / `clear_flag` / `tie_break` 都是追加式裁决，原始事件永不修改。
- **已公示榜单不能被静默覆盖**——每次公示生成带规则版本与日志摘要的不可变快照，更正后只能发布新版本，历史版本始终可回溯。
- **人脸只留模板引用与结论**——日志层拒绝任何生物特征原文字段（photo/image/embedding 等），只保存 `template_ref` 与 match/no_match 结论；冒用他人模板、模板复用、核验失败都会生成阻断奖励资格的标记。
- **规则按发布版本计算**——国籍资格、奖励范围、纪录与申诉期限以公示时钉住的规则版本为准，新版规则不溯及已公示榜单。
- **救治调度脱敏与隔离**——求助单对外只含位置与类别；调度锁定最近可用资源；转运目的地仅医疗、调度、管理角色可见。

## 角色

`admin` `checkin` `timing` `awards` `medical` `dispatch` `marshal` `athlete` `public`

开发模式下角色取自 `X-Role` 请求头；生产模式在 `App(role_tokens={...})` 配置令牌后，必须使用 `Authorization: Bearer <token>`。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 服务身份 |
| POST | `/v1/events` | 批量事件接入（逐条回执 accepted/duplicate/rejected） |
| GET | `/v1/results?distance=&gender=` | 实时名次与奖励预览（含资格标记） |
| POST | `/v1/rulings` | 裁决：disqualify / reinstate / adjust_time / wave_change / clear_flag / tie_break |
| POST | `/v1/leaderboards/publish` | 发布公示快照（版本递增，历史不可变） |
| GET | `/v1/leaderboards/current` `/v1/leaderboards/{id}` | 当前与历史公示榜单 |
| POST | `/v1/appeals` `/v1/appeals/{id}/decide` | 申诉（期限按榜单规则版本校验）与裁决 |
| POST | `/v1/medical/requests` | 脱敏求助单，自动锁定最近可用资源 |
| POST | `/v1/medical/cases/{id}/transport` `/close` | 转运定点医院 / 关闭并释放资源 |
| GET | `/v1/medical/resources` | 资源台账（仅医疗/调度/管理） |
| GET | `/v1/finishers/{bib}/trace` | 完赛追溯：身份核验、分枪、计时、名次、奖励依据、申诉历史、救治资源占用 |
| GET/POST | `/v1/rules/current` `/v1/rules` | 查看当前规则版本 / 注册并启用新版本 |

## 运行

```bash
python3 service.py --check          # 基础检查
python3 service.py --port 8000 --data journal.jsonl
python3 -m unittest -v              # 全部场景重演测试
```

`fixtures/sample.json` 描述赛事基础参数（四万人、四个分枪、奖励范围、医疗资源），人脸信息仅使用外部模板引用，仓库不保存照片或生物特征原文。
