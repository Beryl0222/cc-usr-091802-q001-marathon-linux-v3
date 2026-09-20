# 马拉松赛务判定中枢

为四万人规模、四个发令批次的太原马拉松建设的赛务判定后端。把**报名身份 →
号码布领取 → 分区检录 → 计时点 → 完赛 → 裁决 → 公示 → 申诉**串成一条可追溯
流程，并提供脱敏的现场救治调度。赛务数据全部以**事件**形式进入，设备可短时
离线，业务时间保留原始时区并换算 UTC。

人脸信息只保存**外部模板引用**（`template_ref` / `probe_ref`）与比对**结论**
（pass/fail、相似度分值），仓库与接口都不保存照片或生物特征原文。

## 为什么结论可复现：事件溯源

- 所有事实（报名、人脸核验、领物、检录、改枪、计时、发令、裁决、申诉、榜单、
  求助、资源锁定/释放、转运）都是**只追加事件**，落盘为 JSONL。
- 判定（成绩、名次、奖励、违规、资源占用）全部是对事件流按
  `(occurred_at_utc, event_id)` 规范顺序的**纯函数归约**，查询时重算。
  因此：
  - **断网补传**：迟到事件按其业务时间归位，结论不变；
  - **同一事件重放**：相同 `event_id` 或相同 `(device_id, device_seq)`
    只入库一次，返回 `duplicated: true`；
  - **乱序到达**：归约顺序与到达先后无关，最终名次确定。
- 重启后从 JSONL 重放即可得到完全一致的状态。

## 关键判定规则

- **身份链**：报名底库模板 → 领物人脸 → 检录人脸 → 完赛人脸，任一必做环节
  缺失/比对失败即取消成绩；同一抓拍模板出现在两个号码布下判 `shared_face_probe`
  （替跑强信号）。
- **分区与跨枪**：临时改枪以事件发生时刻生效；起点地毯所属枪组与当时生效分区
  不一致判 `cross_wave_start`；检录必须在本人有效分区。
- **重复过线**：终点多次读数仅首次计入成绩，其余记 `duplicate_finish`。
- **成绩**：净成绩（起点地毯→冲线）与枪成绩（所属枪发令→冲线）并存；
  缺必经计时点、顺序不可能、冲线早于发令等判违规。
- **并列**：按公报 `time_precision`（整秒）与 `tie_policy`（默认竞赛排名
  1,1,3）取名次；总名次与中国籍名次男女分组。
- **奖励按公报版本**：总名次奖（前 100）、中国籍特别奖（前 8）、破赛会纪录奖
  的资格、范围、纪录阈值、申诉期限，都取榜单钉住的**发布版本**，公报版本只增不改。
- **成绩更正**：新裁决可点名冲销旧裁决（`supersedes_ruling_ids`、
  `clears_codes`）或以 `correction` 覆盖成绩；被冲销项保留历史、标注 `voided`。
- **公示榜单不可静默覆盖**：发布即物化不可变快照（含 SHA-256 指纹）；更正只能
  发新版并在 `supersedes` 显式替代旧版，旧版始终可取回。申诉期限按榜单钉住的
  公报版本计算，超期申诉被拒绝（422）。
- **医疗脱敏**：求助只携带服务端生成的假名与症状代码，号码布关联仅总指挥可见；
  按实时位置锁定最近可用资源，占用/释放构成时间线；转运细节只对总指挥、当事
  救护车与接收医院开放，无关岗位看不到案件。

## 运行

```bash
pip install -r requirements.txt

python3 service.py --check      # 自检
python3 service.py --replay     # 内存重演四类演练事件并打印判定摘要
python3 -m pytest -q            # 全部测试
python3 -m unittest -v          # 基线契约

# 落库并启动（首次灌入演练事件；之后重启自动重放）
python3 service.py --seed-scenario --store data/events.jsonl --port 8000
python3 service.py --store data/events.jsonl --port 8000
```

## 岗位与鉴权

岗位由请求头 `X-Staff-Role` 声明（生产环境由前置鉴权网关注入）：

| 岗位 | 能力 |
| --- | --- |
| `official` | 上报事件、出裁决、发榜单、查完整追溯 |
| `medical_commander` | 全部脱敏案件、号码布关联、资源占用与转运 |
| `marshal` | 发起求助、锁定/释放资源 |
| `station:<id>` / `ambulance:<id>` / `hospital:<id>` | 仅见与本资源相关案件 |

## 主要接口（前缀 `/v1`）

赛事与身份
`POST /config`、`POST /bulletins`、`GET /bulletins/latest`、`POST /guns`、
`POST /runners`、`POST /face-verifications`、`POST /pickups`、
`POST /checkins`、`POST /reassignments`、`POST /timing`。

判定与公示
`GET /rankings?distance=&gender=&bulletin_version=`、`GET /results`、
`GET /awards`、`GET /runners/{bib}`、`GET /runners/{bib}/trace`、
`POST /rulings`、`POST /appeals`、`POST /appeals/{id}/decision`、
`POST /publications`、`GET /publications/{id}`。

医疗
`POST /medical/resources`、`POST /medical/resources/{id}/availability`、
`POST /medical/sos`、`POST /medical/cases/{id}/lock`、
`POST /medical/cases/{id}/lock-nearest`、`POST /medical/cases/{id}/release`、
`POST /medical/cases/{id}/transport`、`POST /medical/cases/{id}/outcome`、
`GET /medical/cases/{id}`、`GET /medical/cases`、`GET /medical/occupancy`。

通用事件入口 `POST /events`（单条或 `events` 数组，支持 `device_id`/
`device_seq` 幂等），`GET /events/{id}` 取证。所有写接口接受
`occurred_at`（必须带时区偏移），未提供则取入库时刻。

## 追溯演练

`app/scenarios.py` 构造四万人赛事的赛程、计时与救治事件，覆盖：

- **冒名入场**：替跑者 A008 的抓拍出现在被冒名号码布 A006 下，A006 终点
  人脸失败 → A006/A008 均取消成绩；
- **并列成绩**：A002/A003 枪成绩同为 7800 秒，并列第 2，下一名为第 4；
- **跨枪计时 + 成绩更正**：A005 改枪后踩错起点地毯先被判 DQ，录像复核新裁决
  冲销旧判罚并更正成绩，恢复名次；
- **途中急救**：A007 在 15 公里处倒地，脱敏求助 → 锁定最近医疗站 STA-14 与
  最近可用救护车 AMB-03（更近的 AMB-07 被占用而跳过）→ 转运定点医院 →
  释放资源；无关站点查无此案；
- **断网补传/乱序重放**：MAT-09 迟到补传且同一 `device_seq` 重放，
  全部事件打乱顺序重放后名次不变。

从 `GET /v1/results` 的任一行，经 `GET /v1/runners/{bib}/trace` 可逐项追到：
奖励依据（公报版本、发令/冲线/裁决/人脸事件号）、申诉历史（期限、决定、关联
裁决）、公示历史（各版榜单快照）与资源占用时间线。

## 目录

```
app/timeutil.py     原始时区保留 + UTC 换算
app/events.py       只追加 JSONL 事件日志、幂等去重、确定性排序
app/state.py        事件 -> 状态投影
app/scoring.py      成绩/违规/名次/奖励判定（纯函数）
app/publications.py 榜单物化快照、指纹、显式替代、申诉期限
app/medical.py      脱敏求助、最近资源、占用时间线、按岗视图
app/api.py          HTTP 路由与岗位鉴权
app/scenarios.py    四类重演事件流
service.py          运行入口 / --check / --replay / --seed-scenario
fixtures/sample.json 赛事规模基础参数
tests/              事件存储、判定、公示、医疗、端到端 HTTP 测试
```
